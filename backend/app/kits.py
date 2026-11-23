"""
Kit parts and the Custom Label SKU (CLS).

Mounted at /admin/kits/* .

A store product (EI-xxxxxxx) can be a KIT: a few vendor parts bought separately and sold together. The pieces are
kept in product_kit_parts (vendor, vendor part number, quantity). The CLS is built from them:

    182884xx182951xxNB9512     = three pieces, each written once, in the order they were added, joined by "xx"
    a piece with quantity 2    = still written once (the quantity lives in product_kit_parts, not in the label)
    Automatic Distributors     = the label uses the MANUFACTURER number (mfr_number) by default, or the Autodist SKU when
                                 the person picks that (label_by). The
                                 Autodist SKU stays in product_kit_parts.part_number because that is what goes on the PO.

Saving the pieces builds products.custom_label_sku ONLY when the product has no label yet. A product that already
has one keeps it exactly as it is (edit the label itself to change it). Every eBay listing linked to the product
(channel_listings) must carry the same label, so a changed label is sent to each listing with the SAME safe path the Live listings
editor uses (app/ebay_edit.py): the store's live-push switch must be ON, and only admins can send. Anything that
could not be sent is reported back per listing, never hidden.

  GET  /kits/vendors                    distributors, and which of them has a searchable catalog
  GET  /kits/catalog                    search the vendor catalog (many ways, see catalog_search)
  GET  /kits/catalog/brands             brands in the catalog, for the brand filter
  GET  /kits/manual-catalog             the saved parts of a distributor that has no catalog here (typed once, picked after that)
  POST /kits/manual-catalog             save a part by hand;  PUT/DELETE /kits/manual-catalog/{id}  edit / remove one
  POST /kits/manual-catalog/{id}/make-product   make a (not yet published) product from a saved part
  GET  /kits/{product_id}               the pieces, the CLS, and the linked eBay listings
  PUT  /kits/{product_id}/parts         replace the pieces; builds the CLS only if there is none yet (does NOT talk to eBay)
  POST /kits/{product_id}/sync          send the CLS to every linked eBay listing, then to their Selling Manager inventory
                                        records (admins; needs the store's switch ON)

Also used by other modules:
  expand_rows()           purchase orders: a kit on a PO becomes one line per piece
  credit_kit_receipt()    receiving: pieces received turn into whole kits in stock
"""
import logging
import re
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user, has_perm, needs, require_route_access
from app.database import get_db
from app.images import full_url, thumb_url

log = logging.getLogger(__name__)
router = APIRouter(prefix="/kits", tags=["admin-kits"], dependencies=[Depends(require_route_access)])

SUPPLIER = "autodist"  # the default catalog (Automatic Distributors)
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


def supplier_for(name: Optional[str]) -> Optional[str]:
    """Which stored catalog (supplier_items.supplier) a distributor's name goes with, or None when it has no catalog here."""
    n = (name or "").lower()
    if "autodist" in n or "automatic distributors" in n:
        return "autodist"
    if re.search(r"rocky\s*mountain\s*atv|rmatv", n):
        return "rmatv"
    return None


def is_catalog_vendor(name: Optional[str]) -> bool:
    return supplier_for(name) is not None


def catalog_loaded(db: Session, name: Optional[str]) -> bool:
    """True when the distributor has a catalog here AND parts have been imported into it. A distributor that is known to have
    a catalog (Rocky Mountain, by name) but never had one imported behaves like any other distributor with no catalog: its parts
    are typed and saved instead."""
    sup = supplier_for(name)
    if sup is None:
        return False
    return bool(db.execute(text("select exists (select 1 from supplier_items where supplier = :s)"), {"s": sup}).scalar())


SUPPLIER_RE = "^(autodist|rmatv)$"
# "The product of a catalog part" means the SINGLE product, never a kit that merely contains the part. A single product is
# one with no distributor-part line or exactly one (quantity 1). It belongs to a catalog part when
#   * its one part line points at the part (link), or
#   * it has no pointer to a different part and its barcode (UPC) or manufacturer part # equals the part's
#     (how products that were made before the part lines existed, or by hand, are recognised; same rule as Automatic's "ours").
SINGLES_CTE = (
    "singles as ("
    " select p.id,"
    " nullif(ltrim(regexp_replace(coalesce(p.barcode, ''), '[^0-9]', '', 'g'), '0'), '') as upc_n,"
    " nullif(upper(regexp_replace(coalesce(p.manufacturer_part_number, ''), '[^A-Za-z0-9]', '', 'g')), '') as mpn_n,"
    " (select z.supplier_item_id from product_kit_parts z where z.product_id = p.id limit 1) as si_id,"
    " (select z.manual_item_id from product_kit_parts z where z.product_id = p.id limit 1) as mi_id"
    " from products p"
    " where (select count(*) from product_kit_parts z where z.product_id = p.id) <= 1"
    " and not exists (select 1 from product_kit_parts z where z.product_id = p.id and coalesce(z.quantity, 1) <> 1))"
)
_M_NORM = "upper(regexp_replace(coalesce(m.mfr_number, ''), '[^A-Za-z0-9]', '', 'g'))"


def single_conds(kind: str) -> list[str]:
    """Conditions (on a row x of the singles CTE) under which x is the single product of a catalog part.
    kind 'supplier': the part is supplier_items s; kind 'manual': the part is manual_catalog_items m."""
    if kind == "supplier":
        return [
            "x.si_id = s.id",
            "(length(coalesce(s.upc_norm, '')) >= 8 and x.upc_n = s.upc_norm and x.mi_id is null and (x.si_id is null or x.si_id = s.id))",
            "(coalesce(s.mfr_number_norm, '') <> '' and x.mpn_n = s.mfr_number_norm and x.mi_id is null and (x.si_id is null or x.si_id = s.id))",
        ]
    return [
        "x.mi_id = m.id",
        f"({_M_NORM} <> '' and x.mpn_n = {_M_NORM} and x.si_id is null and (x.mi_id is null or x.mi_id = m.id))",
    ]


def single_exists(kind: str) -> str:
    return "(" + " or ".join(f"exists (select 1 from singles x where {c})" for c in single_conds(kind)) + ")"


def single_pick(kind: str) -> str:
    return "(select min(x.id) from singles x where " + " or ".join(single_conds(kind)) + ")"


def single_count(kind: str) -> str:
    return "(select count(*) from singles x where " + " or ".join(single_conds(kind)) + ")"
# a part's stock across every warehouse column (Rocky Mountain has just the one, qty_other)
_STOCK = "(coalesce(s.qty_maine, 0) + coalesce(s.qty_indiana, 0) + coalesce(s.qty_nevada, 0) + coalesce(s.qty_other, 0))"


# --------------------------------------------------------------------------
# vendors and catalog search
# --------------------------------------------------------------------------
@router.get("/vendors")
@needs("products.edit_details", "products.create")
def vendors(db: Session = Depends(get_db)):
    rows = db.execute(text("select id, name from po_distributors order by lower(name), id")).all()
    loaded = {sup: catalog_loaded(db, name) for sup, name in (("autodist", "autodist"), ("rmatv", "rmatv"))}
    return {
        "items": [
            {
                "id": r.id,
                "name": r.name,
                "has_catalog": bool(supplier_for(r.name)) and loaded.get(supplier_for(r.name) or "", False),
                "supplier": supplier_for(r.name),
            }
            for r in rows
        ]
    }


@router.get("/manual-catalog")
@needs("products.edit_details", "products.create")
def manual_catalog(
    distributor_id: int = Query(..., description="a distributor that has no catalog here"),
    q: Optional[str] = Query(None, max_length=200),
    only: str = Query("all", pattern="^(all|no_product|has_product)$"),
    sort: str = Query("sku", pattern="^(brand|sku|mfr|name|cost)$"),
    dir: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(current_user),
):
    """The parts already typed for this distributor, to pick from instead of typing them again. Words match the vendor part #,
    the manufacturer # or the description (dashes and spaces in part numbers are ignored). Each part also says which SINGLE product points at it
    (product_id, product_title, products = how many; a kit that contains the part does not count), and `only` limits the list to parts with / without a product.
    Cost only for people who may see it."""
    see_cost = has_perm(user, "products.edit_price")
    where = ["m.distributor_id = :d"]
    params: dict = {"d": distributor_id, "n": limit, "o": offset}
    for i, w in enumerate([w for w in re.split(r"\s+", (q or "").strip()) if w][:6]):
        params[f"w{i}"] = f"%{w}%"
        params[f"p{i}"] = f"%{_norm(w)}%"
        where.append(
            f"(m.part_number ilike :w{i} or m.mfr_number ilike :w{i} or m.description ilike :w{i} or m.manufacturer ilike :w{i} "
            f"or m.part_norm like :p{i} or upper(regexp_replace(coalesce(m.mfr_number, ''), '[^A-Za-z0-9]', '', 'g')) like :p{i})"
        )
    used = single_exists("manual")
    if only == "no_product":
        where.append(f"not {used}")
    elif only == "has_product":
        where.append(used)
    clause = " and ".join(where)
    d = "desc" if dir == "desc" else "asc"
    key = sort if (sort != "cost" or see_cost) else "sku"
    order_by = {
        "brand": f"lower(m.manufacturer) {d} nulls last, lower(m.part_number)",
        "sku": f"lower(m.part_number) {d}, m.id",
        "mfr": f"lower(m.mfr_number) {d} nulls last, lower(m.part_number)",
        "name": f"lower(m.description) {d} nulls last, lower(m.part_number)",
        "cost": f"m.unit_cost {d} nulls last, lower(m.part_number)",
    }[key]
    try:
        total = db.execute(text(f"with {SINGLES_CTE} select count(*) from manual_catalog_items m where {clause}"), params).scalar()
        rows = db.execute(
            text(
                f"with {SINGLES_CTE} "
                "select m.id, m.part_number, m.mfr_number, m.manufacturer, m.image_key, m.description, m.unit_cost, "
                f"{single_pick('manual')} as product_id, "
                f"{single_count('manual')} as products "
                f"from manual_catalog_items m where {clause} order by {order_by} limit :n offset :o"
            ),
            params,
        ).all()
        titles = {}
        ids = [r.product_id for r in rows if r.product_id]
        if ids:
            titles = {t.id: (t.title, t.sku_display) for t in db.execute(text("select id, title, sku_display from products where id = any(cast(:i as bigint[]))"), {"i": ids}).all()}
    except Exception:
        db.rollback()
        return {"items": [], "total": 0}
    return {
        "total": int(total or 0),
        "items": [
            {
                "id": r.id,
                "sku": r.part_number,  # the column is called SKU, as in the Automatic Distributors catalog
                "part_number": r.part_number,
                "mfr_number": r.mfr_number,
                "manufacturer": r.manufacturer,
                "image": thumb_url(r.image_key),
                "image_full": full_url(r.image_key),
                "description": r.description,
                "unit_cost": float(r.unit_cost) if r.unit_cost is not None and see_cost else None,
                "product_id": r.product_id,
                "product_title": (titles.get(r.product_id) or (None, None))[0],
                "product_sku": (titles.get(r.product_id) or (None, None))[1],
                "products": int(r.products or 0),
            }
            for r in rows
        ],
    }


class ManualItemIn(BaseModel):
    distributor_id: int
    part_number: str = Field(..., min_length=1, max_length=60)
    mfr_number: Optional[str] = Field(None, max_length=60)
    manufacturer: Optional[str] = Field(None, max_length=80)
    description: Optional[str] = Field(None, max_length=250)
    unit_cost: Optional[float] = Field(None, ge=0, le=1_000_000)


@router.post("/manual-catalog", status_code=201)
@needs("products.edit_details", "products.create")
def add_manual_item(body: ManualItemIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    """Save a part of a distributor that has no catalog here (the same list the product screen picks from)."""
    dist = db.execute(text("select id, name from po_distributors where id = :i"), {"i": body.distributor_id}).first()
    if not dist:
        raise HTTPException(404, "That distributor no longer exists.")
    if catalog_loaded(db, dist.name):
        raise HTTPException(400, f"{dist.name} has a catalog here; its parts come from the catalog.")
    number = body.part_number.strip()
    norm = _norm(number)
    if not norm:
        raise HTTPException(400, "Type the vendor's part number.")
    dup = db.execute(
        text("select part_number from manual_catalog_items where distributor_id = :d and part_norm = :n"), {"d": dist.id, "n": norm}
    ).first()
    if dup:
        raise HTTPException(409, f"{dup.part_number} is already saved for {dist.name}.")
    cost = body.unit_cost if has_perm(user, "products.edit_price") else None
    row = db.execute(
        text(
            "insert into manual_catalog_items (distributor_id, part_number, part_norm, mfr_number, manufacturer, description, unit_cost) "
            "values (:d, :pn, :pnorm, :mfr, :brand, :desc, :uc) returning id, part_number, mfr_number, manufacturer, description, unit_cost"
        ),
        {"d": dist.id, "pn": number, "pnorm": norm, "mfr": (body.mfr_number or "").strip() or None,
         "brand": (body.manufacturer or "").strip() or None, "desc": (body.description or "").strip() or None, "uc": cost},
    ).first()
    db.commit()
    return {"id": row.id, "part_number": row.part_number, "mfr_number": row.mfr_number, "manufacturer": row.manufacturer, "description": row.description,
            "unit_cost": float(row.unit_cost) if row.unit_cost is not None and has_perm(user, "products.edit_price") else None,
            "product_id": None, "product_title": None, "products": 0}


class ManualItemEdit(BaseModel):
    mfr_number: Optional[str] = Field(None, max_length=60)
    manufacturer: Optional[str] = Field(None, max_length=80)
    description: Optional[str] = Field(None, max_length=250)
    unit_cost: Optional[float] = Field(None, ge=0, le=1_000_000)


@router.put("/manual-catalog/{item_id}")
@needs("products.edit_details")
def edit_manual_item(item_id: int, body: ManualItemEdit, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    """Change a saved part. Every piece that points at it follows (cost and description on the next PO, manufacturer # now).
    The vendor part # cannot be changed here: it is what the custom label and the PO use. Cost only for people who may set prices."""
    sets = ["mfr_number = :mfr", "manufacturer = :brand", "description = :desc", "updated_at = now()"]
    params: dict = {"i": item_id, "mfr": (body.mfr_number or "").strip() or None, "brand": (body.manufacturer or "").strip() or None, "desc": (body.description or "").strip() or None}
    if has_perm(user, "products.edit_price"):
        sets.append("unit_cost = :uc")
        params["uc"] = body.unit_cost
    row = db.execute(
        text(f"update manual_catalog_items set {', '.join(sets)} where id = :i returning id, part_number, mfr_number, manufacturer, description, unit_cost"),
        params,
    ).first()
    if not row:
        db.rollback()
        raise HTTPException(404, "That saved part no longer exists.")
    # the product's Manufacturer part # is the pieces' numbers joined: refresh it for the products that use this part
    for pid in [r.product_id for r in db.execute(text("select distinct product_id from product_kit_parts where manual_item_id = :i"), {"i": item_id}).all()]:
        follow = parts_mpn(db, pid)
        if follow:
            db.execute(
                text("update products set manufacturer_part_number = :m, updated_at = now() where id = :p and manufacturer_part_number is distinct from :m"),
                {"m": follow, "p": pid},
            )
    db.commit()
    see_cost = has_perm(user, "products.edit_price")
    return {"id": row.id, "part_number": row.part_number, "mfr_number": row.mfr_number, "manufacturer": row.manufacturer, "description": row.description,
            "unit_cost": float(row.unit_cost) if row.unit_cost is not None and see_cost else None}


@router.delete("/manual-catalog/{item_id}")
@needs("products.edit_details")
def delete_manual_item(item_id: int, db: Session = Depends(get_db)):
    """Remove a saved part. Refused while a product's distributor part points at it."""
    n = db.execute(text("select count(distinct product_id) from product_kit_parts where manual_item_id = :i"), {"i": item_id}).scalar() or 0
    if n:
        raise HTTPException(409, f"{n} product{'' if n == 1 else 's'} use this part. Remove it from the product's distributor parts first.")
    gone = db.execute(text("delete from manual_catalog_items where id = :i returning id"), {"i": item_id}).first()
    if not gone:
        db.rollback()
        raise HTTPException(404, "That saved part no longer exists.")
    db.commit()
    return {"ok": True}


class PartPhotoIn(BaseModel):
    data: str = Field(..., max_length=21 * 1024 * 1024, description="the picture, base64 encoded")


@router.post("/manual-catalog/{item_id}/photo")
@needs("products.edit_details")
def set_manual_photo(item_id: int, body: PartPhotoIn, db: Session = Depends(get_db)):
    """Give a saved part its picture (the same storage and shrinking as product photos). A product made from the part gets it."""
    from app.admin_photos import _process, _upload  # late import: keeps the two modules independent at start-up

    if not db.execute(text("select 1 from manual_catalog_items where id = :i"), {"i": item_id}).first():
        raise HTTPException(404, "That saved part no longer exists.")
    p = _process(body.data)
    _upload(p)
    db.execute(text("update manual_catalog_items set image_key = :k, updated_at = now() where id = :i"), {"k": p.key, "i": item_id})
    db.commit()
    return {"image": thumb_url(p.key), "image_full": full_url(p.key)}


@router.delete("/manual-catalog/{item_id}/photo")
@needs("products.edit_details")
def clear_manual_photo(item_id: int, db: Session = Depends(get_db)):
    """Take the picture off a saved part (the stored file is left alone: products may use it)."""
    db.execute(text("update manual_catalog_items set image_key = null, updated_at = now() where id = :i"), {"i": item_id})
    db.commit()
    return {"ok": True}


@router.post("/manual-catalog/{item_id}/make-product", status_code=201)
@needs("products.create")
def make_manual_product(item_id: int, db: Session = Depends(get_db)):
    """Make one product from a saved part, the way the Automatic Distributors page does from a catalog part: a new product
    (NOT published: it has no price yet) with one distributor part (quantity 1) that follows the saved part, the saved part's
    manufacturer # on the product, and the custom label set to the vendor part # when that is free. Photos, price, category
    and the rest are set on the product afterwards."""
    from app.admin_catalog import slugify  # late import: that module imports this one

    m = db.execute(
        text("select m.id, m.distributor_id, m.part_number, m.mfr_number, m.manufacturer, m.image_key, m.description, m.unit_cost from manual_catalog_items m where m.id = :i"),
        {"i": item_id},
    ).first()
    if not m:
        raise HTTPException(404, "That saved part no longer exists.")
    used = db.execute(
        text(f"with {SINGLES_CTE} select p.id, p.title from manual_catalog_items m join products p on p.id = {single_pick('manual')} where m.id = :i"),
        {"i": item_id},
    ).first()
    if used:
        raise HTTPException(409, f"{m.part_number} already has its single product: #{used.id} {used.title}")
    title = ((m.description or "").strip() or " ".join(x for x in ((m.mfr_number or "").strip(), m.part_number) if x))[:200]
    base = slugify(title) or slugify(f"part {m.part_number}") or "part"
    slug, k = base, 1
    while db.execute(text("select 1 from products where slug = :s"), {"s": slug}).first():
        k += 1
        slug = f"{base}-{k}"
    try:
        pid = db.execute(
            text(
                "insert into products (title, slug, manufacturer_part_number, cost, quantity_on_hand, condition, is_published, is_active, is_universal, show_part_number) "
                "values (:title, :slug, :mpn, :cost, 0, 'New', false, true, false, false) returning id"
            ),
            {"title": title, "slug": slug, "mpn": (m.mfr_number or "").strip() or None, "cost": m.unit_cost},
        ).scalar()
        db.execute(
            text(
                "insert into product_kit_parts (product_id, distributor_id, supplier_item_id, part_number, description, unit_cost, quantity, sort_order, manual_item_id) "
                "values (:p, :d, null, :pn, :desc, :uc, 1, 0, :mi)"
            ),
            {"p": pid, "d": m.distributor_id, "pn": m.part_number, "desc": (m.description or "").strip() or None, "uc": m.unit_cost, "mi": m.id},
        )
        follow = parts_mpn(db, pid)
        if follow:
            db.execute(text("update products set manufacturer_part_number = :m where id = :p"), {"m": follow, "p": pid})
        # the product's Manufacturer is the part's brand (never the distributor)
        if (m.manufacturer or "").strip():
            from app.manufacturers import find_or_create

            mid = find_or_create(db, m.manufacturer)
            if mid:
                db.execute(text("update products set part_manufacturer_id = :m where id = :p"), {"m": mid, "p": pid})
        # the part's picture becomes the product's main photo
        if m.image_key:
            from app import photo_store

            db.execute(
                text("insert into product_photos (product_id, image_key, storage_path, is_primary, sort_order) values (:p, :k, :sp, true, 0)"),
                {"p": pid, "k": m.image_key, "sp": photo_store.full_path(m.image_key)},
            )
        # the custom label is the vendor part #, unless it cannot be a label or another product already has it (left blank, never changed)
        tok = label_token_for(m.part_number)
        label = None
        if tok and TOKEN_RE.fullmatch(tok) and CLS_JOIN not in tok.lower() and len(tok) <= CLS_MAX and not db.execute(
            text("select 1 from products o where lower(o.custom_label_sku) = lower(:t)"), {"t": tok}
        ).first():
            db.execute(text("update products set custom_label_sku = :t, updated_at = now() where id = :p"), {"t": tok, "p": pid})
            label = tok
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Could not make the product: {str(e)[:300]}")
    return {"product_id": pid, "title": title, "custom_label": label, "published": False}


@router.get("/catalog/brands")
@needs("products.edit_details", "products.create")
def catalog_brands(supplier: str = Query(SUPPLIER, pattern=SUPPLIER_RE), db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select manufacturer, count(*) as n from supplier_items where supplier = :s and active "
            "and coalesce(manufacturer, '') <> '' group by manufacturer order by manufacturer"
        ),
        {"s": supplier},
    ).all()
    return {"items": [{"name": r.manufacturer, "count": r.n} for r in rows]}


def _norm(v: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", v or "").upper()


@router.get("/catalog")
@needs("products.edit_details", "products.create")
def catalog_search(
    supplier: str = Query(SUPPLIER, pattern=SUPPLIER_RE, description="which distributor's catalog to search"),
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
    params: dict = {"s": supplier, "n": limit, "o": offset}
    if not include_inactive:
        where.append("s.active")
    if brand:
        where.append("lower(s.manufacturer) = lower(:brand)")
        params["brand"] = brand.strip()
    if in_stock:
        where.append(f"{_STOCK} > 0")
    if see_cost:
        if min_cost is not None:
            where.append("coalesce(s.effective_cost, s.dealer_cost) >= :minc")
            params["minc"] = min_cost
        if max_cost is not None:
            where.append("coalesce(s.effective_cost, s.dealer_cost) <= :maxc")
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
        "cost_asc": "coalesce(s.effective_cost, s.dealer_cost) asc nulls last, s.sku" if see_cost else "s.sku",
        "cost_desc": "coalesce(s.effective_cost, s.dealer_cost) desc nulls last, s.sku" if see_cost else "s.sku",
        "stock": f"{_STOCK} desc, s.sku",
    }.get(sort)
    if order is None:  # "best": an exact number match first, then in-stock, then by brand and SKU
        first = (q or "").strip()
        params["best_n"] = _norm(first)
        params["best_l"] = first.lower()
        order = (
            "(case when cast(:best_n as text) <> '' and (upper(regexp_replace(s.sku, '[^A-Za-z0-9]', '', 'g')) = cast(:best_n as text) "
            "or s.mfr_number_norm = cast(:best_n as text)) then 0 "
            "when lower(s.sku) = cast(:best_l as text) then 0 else 1 end), "
            f"({_STOCK} > 0) desc, s.manufacturer, s.sku"
        )
    clause = " and ".join(where)
    total = db.execute(text(f"select count(*) from supplier_items s where {clause}"), params).scalar()
    rows = db.execute(
        text(
            f"select s.id, s.sku, s.mfr_number, s.manufacturer, s.name, s.unit_of_measure, s.list_price, coalesce(s.effective_cost, s.dealer_cost) as dealer_cost, "
            f"s.qty_maine, s.qty_indiana, s.qty_nevada, coalesce(s.qty_other, 0) as qty_other, s.upc, s.active from supplier_items s "
            f"where {clause} order by {order} limit :n offset :o"
        ),
        params,
    ).all()
    items = []
    for r in rows:
        d = dict(r._mapping)
        d["in_stock_total"] = sum(int(d.get(k) or 0) for k in ("qty_maine", "qty_indiana", "qty_nevada", "qty_other"))
        if not see_cost:
            d.pop("dealer_cost", None)
        else:
            d["dealer_cost"] = float(d["dealer_cost"]) if d["dealer_cost"] is not None else None
        d["list_price"] = float(d["list_price"]) if d.get("list_price") is not None else None
        items.append(d)
    return {"total": total, "items": items}


# --------------------------------------------------------------------------
# manufacturer part numbers: one per piece (product_kit_parts.mfr_number)
# --------------------------------------------------------------------------
# A catalog piece with no number of its own follows the catalog's manufacturer number. The product's own Manufacturer
# part # (products.manufacturer_part_number) then shows every piece's number, " / " between them, so everything that
# already reads or searches that one field keeps working and a search for any one of them finds the product.
MPN_JOIN = " / "


def _norm_mpn(v: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (v or "").upper())


def ensure_mfr_column() -> None:
    """product_kit_parts.mfr_number, and the table that remembers the one-time move of existing numbers. Called at start-up."""
    from app.database import _session_factory

    db = _session_factory()()
    try:
        db.execute(text("alter table product_kit_parts add column if not exists mfr_number text"))
        db.execute(text("alter table product_kit_parts add column if not exists manufacturer text"))  # brand of a typed piece (catalog / saved parts have their own)
        db.execute(text("create table if not exists kit_mfr_backfill (id int primary key default 1, done_at timestamptz not null default now(), summary text)"))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not add product_kit_parts.mfr_number; saving distributor parts will fail until it exists")
    try:
        # the saved parts of distributors that have no catalog here: type a part once, pick it on every other product
        db.execute(
            text(
                "create table if not exists manual_catalog_items ("
                "id bigserial primary key, "
                "distributor_id integer not null references po_distributors(id) on delete cascade, "
                "part_number text not null, part_norm text not null, mfr_number text, description text, unit_cost numeric, "
                "created_at timestamptz not null default now(), updated_at timestamptz not null default now(), "
                "unique (distributor_id, part_norm))"
            )
        )
        # a piece can point at a saved part: its cost, description and manufacturer # then come from the saved part
        db.execute(text("alter table product_kit_parts add column if not exists manual_item_id bigint references manual_catalog_items(id) on delete set null"))
        db.execute(text("alter table manual_catalog_items add column if not exists manufacturer text"))  # the brand (Wiseco, Tusk, ...)
        db.execute(text("alter table manual_catalog_items add column if not exists image_key text"))  # the part's picture (same storage as product photos)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not create manual_catalog_items; the saved-parts list will be empty until it exists")
    finally:
        db.close()


def _line_numbers(db: Session, product_id: int) -> list[tuple[int, str]]:
    """(line id, the number of that piece: its own, else the catalog's, else '') in order."""
    rows = db.execute(
        text(
            "select k.id, coalesce(nullif(btrim(k.mfr_number), ''), nullif(btrim(si.mfr_number), ''), nullif(btrim(mi.mfr_number), ''), '') as m "
            "from product_kit_parts k left join supplier_items si on si.id = k.supplier_item_id "
            "left join manual_catalog_items mi on mi.id = k.manual_item_id "
            "where k.product_id = :p order by k.sort_order, k.id"
        ),
        {"p": product_id},
    ).all()
    return [(r.id, r.m) for r in rows]


def parts_mpn(db: Session, product_id: int) -> Optional[str]:
    """Every piece's manufacturer number once, in order, joined; None when no piece has one."""
    seen: set[str] = set()
    out: list[str] = []
    for _id, m in _line_numbers(db, product_id):
        n = _norm_mpn(m)
        if n and n not in seen:
            seen.add(n)
            out.append(m.strip())
    return MPN_JOIN.join(out) or None


def _top_mpn(db: Session, product_id: int) -> str:
    return (db.execute(text("select manufacturer_part_number from products where id = :p"), {"p": product_id}).scalar() or "").strip()


def place_existing_mpn(db: Session, product_id: int) -> str:
    """Move the number typed on the product onto a piece when no piece has it yet: a one-piece product's only piece, else
    the first piece that has no number. Returns 'placed', 'ok' (nothing to move), 'none' (nothing to place or no pieces)
    or 'ambiguous' (every piece already has a different number, so it was left where it is)."""
    top = _top_mpn(db, product_id)
    rows = _line_numbers(db, product_id)
    if not top or not rows:
        return "none"
    nt = _norm_mpn(top)
    if nt in [_norm_mpn(m) for _, m in rows] or nt == _norm_mpn(parts_mpn(db, product_id)):
        return "ok"
    if len(rows) == 1:
        target = rows[0][0]
    else:
        blanks = [i for i, m in rows if not _norm_mpn(m)]
        if not blanks:
            return "ambiguous"
        target = blanks[0]
    db.execute(text("update product_kit_parts set mfr_number = :m where id = :i"), {"m": top[:60], "i": target})
    return "placed"


def sync_product_mpn(db: Session, product_id: int) -> str:
    """Make the product's Manufacturer part # the list of its pieces' numbers (after moving a number typed on the product
    onto a piece). Never overwrites a number it could not place. Returns 'synced', 'ambiguous' or 'none'. No commit."""
    state = place_existing_mpn(db, product_id)
    if state == "ambiguous":
        return "ambiguous"
    m = parts_mpn(db, product_id)
    if not m:
        return "none"
    db.execute(
        text(
            "update products set manufacturer_part_number = :m, updated_at = now() "
            "where id = :p and manufacturer_part_number is distinct from :m"
        ),
        {"m": m, "p": product_id},
    )
    return "synced"


def mpn_locked(db: Session, product_id: int) -> bool:
    """True when the product's Manufacturer part # comes from its pieces (so the product screen shows it read-only):
    some piece has a number and the product's own number is blank or is already one of those / the list."""
    m = parts_mpn(db, product_id)
    if not m:
        return False
    top = _norm_mpn(_top_mpn(db, product_id))
    return not top or top == _norm_mpn(m) or top in [_norm_mpn(x) for _, x in _line_numbers(db, product_id)]


def backfill_mfr_numbers(db: Session) -> dict:
    """One-time (safe to repeat): move the numbers products already have onto their part lines. A one-piece product's own
    number goes onto its piece when the piece does not already have it; products with several pieces are handled one by
    one (see place_existing_mpn). Then a one-piece product with a blank number gets its piece's number. Commits."""
    res = {"one_piece_moved": 0, "multi_products": 0, "multi_placed": 0, "multi_synced": 0, "left_alone": 0, "blank_filled": 0}
    res["one_piece_moved"] = db.execute(
        text(
            """
            update product_kit_parts k set mfr_number = left(btrim(p.manufacturer_part_number), 60)
            from products p
            where p.id = k.product_id
              and nullif(btrim(p.manufacturer_part_number), '') is not null
              and (select count(*) from product_kit_parts x where x.product_id = p.id) = 1
              and regexp_replace(upper(p.manufacturer_part_number), '[^A-Z0-9]', '', 'g')
                  <> regexp_replace(upper(coalesce(nullif(btrim(k.mfr_number), ''),
                                                   (select si.mfr_number from supplier_items si where si.id = k.supplier_item_id), '')),
                                    '[^A-Z0-9]', '', 'g')
            """
        )
    ).rowcount
    db.commit()
    ids = [r.product_id for r in db.execute(text("select product_id from product_kit_parts group by product_id having count(*) > 1")).all()]
    res["multi_products"] = len(ids)
    for pid in ids:
        try:
            placed = place_existing_mpn(db, pid)
            if placed == "placed":
                res["multi_placed"] += 1
            if sync_product_mpn(db, pid) == "ambiguous":
                res["left_alone"] += 1
            else:
                res["multi_synced"] += 1
            db.commit()
        except Exception:
            db.rollback()
            log.exception("manufacturer number move failed for product %s", pid)
    res["blank_filled"] = db.execute(
        text(
            """
            update products p set manufacturer_part_number = x.m
            from (select k.product_id, coalesce(nullif(btrim(k.mfr_number), ''), nullif(btrim(si.mfr_number), '')) as m
                  from product_kit_parts k left join supplier_items si on si.id = k.supplier_item_id
                  where (select count(*) from product_kit_parts y where y.product_id = k.product_id) = 1) x
            where p.id = x.product_id and x.m is not null and nullif(btrim(p.manufacturer_part_number), '') is null
            """
        )
    ).rowcount
    db.commit()
    return res


def start_mfr_backfill_once() -> None:
    """Start-up: move existing numbers onto the part lines once (a minute after start, in the background), then remember
    that it was done so later start-ups skip it. POST /kits/mfr-numbers/backfill runs it again by hand."""
    from app.database import _session_factory

    def run() -> None:
        time.sleep(45)
        db = _session_factory()()
        try:
            if db.execute(text("select 1 from kit_mfr_backfill where id = 1")).first():
                return
            res = backfill_mfr_numbers(db)
            db.execute(text("insert into kit_mfr_backfill (id, summary) values (1, :s) on conflict (id) do nothing"), {"s": str(res)})
            db.commit()
            log.info("manufacturer numbers moved onto part lines: %s", res)
        except Exception:
            db.rollback()
            log.exception("moving manufacturer numbers onto part lines failed (it will be tried again at the next start-up)")
        finally:
            db.close()

    threading.Thread(target=run, name="kit-mfr-backfill", daemon=True).start()


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
    # the manufacturer's own part number for THIS piece. Blank on a catalog part = follow the catalog's number.
    mfr_number: Optional[str] = Field(None, max_length=60)
    # a part saved in the vendor's saved-parts list (vendors with no catalog): the piece follows it instead of holding a copy
    manual_item_id: Optional[int] = None
    # the brand of a typed piece (a catalog or saved part has its own)
    manufacturer: Optional[str] = Field(None, max_length=80)


LABEL_BY = "^(mfr|sku)$"  # what an Automatic Distributors part is written as in a NEW label: manufacturer number or vendor SKU


class PartsIn(BaseModel):
    parts: list[KitPartIn] = Field(default_factory=list, max_length=25)
    label_by: str = Field("mfr", pattern=LABEL_BY)


def availability_of(db: Session, product_id: int) -> Optional[dict]:
    """How many of this product the distributor stock can make: for each piece, floor(stock / pieces needed per product); the
    scarcest piece decides. A piece's stock is what its catalog item shows at the distributor's Indiana and Nevada warehouses
    (plus the single stock number of a vendor like Rocky Mountain), the same warehouses the availability rule counts
    (availability.py dist_sql). A piece that is not matched to a live catalog item counts as 0. None when there are no pieces.
    Read-only: nothing is stored or sent anywhere from this."""
    rows = db.execute(
        text(
            "select k.id, k.part_number, k.quantity, si.id as si_id, coalesce(si.active, false) as active, "
            "(coalesce(si.qty_indiana, 0) + coalesce(si.qty_nevada, 0) + coalesce(si.qty_other, 0)) as stock "
            "from product_kit_parts k left join supplier_items si on si.id = k.supplier_item_id "
            "where k.product_id = :p order by k.sort_order, k.id"
        ),
        {"p": product_id},
    ).all()
    if not rows:
        return None
    parts = []
    for r in rows:
        need = max(int(r.quantity or 1), 1)
        matched = r.si_id is not None and bool(r.active)
        stock = int(r.stock or 0) if matched else 0
        parts.append({"id": r.id, "part_number": r.part_number, "need": need, "matched": matched, "stock": stock, "can_make": stock // need})
    low = min(p["can_make"] for p in parts)
    for p in parts:
        p["limiting"] = p["can_make"] == low and len(parts) > 1
    # OUR OWN shelf: for each piece, what our single-part products that are that same distributor part hold (available, not set
    # aside), counted in pieces. This is what a kit's E&I warehouse quantity is made from (app/stock.py kit_shelf_sql).
    ours = {}
    if len(parts) > 1:
        from app.stock import same_part_sql, set_aside_sql

        for r in db.execute(
            text(
                "select k.id, coalesce(("
                "  select sum(greatest(0, coalesce(p2.quantity_on_hand, 0) - " + set_aside_sql("p2") + ") * greatest(coalesce(k2.quantity, 1), 1)) "
                "  from product_kit_parts k2 join products p2 on p2.id = k2.product_id "
                f"  where {same_part_sql('k', 'k2')} and p2.id <> :p "
                "    and (select count(*) from product_kit_parts k3 where k3.product_id = p2.id) = 1), 0) as units "
                "from product_kit_parts k where k.product_id = :p"
            ),
            {"p": product_id},
        ).all():
            ours[r.id] = int(r.units or 0)
    for p in parts:
        units = ours.get(p["id"], 0)
        p["on_our_shelf"] = units
        p["ours_can_make"] = units // p["need"]
    ours_low = min(p["ours_can_make"] for p in parts) if len(parts) > 1 else None
    for p in parts:
        p["ours_limiting"] = ours_low is not None and p["ours_can_make"] == ours_low
    return {"available": low, "is_kit": len(parts) > 1, "parts": parts, "ours_available": ours_low}


def _parts_of(db: Session, product_id: int, see_cost: bool = True) -> list[dict]:
    rows = db.execute(
        text(
            "select k.id, k.distributor_id, d.name as vendor, k.supplier_item_id, k.manual_item_id, k.part_number, "
            "coalesce(nullif(btrim(mi.description), ''), k.description) as description, "
            "coalesce(mi.unit_cost, k.unit_cost) as unit_cost, k.quantity, k.sort_order, "
            "coalesce(nullif(btrim(si.mfr_number), ''), k.part_number) as label_part, "
            "coalesce(nullif(btrim(k.mfr_number), ''), nullif(btrim(si.mfr_number), ''), nullif(btrim(mi.mfr_number), '')) as mfr_number, "
            # the brand of the piece: the catalog's, the saved part's or the typed one. Never the distributor's name.
            "coalesce(nullif(btrim(si.manufacturer), ''), nullif(btrim(mi.manufacturer), ''), nullif(btrim(k.manufacturer), '')) as manufacturer from product_kit_parts k "
            "left join supplier_items si on si.id = k.supplier_item_id "
            "left join manual_catalog_items mi on mi.id = k.manual_item_id "
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


def replace_parts(db: Session, product_id: int, parts: list[KitPartIn], label_by: str = "mfr") -> dict:
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
            sup = supplier_for(dists[p.distributor_id])
            if sup is None:
                raise HTTPException(400, f"{dists[p.distributor_id]} has no catalog here, so a piece cannot point at a catalog part. Type the part number instead.")
            it = db.execute(
                text("select sku, mfr_number, name, manufacturer, coalesce(effective_cost, dealer_cost) as dealer_cost from supplier_items where id = :i and supplier = :s"),
                {"i": p.supplier_item_id, "s": sup},
            ).first()
            if not it:
                raise HTTPException(400, "A piece points at a catalog part that is no longer in the catalog. Pick it again.")
            number = it.sku  # the distributor's own part number (Autodist SKU, Rocky Mountain part #) is what goes on the purchase order
            # the label uses the manufacturer number (default) or the vendor SKU, as chosen; no manufacturer number -> the SKU
            label = label_token_for(it.mfr_number or "") if label_by != "sku" else ""
            desc = desc or " ".join(x for x in (it.manufacturer, it.name) if x)[:250]
            cost = float(it.dealer_cost) if it.dealer_cost is not None else cost
        saved = None
        if p.manual_item_id and not p.supplier_item_id:
            saved = db.execute(
                text("select id, part_number, mfr_number, description, unit_cost from manual_catalog_items where id = :i and distributor_id = :d"),
                {"i": p.manual_item_id, "d": p.distributor_id},
            ).first()
            if not saved:
                raise HTTPException(400, "A piece points at a saved part that is no longer in this vendor's saved parts. Pick it again.")
            number = saved.part_number
            desc = (saved.description or "").strip() or desc
            cost = float(saved.unit_cost) if saved.unit_cost is not None else cost
        if not number:
            raise HTTPException(400, "Every piece needs a part number.")
        mfr = (p.mfr_number or "").strip() or None
        if mfr and p.supplier_item_id and _norm_mpn(mfr) == _norm_mpn(it.mfr_number or ""):
            mfr = None  # the same as the catalog's number: keep following the catalog
        if mfr and saved is not None and _norm_mpn(mfr) == _norm_mpn(saved.mfr_number or ""):
            mfr = None  # the same as the saved part's number: keep following the saved part
        clean.append({"distributor_id": p.distributor_id, "supplier_item_id": p.supplier_item_id, "part_number": number,
                      "label": label or number, "description": desc, "unit_cost": cost, "quantity": p.quantity, "sort_order": i,
                      "mfr_number": mfr, "manual_item_id": saved.id if saved is not None else None,
                      "manufacturer": ((p.manufacturer or "").strip() or None) if (not p.supplier_item_id and saved is None) else None})
    for c in clean:
        db.execute(
            text(
                "insert into product_kit_parts (product_id, distributor_id, supplier_item_id, part_number, description, unit_cost, quantity, sort_order, mfr_number, manual_item_id, manufacturer) "
                "values (:p, :d, :si, :pn, :desc, :uc, :q, :so, :mfr, :mi, :brand)"
            ),
            {"p": product_id, "d": c["distributor_id"], "si": c["supplier_item_id"], "pn": c["part_number"],
             "desc": c["description"], "uc": c["unit_cost"], "q": c["quantity"], "so": c["sort_order"], "mfr": c["mfr_number"],
             "mi": c["manual_item_id"], "brand": c["manufacturer"]},
        )
    # Remember typed pieces of distributors with no catalog, so the next product can pick them instead of typing them again.
    # A part already saved keeps its details (only blanks are filled), and a failure here never stops the parts from saving.
    for c in clean:
        if c["supplier_item_id"] or c["manual_item_id"] or catalog_loaded(db, dists[c["distributor_id"]]):
            continue
        try:
            with db.begin_nested():
                db.execute(
                    text(
                        "insert into manual_catalog_items (distributor_id, part_number, part_norm, mfr_number, manufacturer, description, unit_cost) "
                        "values (:d, :pn, :pnorm, :mfr, :brand, :desc, :uc) "
                        "on conflict (distributor_id, part_norm) do update set "
                        "mfr_number = coalesce(nullif(btrim(manual_catalog_items.mfr_number), ''), excluded.mfr_number), "
                        "manufacturer = coalesce(nullif(btrim(manual_catalog_items.manufacturer), ''), excluded.manufacturer), "
                        "description = coalesce(nullif(btrim(manual_catalog_items.description), ''), excluded.description), "
                        "unit_cost = coalesce(manual_catalog_items.unit_cost, excluded.unit_cost), updated_at = now()"
                    ),
                    {"d": c["distributor_id"], "pn": c["part_number"], "pnorm": _norm(c["part_number"]), "mfr": c["mfr_number"],
                     "brand": c["manufacturer"], "desc": c["description"], "uc": c["unit_cost"]},
                )
        except Exception:
            log.exception("could not remember a typed distributor part")
    # The product's Manufacturer part # follows its pieces. What was just saved on the pieces wins: the number still stored on the
    # product is the OLD one, and moving it back onto a piece (sync_product_mpn -> place_existing_mpn) is what undid every change
    # to a piece's Manufacturer # (a one-piece product always got its old number put back). That move is only for pieces that
    # have no number at all, to carry over a number that was typed on the product before it had pieces.
    follow = parts_mpn(db, product_id)
    if follow:
        db.execute(
            text(
                "update products set manufacturer_part_number = :m, updated_at = now() "
                "where id = :p and manufacturer_part_number is distinct from :m"
            ),
            {"m": follow, "p": product_id},
        )
    else:
        sync_product_mpn(db, product_id)
    # The product's Manufacturer follows its pieces too (the product screen shows it read only): when every piece that has a
    # brand has the same one, that brand is the product's Manufacturer. Different brands leave the stored one alone.
    try:
        brands: dict[str, str] = {}
        for pc in _parts_of(db, product_id, see_cost=False):
            b = (pc.get("manufacturer") or "").strip()
            if b:
                brands.setdefault(b.lower(), b)
        if len(brands) == 1:
            from app.manufacturers import find_or_create

            mid = find_or_create(db, next(iter(brands.values())))
            if mid:
                db.execute(
                    text("update products set part_manufacturer_id = :m, updated_at = now() where id = :p and part_manufacturer_id is distinct from :m"),
                    {"m": mid, "p": product_id},
                )
    except Exception:
        log.exception("could not set the product's manufacturer from its parts")
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
            f"select cl.id, cl.store, {_DIGITS} as item_id, cl.status, cl.cls_pushed, cl.cls_pushed_at, "
            f"       coalesce(nullif(btrim(lr.custom_label_sku), ''), nullif(btrim(la.custom_label_sku), '')) as ebay_label "
            f"from channel_listings cl "
            f"left join ebay_report_rows lr on lr.item_id = {_DIGITS} "
            f"left join ebay_api_details la on la.item_id = {_DIGITS} "
            f"where cl.product_id = :p order by cl.store, cl.id"
        ),
        {"p": product_id},
    ).all()
    return [dict(r._mapping) for r in rows if r.item_id]


def _product_cls(db: Session, product_id: int) -> Optional[str]:
    v = db.execute(text("select custom_label_sku from products where id = :p"), {"p": product_id}).scalar()
    return (v or "").strip() or None


@router.post("/mfr-numbers/backfill")
@needs("products.edit_details")
def run_mfr_backfill(db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    """Move the manufacturer numbers products already have onto their part lines (done once automatically at start-up;
    this runs it again, e.g. after importing more products). Owner / admin only."""
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Only an owner or admin can do this.")
    return backfill_mfr_numbers(db)


@router.get("/{product_id}")
@needs("products.view", "products.edit_details", "products.create")
def get_kit(product_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    if not db.execute(text("select 1 from products where id = :p"), {"p": product_id}).first():
        raise HTTPException(404, "Product not found")
    cls = _product_cls(db, product_id)
    listings = _linked_listings(db, product_id)
    for l in listings:
        # in sync when we pushed this label, or the label eBay already shows (last report / eBay lookup) is it
        l["in_sync"] = bool(cls) and ((l["cls_pushed"] or "") == cls or (l.get("ebay_label") or "") == cls)
    return {
        "cls": cls,
        "parts": _parts_of(db, product_id, see_cost=has_perm(user, "products.edit_price")),
        "listings": listings,
        "can_sync": user.role in ("owner", "admin"),
        "mpn": _top_mpn(db, product_id) or None,
        "mpn_locked": mpn_locked(db, product_id),
        "availability": availability_of(db, product_id),
    }


@router.put("/{product_id}/parts")
@needs("products.edit_details")
def put_parts(product_id: int, body: PartsIn, db: Session = Depends(get_db)):
    if not db.execute(text("select 1 from products where id = :p"), {"p": product_id}).first():
        raise HTTPException(404, "Product not found")
    res = replace_parts(db, product_id, body.parts, body.label_by)
    db.commit()
    listings = _linked_listings(db, product_id)
    return {
        "cls": res["cls"],
        "cls_changed": res["cls_changed"],
        "duplicates": res["duplicates"],
        # only a label that was just created needs sending; an unchanged one is already on the listings
        "listings_to_update": len(listings) if res["cls_changed"] else 0,
        "mpn": _top_mpn(db, product_id) or None,
        "mpn_locked": mpn_locked(db, product_id),
        "availability": availability_of(db, product_id),
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
            # The Live listings page shows the label from saved copies of what eBay reported (report upload / lookups);
            # nothing refreshed them after a push. eBay now has this label (just sent, or it already did), so update the copies.
            digits = re.sub(r"[^0-9]", "", str(l["item_id"] or ""))
            if digits:
                try:
                    with db.begin_nested():
                        for tbl in ("ebay_report_rows", "ebay_api_details"):
                            db.execute(text(f"update {tbl} set custom_label_sku = :c where item_id = :i"), {"c": cls, "i": digits})
                except Exception:
                    log.warning("could not refresh the saved label copy for listing %s", l["item_id"], exc_info=True)
            db.execute(
                text("update channel_listings set cls_pushed = :c, cls_pushed_at = now() where id = :i"), {"c": cls, "i": l["id"]}
            )
            db.commit()
        except HTTPException as e:
            db.rollback()
            res["status"], res["detail"] = "error", str(e.detail)
            if "eBay 17" in str(e.detail):
                res["detail"] += (
                    f" Item {l['item_id']} was looked up in store '{l['store'] or '(none saved)'}'. eBay 17 means that item is ended/deleted, "
                    "or it belongs to a different eBay account than that store: check the listing's store on the Live listings tab, "
                    "or unlink the listing from this product if it is gone."
                )
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
    # Seller Hub > Listings > Inventory keeps its own custom label per product; bring those records along too
    try:
        from app.ebay_sm_label import sync_inventory  # imported here: it pulls in the eBay stack

        inventory = sync_inventory(db, user, product_id, True)
    except HTTPException as e:
        inventory = [{"store": "", "sm_product_id": "", "name": "", "before": "", "after": "", "status": "error", "detail": str(e.detail)}]
    except Exception as e:
        db.rollback()
        log.exception("inventory label sync failed for product %s", product_id)
        # name the real error (class and first line) so the screen shows what to fix, not just "unexpected"
        why = f"{type(e).__name__}: {str(e).strip().splitlines()[0][:240]}" if str(e).strip() else type(e).__name__
        inventory = [{"store": "", "sm_product_id": "", "name": "", "before": "", "after": "", "status": "error", "detail": f"The eBay inventory records could not be checked ({why})."}]
    return {"cls": _product_cls(db, product_id), "results": results, "inventory": inventory}


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
                "select k.id, k.distributor_id, k.part_number, coalesce(nullif(btrim(mi.description), ''), k.description) as description, k.unit_cost, k.quantity, k.supplier_item_id, "
                "       coalesce(si.effective_cost, si.dealer_cost, mi.unit_cost) as live_cost, si.name as live_name, d.name as vendor "
                "from product_kit_parts k left join supplier_items si on si.id = k.supplier_item_id "
                "left join manual_catalog_items mi on mi.id = k.manual_item_id "
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
