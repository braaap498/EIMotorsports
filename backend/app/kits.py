"""
Kit parts and the Custom Label SKU (CLS).

Mounted at /admin/kits/* .

A store product (EI-xxxxxxx) can be a KIT: a few vendor parts bought separately and sold together. The pieces are
kept in product_kit_parts (vendor, vendor part number, quantity). The CLS is built from them:

    182884xx182951xxNB9512     = three pieces, each written once, in the order they were added, joined by "xx"
    a piece with quantity 2    = still written once (the quantity lives in product_kit_parts, not in the label)
    Automatic Distributors     = the label uses the MANUFACTURER number (mfr_number), not the Autodist SKU. The
                                 Autodist SKU stays in product_kit_parts.part_number because that is what goes on the PO.

Saving the pieces builds products.custom_label_sku ONLY when the product has no label yet. A product that already
has one keeps it exactly as it is (edit the label itself to change it). Every eBay listing linked to the product
(channel_listings) must carry the same label, so a changed label is sent to each listing with the SAME safe path the Live listings
editor uses (app/ebay_edit.py): the store's live-push switch must be ON, and only admins can send. Anything that
could not be sent is reported back per listing, never hidden.

  GET  /kits/vendors                    distributors, and which of them has a searchable catalog
  GET  /kits/catalog                    search the vendor catalog (many ways, see catalog_search)
  GET  /kits/catalog/brands             brands in the catalog, for the brand filter
  GET  /kits/{product_id}               the pieces, the CLS, and the linked eBay listings
  PUT  /kits/{product_id}/parts         replace the pieces; builds the CLS only if there is none yet (does NOT talk to eBay)
  POST /kits/{product_id}/sync          send the CLS to every linked eBay listing (admins; needs the store's switch ON)

Also used by other modules:
  expand_rows()           purchase orders: a kit on a PO becomes one line per piece
  credit_kit_receipt()    receiving: pieces received turn into whole kits in stock
"""
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user, has_perm, needs, require_route_access
from app.database import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/kits", tags=["admin-kits"], dependencies=[Depends(require_route_access)])

SUPPLIER = "autodist"
CLS_MAX = 50  # eBay's limit for a custom label
CLS_JOIN = "xx"
TOKEN_RE = re.compile(r"[A-Za-z0-9\-_.]+")
_DIGITS = "regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g')"


# --------------------------------------------------------------------------
# the CLS itself (one place; change the rule here)
# --------------------------------------------------------------------------
def label_token_for(number: str) -> str:
    """A vendor/manufacturer number as it is written in a label: spaces and other odd characters dropped
    (the Autodist matcher ignores them anyway)."""
    return re.sub(r"[^A-Za-z0-9\-_.]", "", (number or "").strip())


def build_cls(parts: list[dict]) -> str:
    """Pieces in the order they were added -> '182884xx182951xxNB9512'.
    Each piece appears once however many are in the kit (and however many times it was added).
    A piece's label text is p['label'] when given (Autodist pieces: the manufacturer number), else its part number."""
    tokens: list[str] = []
    seen: set[str] = set()
    for p in parts:
        tok = (p.get("label") or p.get("part_number") or "").strip()
        if not tok:
            raise HTTPException(400, "Every piece needs a part number.")
        if not TOKEN_RE.fullmatch(tok):
            raise HTTPException(400, f"Part number '{tok}' has characters that cannot go in a custom label (use letters, digits, - _ .).")
        if CLS_JOIN in tok.lower():
            raise HTTPException(400, f"Part number '{tok}' contains '{CLS_JOIN}', which is the separator in the custom label, so it cannot be used. Type the label yourself in the custom label box instead.")
        if tok.lower() in seen:
            continue
        seen.add(tok.lower())
        tokens.append(tok)
    cls = CLS_JOIN.join(tokens)
    if len(cls) > CLS_MAX:
        raise HTTPException(400, f"That kit's custom label would be {len(cls)} characters; eBay allows {CLS_MAX}. Use fewer pieces, or type a shorter label yourself.")
    return cls


def is_catalog_vendor(name: Optional[str]) -> bool:
    n = (name or "").lower()
    return "autodist" in n or "automatic distributors" in n


# --------------------------------------------------------------------------
# vendors and catalog search
# --------------------------------------------------------------------------
@router.get("/vendors")
@needs("products.edit_details", "products.create")
def vendors(db: Session = Depends(get_db)):
    rows = db.execute(text("select id, name from po_distributors order by lower(name), id")).all()
    return {"items": [{"id": r.id, "name": r.name, "has_catalog": is_catalog_vendor(r.name)} for r in rows]}


@router.get("/catalog/brands")
@needs("products.edit_details", "products.create")
def catalog_brands(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select manufacturer, count(*) as n from supplier_items where supplier = :s and active "
            "and coalesce(manufacturer, '') <> '' group by manufacturer order by manufacturer"
        ),
        {"s": SUPPLIER},
    ).all()
    return {"items": [{"name": r.manufacturer, "count": r.n} for r in rows]}


def _norm(v: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", v or "").upper()


@router.get("/catalog")
@needs("products.edit_details", "products.create")
def catalog_search(
    q: Optional[str] = Query(None, max_length=200),
    field: str = Query("all", pattern="^(all|sku|mfr|name|brand|upc)$"),
    match: str = Query("contains", pattern="^(contains|starts|exact)$"),
    brand: Optional[str] = Query(None, max_length=100),
    in_stock: bool = False,
    min_cost: Optional[float] = Query(None, ge=0),
    max_cost: Optional[float] = Query(None, ge=0),
    codes: Optional[str] = Query(None, max_length=4000, description="a pasted list of part numbers / SKUs / UPCs, one per line or comma separated"),
    from_product: Optional[int] = Query(None, description="find the catalog part that matches one of OUR products (barcode or manufacturer part number)"),
    sort: str = Query("best", pattern="^(best|sku|name|brand|cost_asc|cost_desc|stock)$"),
    include_inactive: bool = False,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(current_user),
):
    """Ways to find a part: words anywhere; one field only (SKU, manufacturer number, name, brand, UPC);
    contains / starts with / exact (dashes and spaces ignored for numbers); brand; in stock only; cost range;
    a pasted list of numbers; or 'the part that matches this product of ours'. Cost is only returned and only
    filterable for people who may see what the shop pays."""
    see_cost = has_perm(user, "products.edit_price")
    where = ["s.supplier = :s"]
    params: dict = {"s": SUPPLIER, "n": limit, "o": offset}
    if not include_inactive:
        where.append("s.active")
    if brand:
        where.append("lower(s.manufacturer) = lower(:brand)")
        params["brand"] = brand.strip()
    if in_stock:
        where.append("coalesce(s.qty_maine, 0) + coalesce(s.qty_indiana, 0) + coalesce(s.qty_nevada, 0) > 0")
    if see_cost:
        if min_cost is not None:
            where.append("s.dealer_cost >= :minc")
            params["minc"] = min_cost
        if max_cost is not None:
            where.append("s.dealer_cost <= :maxc")
            params["maxc"] = max_cost

    def one(word: str, i: int) -> str:
        """The condition for ONE typed word."""
        norm = _norm(word)
        params[f"n{i}"] = norm
        if match == "exact":
            params[f"w{i}"] = word
            cols = {
                "sku": f"(lower(s.sku) = lower(:w{i}) or upper(regexp_replace(s.sku, '[^A-Za-z0-9]', '', 'g')) = :n{i})",
                "mfr": f"(s.mfr_number_norm = :n{i})",
                "name": f"(lower(s.name) = lower(:w{i}))",
                "brand": f"(lower(s.manufacturer) = lower(:w{i}))",
                "upc": f"(s.upc_norm = ltrim(:n{i}, '0'))",
            }
        elif match == "starts":
            params[f"w{i}"] = f"{word}%"
            params[f"p{i}"] = f"{norm}%"
            cols = {
                "sku": f"(s.sku ilike :w{i} or upper(regexp_replace(s.sku, '[^A-Za-z0-9]', '', 'g')) like :p{i})",
                "mfr": f"(s.mfr_number ilike :w{i} or s.mfr_number_norm like :p{i})",
                "name": f"(s.name ilike :w{i})",
                "brand": f"(s.manufacturer ilike :w{i})",
                "upc": f"(s.upc like :w{i})",
            }
        else:
            params[f"w{i}"] = f"%{word}%"
            params[f"p{i}"] = f"%{norm}%" if norm else "%"
            cols = {
                "sku": f"(s.sku ilike :w{i} or upper(regexp_replace(s.sku, '[^A-Za-z0-9]', '', 'g')) like :p{i})",
                "mfr": f"(s.mfr_number ilike :w{i} or s.mfr_number_norm like :p{i})",
                "name": f"(s.name ilike :w{i})",
                "brand": f"(s.manufacturer ilike :w{i})",
                "upc": f"(s.upc ilike :w{i})",
            }
        if field == "all":
            return "(" + " or ".join(cols.values()) + ")"
        return cols[field]

    words = [w for w in re.split(r"\s+", (q or "").strip()) if w][:6]
    if match == "exact" and field == "all" and words:
        # an exact search over every field is a number lookup: SKU, manufacturer number or UPC
        field_all_exact = True
    else:
        field_all_exact = False
    for i, w in enumerate(words):
        if match == "exact" and field == "name":
            continue
        where.append(one(w, i))
    if match == "exact" and field == "name" and q and q.strip():
        params["w_name"] = q.strip()
        where.append("lower(s.name) = lower(:w_name)")
    _ = field_all_exact

    code_list = [c for c in re.split(r"[\s,;]+", codes or "") if c][:60]
    if code_list:
        params["codes_n"] = [_norm(c) for c in code_list if _norm(c)]
        params["codes_u"] = [_norm(c).lstrip("0") for c in code_list if _norm(c).lstrip("0")]
        where.append(
            "(upper(regexp_replace(s.sku, '[^A-Za-z0-9]', '', 'g')) = any(cast(:codes_n as text[])) "
            "or s.mfr_number_norm = any(cast(:codes_n as text[])) "
            "or s.upc_norm = any(cast(:codes_u as text[])))"
        )

    if from_product:
        prod = db.execute(
            text(
                "select nullif(ltrim(regexp_replace(coalesce(p.barcode, ''), '\\D', '', 'g'), '0'), '') as upc_n, "
                "nullif(upper(regexp_replace(coalesce(nullif(btrim(p.manufacturer_part_number), ''), "
                "  (select sp.value from product_item_specifics sp where sp.product_id = p.id "
                "   and sp.name = 'Manufacturer Part Number' order by sp.sort_order, sp.id limit 1), ''), "
                "  '[^A-Za-z0-9]', '', 'g')), '') as mpn_n "
                "from products p where p.id = :id"
            ),
            {"id": from_product},
        ).first()
        if not prod:
            raise HTTPException(404, "That product does not exist.")
        params["fp_u"], params["fp_m"] = prod.upc_n, prod.mpn_n
        where.append("((cast(:fp_u as text) is not null and s.upc_norm = cast(:fp_u as text)) "
                     "or (cast(:fp_m as text) is not null and s.mfr_number_norm = cast(:fp_m as text)))")

    order = {
        "sku": "s.sku",
        "name": "lower(s.name), s.sku",
        "brand": "s.manufacturer, s.sku",
        "cost_asc": "s.dealer_cost asc nulls last, s.sku" if see_cost else "s.sku",
        "cost_desc": "s.dealer_cost desc nulls last, s.sku" if see_cost else "s.sku",
        "stock": "(coalesce(s.qty_maine, 0) + coalesce(s.qty_indiana, 0) + coalesce(s.qty_nevada, 0)) desc, s.sku",
    }.get(sort)
    if order is None:  # "best": an exact number match first, then in-stock, then by brand and SKU
        first = (q or "").strip()
        params["best_n"] = _norm(first)
        params["best_l"] = first.lower()
        order = (
            "(case when cast(:best_n as text) <> '' and (upper(regexp_replace(s.sku, '[^A-Za-z0-9]', '', 'g')) = cast(:best_n as text) "
            "or s.mfr_number_norm = cast(:best_n as text)) then 0 "
            "when lower(s.sku) = cast(:best_l as text) then 0 else 1 end), "
            "(coalesce(s.qty_maine, 0) + coalesce(s.qty_indiana, 0) + coalesce(s.qty_nevada, 0) > 0) desc, s.manufacturer, s.sku"
        )
    clause = " and ".join(where)
    total = db.execute(text(f"select count(*) from supplier_items s where {clause}"), params).scalar()
    rows = db.execute(
        text(
            f"select s.id, s.sku, s.mfr_number, s.manufacturer, s.name, s.unit_of_measure, s.list_price, s.dealer_cost, "
            f"s.qty_maine, s.qty_indiana, s.qty_nevada, s.upc, s.active from supplier_items s "
            f"where {clause} order by {order} limit :n offset :o"
        ),
        params,
    ).all()
    items = []
    for r in rows:
        d = dict(r._mapping)
        d["in_stock_total"] = sum(int(d.get(k) or 0) for k in ("qty_maine", "qty_indiana", "qty_nevada"))
        if not see_cost:
            d.pop("dealer_cost", None)
        else:
            d["dealer_cost"] = float(d["dealer_cost"]) if d["dealer_cost"] is not None else None
        d["list_price"] = float(d["list_price"]) if d.get("list_price") is not None else None
        items.append(d)
    return {"total": total, "items": items}


# --------------------------------------------------------------------------
# the pieces of a kit
# --------------------------------------------------------------------------
class KitPartIn(BaseModel):
    distributor_id: int
    supplier_item_id: Optional[int] = None
    part_number: Optional[str] = Field(None, max_length=60)
    description: Optional[str] = Field(None, max_length=250)
    unit_cost: Optional[float] = Field(None, ge=0, le=1_000_000)
    quantity: int = Field(1, ge=1, le=1000)


class PartsIn(BaseModel):
    parts: list[KitPartIn] = Field(default_factory=list, max_length=25)


def _parts_of(db: Session, product_id: int, see_cost: bool = True) -> list[dict]:
    rows = db.execute(
        text(
            "select k.id, k.distributor_id, d.name as vendor, k.supplier_item_id, k.part_number, k.description, "
            "k.unit_cost, k.quantity, k.sort_order, "
            "coalesce(nullif(btrim(si.mfr_number), ''), k.part_number) as label_part from product_kit_parts k "
            "left join supplier_items si on si.id = k.supplier_item_id "
            "left join po_distributors d on d.id = k.distributor_id where k.product_id = :p order by k.sort_order, k.id"
        ),
        {"p": product_id},
    ).all()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["unit_cost"] = float(d["unit_cost"]) if d["unit_cost"] is not None and see_cost else None
        out.append(d)
    return out


def replace_parts(db: Session, product_id: int, parts: list[KitPartIn]) -> dict:
    """Replaces the pieces. Builds products.custom_label_sku only when the product has none yet: an existing
    label is never changed here (it can be edited by hand on the product, which then goes to every listing).
    No commit (the caller commits).
    Returns {cls: the label the product has now (or None), cls_changed: True when this call set it, duplicates}."""
    db.execute(text("delete from product_kit_parts where product_id = :p"), {"p": product_id})
    if not parts:
        return {"cls": _product_cls(db, product_id), "cls_changed": False, "duplicates": []}
    dists = {r.id: r.name for r in db.execute(text("select id, name from po_distributors")).all()}
    clean: list[dict] = []
    for i, p in enumerate(parts):
        if p.distributor_id not in dists:
            raise HTTPException(400, "One of the pieces has a vendor that no longer exists. Pick the vendor again.")
        number, desc, cost = (p.part_number or "").strip(), (p.description or "").strip() or None, p.unit_cost
        label = ""
        if p.supplier_item_id:
            it = db.execute(
                text("select sku, mfr_number, name, manufacturer, dealer_cost from supplier_items where id = :i and supplier = :s"),
                {"i": p.supplier_item_id, "s": SUPPLIER},
            ).first()
            if not it:
                raise HTTPException(400, "A piece points at a catalog part that is no longer in the catalog. Pick it again.")
            number = it.sku  # the Autodist SKU is what goes on the purchase order
            label = label_token_for(it.mfr_number or "")  # the label uses the manufacturer number
            desc = desc or " ".join(x for x in (it.manufacturer, it.name) if x)[:250]
            cost = float(it.dealer_cost) if it.dealer_cost is not None else cost
        if not number:
            raise HTTPException(400, "Every piece needs a part number.")
        clean.append({"distributor_id": p.distributor_id, "supplier_item_id": p.supplier_item_id, "part_number": number,
                      "label": label or number, "description": desc, "unit_cost": cost, "quantity": p.quantity, "sort_order": i})
    for c in clean:
        db.execute(
            text(
                "insert into product_kit_parts (product_id, distributor_id, supplier_item_id, part_number, description, unit_cost, quantity, sort_order) "
                "values (:p, :d, :si, :pn, :desc, :uc, :q, :so)"
            ),
            {"p": product_id, "d": c["distributor_id"], "si": c["supplier_item_id"], "pn": c["part_number"],
             "desc": c["description"], "uc": c["unit_cost"], "q": c["quantity"], "so": c["sort_order"]},
        )
    existing = _product_cls(db, product_id)
    if existing:
        return {"cls": existing, "cls_changed": False, "duplicates": []}
    cls = build_cls(clean)
    try:
        db.execute(
            text("update products set custom_label_sku = :c, updated_at = now() where id = :p"), {"c": cls, "p": product_id}
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"Another product already uses the custom label {cls}.")
    dups = db.execute(
        text("select id, sku_display as sku, title from products where custom_label_sku = :c and id <> :p limit 5"),
        {"c": cls, "p": product_id},
    ).all()
    return {"cls": cls, "cls_changed": True, "duplicates": [dict(d._mapping) for d in dups]}


def _linked_listings(db: Session, product_id: int) -> list[dict]:
    rows = db.execute(
        text(
            f"select cl.id, cl.store, {_DIGITS} as item_id, cl.status, cl.cls_pushed, cl.cls_pushed_at "
            f"from channel_listings cl where cl.product_id = :p order by cl.store, cl.id"
        ),
        {"p": product_id},
    ).all()
    return [dict(r._mapping) for r in rows if r.item_id]


def _product_cls(db: Session, product_id: int) -> Optional[str]:
    v = db.execute(text("select custom_label_sku from products where id = :p"), {"p": product_id}).scalar()
    return (v or "").strip() or None


@router.get("/{product_id}")
@needs("products.view", "products.edit_details", "products.create")
def get_kit(product_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    if not db.execute(text("select 1 from products where id = :p"), {"p": product_id}).first():
        raise HTTPException(404, "Product not found")
    cls = _product_cls(db, product_id)
    listings = _linked_listings(db, product_id)
    for l in listings:
        l["in_sync"] = bool(cls) and (l["cls_pushed"] or "") == cls
    return {
        "cls": cls,
        "parts": _parts_of(db, product_id, see_cost=has_perm(user, "products.edit_price")),
        "listings": listings,
        "can_sync": user.role in ("owner", "admin"),
    }


@router.put("/{product_id}/parts")
@needs("products.edit_details")
def put_parts(product_id: int, body: PartsIn, db: Session = Depends(get_db)):
    if not db.execute(text("select 1 from products where id = :p"), {"p": product_id}).first():
        raise HTTPException(404, "Product not found")
    res = replace_parts(db, product_id, body.parts)
    db.commit()
    listings = _linked_listings(db, product_id)
    return {
        "cls": res["cls"],
        "cls_changed": res["cls_changed"],
        "duplicates": res["duplicates"],
        # only a label that was just created needs sending; an unchanged one is already on the listings
        "listings_to_update": len(listings) if res["cls_changed"] else 0,
    }


# --------------------------------------------------------------------------
# sending the CLS to every linked eBay listing
# --------------------------------------------------------------------------
def sync_listings(db: Session, user: AuthUser, product_id: int) -> list[dict]:
    """One result per linked listing. Uses the Live listings editor's own read / build / push, so the same rules
    apply: the store's live-push switch, single (non-variation) listings only, and a push only if the request
    matches what was built."""
    from app import ebay_edit as ee  # imported here: ebay_edit pulls in the whole eBay stack

    cls = _product_cls(db, product_id)
    if not cls:
        raise HTTPException(400, "This product has no custom label to send.")
    out: list[dict] = []
    for l in _linked_listings(db, product_id):
        res = {"store": l["store"], "item_id": l["item_id"], "status": "", "detail": ""}
        out.append(res)
        try:
            enabled = bool(db.execute(text("select live_pushes_enabled from ebay_accounts where lower(store) = lower(:s)"), {"s": l["store"]}).scalar())
            current = ee._read_item(db, l["store"], l["item_id"])
            if current.get("has_variations"):
                res["status"], res["detail"] = "skipped", "This listing has variations; eBay only lets us change a custom label on single listings."
                continue
            if (current.get("sku") or "") == cls:
                res["status"], res["detail"] = "already_current", "eBay already shows this label."
            else:
                changes = ee.Changes(sku=cls)
                calls, diff = ee._build(current, changes)
                fp = ee._fingerprint(l["store"], calls)
                if not enabled:
                    res["status"], res["detail"] = "not_sent", f"Live pushes are OFF for {l['store']}. Turn them on in the Live listings Stores box, then send again."
                    continue
                ee.push(ee.PushIn(store=l["store"], item_id=l["item_id"], changes=changes, fingerprint=fp), db, user)
                res["status"], res["detail"] = "updated", f"{current.get('sku') or '(none)'} -> {cls}"
            db.execute(
                text("update channel_listings set cls_pushed = :c, cls_pushed_at = now() where id = :i"), {"c": cls, "i": l["id"]}
            )
            db.commit()
        except HTTPException as e:
            db.rollback()
            res["status"], res["detail"] = "error", str(e.detail)
        except Exception:
            db.rollback()
            log.exception("CLS sync failed for listing %s", l["item_id"])
            res["status"], res["detail"] = "error", "Something unexpected went wrong with this listing."
    return out


@router.post("/{product_id}/sync")
@needs("products.edit_details")
def sync(product_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Only an owner or admin can send changes to eBay. The label is saved here; ask an admin to send it.")
    results = sync_listings(db, user, product_id)
    return {"cls": _product_cls(db, product_id), "results": results}


# --------------------------------------------------------------------------
# purchase orders: a kit becomes one line per piece
# --------------------------------------------------------------------------
def expand_rows(db: Session, rows: list[dict], distributor_id: int) -> tuple[list[dict], list[str]]:
    """rows: the PO lines about to be written (product_id, item, description, quantity, rate).
    A product with kit pieces for THIS vendor is replaced by one line per piece (pieces x kits ordered), at the
    catalog's current dealer cost when it has one. Pieces bought from another vendor are left off and reported,
    so they can go on that vendor's PO. A kit with no pieces for this vendor stays a normal line."""
    out: list[dict] = []
    notes: list[str] = []
    for r in rows:
        parts = db.execute(
            text(
                "select k.id, k.distributor_id, k.part_number, k.description, k.unit_cost, k.quantity, k.supplier_item_id, "
                "       si.dealer_cost as live_cost, si.name as live_name, d.name as vendor "
                "from product_kit_parts k left join supplier_items si on si.id = k.supplier_item_id "
                "left join po_distributors d on d.id = k.distributor_id "
                "where k.product_id = :p order by k.sort_order, k.id"
            ),
            {"p": r["product_id"]},
        ).all()
        mine = [p for p in parts if p.distributor_id == distributor_id]
        if not mine:
            out.append(r)
            continue
        for p in mine:
            cost = p.live_cost if p.live_cost is not None else p.unit_cost
            out.append(
                {
                    "product_id": r["product_id"],
                    "item": p.part_number,
                    "description": (p.description or p.live_name or r["description"] or "")[:250],
                    "quantity": r["quantity"] * p.quantity,
                    "rate": float(cost) if cost is not None else 0.0,
                    "kit_product_id": r["product_id"],
                    "kit_qty": r["quantity"],
                    "per_kit": p.quantity,
                }
            )
        for p in parts:
            if p.distributor_id != distributor_id:
                notes.append(f"{r['description'][:60]}: piece {p.part_number} (x{p.quantity * r['quantity']}) is bought from {p.vendor or 'another vendor'}, so it is not on this PO.")
    return out, notes


# --------------------------------------------------------------------------
# receiving: pieces in, whole kits to stock
# --------------------------------------------------------------------------
def credit_kit_receipt(db: Session, po_id: int, kit_product_id: int, user_email: Optional[str], po_number) -> int:
    """After a piece of a kit is received (partly or fully), the number of WHOLE kits that can now be made from
    everything received on this PO is the smallest of (received / per kit) across the pieces. Only the new kits
    are added to stock. Returns the number just added. No commit."""
    lines = db.execute(
        text("select per_kit, coalesce(received_qty, 0) as got from po_lines where po_id = :po and kit_product_id = :k"),
        {"po": po_id, "k": kit_product_id},
    ).all()
    if not lines:
        return 0
    whole = min(int(l.got) // max(int(l.per_kit or 1), 1) for l in lines)
    done = db.execute(
        text("select kits_credited from po_kit_credits where po_id = :po and kit_product_id = :k for update"),
        {"po": po_id, "k": kit_product_id},
    ).scalar() or 0
    add = whole - int(done)
    if add <= 0:
        return 0
    db.execute(
        text("update products set quantity_on_hand = coalesce(quantity_on_hand, 0) + :q, updated_at = now() where id = :p"),
        {"q": add, "p": kit_product_id},
    )
    db.execute(
        text(
            "insert into inventory_transactions (product_id, transaction_type, quantity_change, reference, legacy_created_by) "
            "values (:p, 'Receive', :q, :ref, :by)"
        ),
        {"p": kit_product_id, "q": add, "ref": f"Kit made from pieces received - PO {po_number}", "by": user_email},
    )
    db.execute(
        text(
            "insert into po_kit_credits (po_id, kit_product_id, kits_credited) values (:po, :k, :n) "
            "on conflict (po_id, kit_product_id) do update set kits_credited = excluded.kits_credited"
        ),
        {"po": po_id, "k": kit_product_id, "n": whole},
    )
    return add
