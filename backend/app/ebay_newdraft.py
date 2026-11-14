"""
New eBay listing from a product that is not on eBay yet.

Mounted at /admin/ebay-draft/* (owner/admin only). It only BUILDS the draft; the draft is then edited, checked with eBay
and listed by the existing "Sell similar" routes in ebay_copy.py (/admin/ebay-copy/{id}, /preview, /list), so those
safety steps (VerifyAddFixedPriceItem, the live-push switch, the fingerprint, the push log, linking the new item number
to the product) are the same for both.

  GET  /products?q=        find a product to start from
  GET  /check/{product_id} which eBay listings the product already has (then use Sell similar instead), and open drafts
  GET  /stores             the connected stores, to pick where to list
  POST /create             {product_id}  save a DRAFT on the website only. Refused when the product already has an eBay
                           listing: the answer lists them so the Sell similar route can be used.
  POST /{id}/store         {store, model_item_id?}  choose the store. The category, shipping and return rules (business policies
                           when the store uses them, otherwise the shipping services and return terms written on the older
                           listings), location, handling time and store categories are taken from ONE live listing in that store that
                           is as close as possible to this part: a listing of a product in the same website category,
                           then a neighbouring category, then the newest. "model_item_id" picks that listing by hand.

What the draft is filled with, from the product: title (cut to 80), description, price, custom label, the non-hidden photos
in their saved order (public image URLs), the item specifics saved on the product (plus Brand and Manufacturer Part Number),
condition, and the compatibility table.

Compatibility: eBay's vehicle lists are their own, so our year / make / model fitment is converted into THEIR table, using the
eBay compatibility tables the compat import saved (ebay_compat_items). For each of our models the eBay name is found through the
same decisions table the compat-to-fitment conversion used (fitment_ebay_model_map, reversed), then by our own name. A
make / model / year is only put in the draft when that exact combination already exists in an eBay table we saved, so eBay's
own vocabulary is used; anything else is left out and listed in the draft's notes. eBay's check at preview time is the final word.

Nothing is sent to eBay except reads (GetItem on the model listing, twice) until the person lists the draft.
"""
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.admin_cleanup import _brand_names
from app.admin_ebay_fitment import _LIST
from app.auth import AuthUser, require_admin
from app.database import get_db
from app.ebay_copy import NO_TABLE, _extras, _has_profiles, _has_terms, _row, _terms_problem
from app.ebay_edit import ITEM_RE, _money, _read_item
from app.ebay_oauth import _store_row
from app.images import full_url

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-draft", tags=["admin-ebay-draft"], dependencies=[Depends(require_admin)])

_DIG = "regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g')"
MAX_PHOTOS = 24
MAX_MODELS = 40          # models looked up per draft (each is one query over the saved eBay tables)
MAX_COMPAT_ROWS = 3000   # eBay's limit for one listing's compatibility table
MODEL_CANDIDATES = 5     # live listings tried, best match first, until one gives a category and all three policies

CONDITION_IDS = [  # product "condition" text -> eBay ConditionID (first match wins)
    ("for parts", "7000"), ("parts", "7000"), ("remanufactured", "2500"), ("refurbished", "2500"),
    ("new other", "1500"), ("open box", "1500"), ("new", "1000"), ("used", "3000"),
]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def fit_title(t: str, limit: int = 80) -> str:
    t = " ".join((t or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    return (cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else cut).rstrip(" -,/")


def condition_id(text_value: Optional[str]) -> str:
    v = (text_value or "").strip().lower()
    if not v:
        return ""
    for key, cid in CONDITION_IDS:
        if v == key or v.startswith(key + " ") or v.endswith(" " + key):
            return cid
    return ""


def _years_text(years: list[int]) -> str:
    ys = sorted(set(years))
    out, i = [], 0
    while i < len(ys):
        j = i
        while j + 1 < len(ys) and ys[j + 1] == ys[j] + 1:
            j += 1
        out.append(str(ys[i]) if i == j else f"{ys[i]}-{ys[j]}")
        i = j + 1
    return ", ".join(out)


# --------------------------------------------------------------------------
# our fitment -> eBay's compatibility table
# --------------------------------------------------------------------------
def _fitment_rows(db: Session, product_id: int):
    return db.execute(
        text(
            """
            select vmo.id as model_id, vmk.name as make, vmo.name as model, v.year as year,
                   nullif(btrim(cast(v.submodel as text)), '') as sub
            from product_fitment pf
            join vehicles v on v.id = pf.vehicle_id
            join vehicle_models vmo on vmo.id = v.model_id
            join vehicle_makes vmk on vmk.id = vmo.make_id
            where pf.product_id = :p
            """
        ),
        {"p": product_id},
    ).all()


def _ebay_names(db: Session, model_id: int, make: str, model: str) -> list[tuple[str, str]]:
    """The eBay spellings to try for one of our models: the ones the compat conversion mapped to it, then our own."""
    out: list[tuple[str, str]] = []
    try:
        with db.begin_nested():
            rows = db.execute(
                text("select make_name, model_name from fitment_ebay_model_map "
                     "where model_id = :m and not coalesce(skip, false) "
                     "and coalesce(btrim(make_name), '') <> '' and coalesce(btrim(model_name), '') <> ''"),
                {"m": model_id},
            ).all()
        out += [(r.make_name.strip(), r.model_name.strip()) for r in rows]
    except Exception:
        log.warning("fitment_ebay_model_map could not be read; using our own names only")
    out.append((make.strip(), model.strip()))
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


_L = _LIST.format(c="c")
_VOCAB = text(
    f"""
    select distinct x.e -> 'attrs' as attrs
    from ebay_compat_items c
    cross join lateral jsonb_array_elements(
        case when jsonb_typeof({_L}) = 'array' then {_L} else cast('[]' as jsonb) end) as x(e)
    where c.status = 'ok'
      and {_L} @> cast(:needle as jsonb)
      and btrim(x.e -> 'attrs' ->> 'Make') = :mk
      and btrim(x.e -> 'attrs' ->> 'Model') = :mo
    """
)


def _vocab(db: Session, make: str, model: str) -> list[dict]:
    """Every attribute set eBay's own tables (saved by the compat import) hold for this exact make + model."""
    needle = json.dumps([{"attrs": {"Make": make, "Model": model}}])
    rows = db.execute(_VOCAB, {"needle": needle, "mk": make, "mo": model}).all()
    return [dict(r.attrs) for r in rows if isinstance(r.attrs, dict)]


_PLAIN = {"Make", "Model", "Year"}
_ORDER = ["Make", "Model", "Submodel", "Trim", "Engine"]


def _pairs(attrs: dict) -> list[list[str]]:
    """[[name, value], ...] the way the draft and the AddFixedPriceItem request keep it: Make, Model, other, Year last."""
    keys = [k for k in _ORDER if k in attrs] + sorted(k for k in attrs if k not in _ORDER and k != "Year") + (["Year"] if "Year" in attrs else [])
    return [[k, str(attrs[k]).strip()] for k in keys if str(attrs[k]).strip()]


def convert_fitment(db: Session, product_id: int, rows=None, names=_ebay_names, vocab=_vocab) -> tuple[list[dict], list[str]]:
    """Our fitment as eBay compatibility rows, plus plain-language notes about what could not be converted."""
    rows = _fitment_rows(db, product_id) if rows is None else rows
    groups: dict[tuple, dict] = {}
    no_year = 0
    for r in rows:
        if r.year is None:
            no_year += 1
            continue
        g = groups.setdefault((r.model_id, r.make, r.model), {"years": set(), "subs": {}})
        y = int(r.year)
        g["years"].add(y)
        g["subs"].setdefault(y, set())
        if r.sub:
            g["subs"][y].add(r.sub.strip().lower())
    out: list[dict] = []
    seen: set[tuple] = set()
    notes: list[str] = []
    if no_year:
        notes.append(f"{no_year} fitment {'entry has' if no_year == 1 else 'entries have'} no year, and eBay needs one, so {'it was' if no_year == 1 else 'they were'} left out.")
    for n, ((mid, make, model), g) in enumerate(sorted(groups.items(), key=lambda kv: (str(kv[0][1]), str(kv[0][2])))):
        if n >= MAX_MODELS:
            notes.append(f"Only the first {MAX_MODELS} models were converted here; choosing the store redoes the table with eBay's own list and no model limit.")
            break
        found = None
        for mk, mo in names(db, mid, make, model):
            v = vocab(db, mk, mo)
            if v:
                found = v
                break
        if not found:
            notes.append(f"{make} {model}: eBay has no name for this model in the saved compatibility tables, so it was left out.")
            continue
        by_year: dict[str, list[dict]] = {}
        for a in found:
            by_year.setdefault(str(a.get("Year", "")).strip(), []).append(a)
        missing: list[int] = []
        for y in sorted(g["years"]):
            opts = by_year.get(str(y))
            if not opts:
                missing.append(y)
                continue
            want = g["subs"].get(y) or set()
            if want:
                pick = [a for a in opts if str(a.get("Submodel") or a.get("Trim") or "").strip().lower() in want]
                if not pick:
                    missing.append(y)
                    continue
            else:  # model and year only: the plain row if eBay has one, otherwise every variant eBay lists for that year
                pick = [a for a in opts if set(a) <= _PLAIN] or opts
            for a in pick:
                p = _pairs(a)
                key = tuple(map(tuple, p))
                if key in seen:
                    continue
                seen.add(key)
                out.append({"pairs": p, "notes": ""})
        if missing:
            notes.append(f"{make} {model}: {_years_text(missing)} {'is' if len(missing) == 1 else 'are'} not in eBay's list, so left out.")
    if len(out) > MAX_COMPAT_ROWS:
        notes.append(f"eBay takes at most {MAX_COMPAT_ROWS} compatibility rows; {len(out) - MAX_COMPAT_ROWS} were cut.")
        out = out[:MAX_COMPAT_ROWS]
    return out, notes


# --------------------------------------------------------------------------
# finding a product, and what it already has on eBay
# --------------------------------------------------------------------------
@router.get("/products")
def find_products(q: str = "", db: Session = Depends(get_db)):
    q = q.strip()
    if len(q) < 2:
        return {"products": []}
    rows = db.execute(
        text(
            f"""
            select p.id, p.title, p.sku_display as sku, p.custom_label_sku, p.list_price,
                   (select count(*) from channel_listings cl where cl.product_id = p.id and {_DIG} <> '') as listings
            from products p
            where p.title ilike :l or p.sku_display ilike :l or p.custom_label_sku ilike :l
               or p.manufacturer_part_number ilike :l or cast(p.id as text) = :q
            order by (cast(p.id as text) = :q) desc, p.id desc
            limit 20
            """
        ),
        {"l": f"%{q}%", "q": q},
    ).all()
    return {"products": [
        {"id": r.id, "title": r.title, "sku": r.sku, "custom_label": r.custom_label_sku,
         "price": float(r.list_price) if r.list_price is not None else None, "listings": int(r.listings or 0)}
        for r in rows]}


def _listings_of(db: Session, product_id: int) -> list[dict]:
    rows = db.execute(
        text(
            f"""
            select cl.store, cl.status, {_DIG} as item_id,
                   coalesce((select r.title from ebay_report_rows r where r.item_id = {_DIG} limit 1),
                            (select a.title from ebay_api_details a where a.item_id = {_DIG} limit 1),
                            p.title) as title
            from channel_listings cl join products p on p.id = cl.product_id
            where cl.product_id = :p and {_DIG} <> ''
            order by lower(coalesce(cl.status, '')) = 'active' desc, cl.id desc
            """
        ),
        {"p": product_id},
    ).all()
    return [{"store": r.store, "status": r.status, "item_id": r.item_id, "title": r.title} for r in rows]


@router.get("/check/{product_id}")
def check(product_id: int, db: Session = Depends(get_db)):
    p = db.execute(text("select id, title, sku_display, custom_label_sku, list_price from products where id = :p"), {"p": product_id}).first()
    if not p:
        raise HTTPException(404, "That product does not exist.")
    drafts = []
    try:
        drafts = [{"id": r.id, "store": r.store, "title": r.title or ""} for r in db.execute(
            text("select id, store, data->>'title' as title from ebay_listing_copies where product_id = :p and status = 'draft' order by updated_at desc"),
            {"p": product_id}).all()]
    except Exception:
        db.rollback()
    return {
        "product": {"id": p.id, "title": p.title, "sku": p.sku_display, "custom_label": p.custom_label_sku,
                    "price": float(p.list_price) if p.list_price is not None else None},
        "listings": _listings_of(db, product_id),
        "drafts": drafts,
    }


@router.get("/stores")
def stores(db: Session = Depends(get_db)):
    rows = db.execute(text("select store, seller_username, live_pushes_enabled from ebay_accounts where refresh_token_enc is not null order by store")).all()
    return {"stores": [{"store": r.store, "seller": r.seller_username, "live": bool(r.live_pushes_enabled)} for r in rows]}


# --------------------------------------------------------------------------
# the draft
# --------------------------------------------------------------------------
class CreateIn(BaseModel):
    product_id: int


def _photos_of(db: Session, product_id: int) -> list[str]:
    try:
        rows = db.execute(
            text("select image_key, source_url from product_photos where product_id = :p and not coalesce(hidden, false) "
                 "order by is_primary desc, sort_order, id"),
            {"p": product_id},
        ).all()
    except Exception:
        db.rollback()
        rows = db.execute(
            text("select image_key, source_url from product_photos where product_id = :p order by is_primary desc, sort_order, id"),
            {"p": product_id},
        ).all()
    urls: list[str] = []
    for r in rows:
        u = full_url(r.image_key) or r.source_url
        if u and u.startswith("https://") and u not in urls:
            urls.append(u)
    return urls[:MAX_PHOTOS]


def _brand_for(db: Session, product_id: int) -> str:
    """The product's brand: its saved brand, else the manufacturer of the distributor catalog part it was made from
    (products made from the Autodist catalog have no saved brand, and eBay refuses a listing without the Brand specific)."""
    brand = ""
    try:
        pmid = db.execute(text("select part_manufacturer_id from products where id = :p"), {"p": product_id}).scalar()
    except Exception:
        db.rollback()
        pmid = None
    if pmid is not None:
        brand = (_brand_names(db, [pmid]).get(pmid) or "").strip()
    if not brand:
        try:
            raw = db.execute(
                text("select m from ("
                     " select s.manufacturer as m, 1 as o from supplier_product_links l join supplier_items s on s.supplier = l.supplier and s.sku = l.supplier_sku "
                     "  where l.product_id = :p"
                     " union all"
                     " select s.manufacturer, 2 from product_kit_parts k join supplier_items s on s.id = k.supplier_item_id where k.product_id = :p"
                     ") x where coalesce(btrim(m), '') <> '' order by o limit 1"),
                {"p": product_id},
            ).scalar()
            if raw:
                try:
                    from app.autodist import _nice_case
                    brand = _nice_case(raw).strip()
                except Exception:
                    brand = str(raw).strip()
        except Exception:
            db.rollback()
    return brand


def _remember_brand(db: Session, product_id: int, brand: str) -> None:
    """A product with no Manufacturer gets the brand found from the catalog, so the product screen shows it from now on."""
    if not brand:
        return
    try:
        has = db.execute(text("select part_manufacturer_id from products where id = :p"), {"p": product_id}).scalar()
        if has is not None:
            return
        from app.manufacturers import find_or_create
        mid = find_or_create(db, brand)
        if mid is not None:
            db.execute(text("update products set part_manufacturer_id = :m where id = :p and part_manufacturer_id is null"), {"m": mid, "p": product_id})
    except Exception:
        db.rollback()
        log.exception("could not save the brand on product %s", product_id)


def _specifics_of(db: Session, product_id: int, brand: str, mpn: str) -> list[dict]:
    specs: list[dict] = []
    try:
        rows = db.execute(text("select name, value from product_item_specifics where product_id = :p order by sort_order, id"), {"p": product_id}).all()
    except Exception:
        db.rollback()
        rows = []
    for r in rows:
        n, v = " ".join(str(r.name or "").split()), " ".join(str(r.value or "").split())
        if not n or not v or len(n) > 65 or len(v) > 65:
            continue
        for s in specs:
            if s["name"].lower() == n.lower():
                if v not in s["values"]:
                    s["values"].append(v)
                break
        else:
            specs.append({"name": n, "values": [v]})
    have = {s["name"].lower() for s in specs}
    if brand and "brand" not in have and len(brand) <= 65:
        specs.insert(0, {"name": "Brand", "values": [brand]})   # Brand is always the first row
    if mpn and "manufacturer part number" not in have and len(mpn) <= 65:
        specs.append({"name": "Manufacturer Part Number", "values": [mpn]})
    return specs[:100]


@router.post("/create")
def create(body: CreateIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    j = db.execute(text("select to_jsonb(p) as j from products p where p.id = :p"), {"p": body.product_id}).scalar()
    if not j:
        raise HTTPException(404, "That product does not exist.")
    existing = _listings_of(db, body.product_id)
    if existing:
        where = "; ".join(f"{e['store']} item {e['item_id']} ({e['status'] or 'status unknown'})" for e in existing[:8])
        raise HTTPException(
            409,
            f"This product already has {len(existing)} eBay listing{'s' if len(existing) != 1 else ''}: {where}. "
            "Use Sell similar from one of those, so the new listing copies it.",
        )
    title = fit_title(j.get("title") or "")
    if not title:
        raise HTTPException(400, "The product has no title.")
    photos = _photos_of(db, body.product_id)
    if not photos:
        raise HTTPException(409, "The product has no photos yet. eBay needs at least one. Add one on the product first.")

    brand = _brand_for(db, body.product_id)
    _remember_brand(db, body.product_id, brand)
    mpn = (j.get("manufacturer_part_number") or "").strip()
    description = (j.get("description") or "").strip() or f"<p>{title}</p>"
    price = ""
    if j.get("list_price") is not None:
        try:
            price = str(_money(str(j["list_price"])))
        except HTTPException:
            price = ""

    compat, compat_notes = [], []
    if not j.get("is_universal") and not j.get("fitment_exempt"):
        try:
            compat, compat_notes = convert_fitment(db, body.product_id)
        except Exception:
            db.rollback()
            log.exception("could not convert the fitment of product %s", body.product_id)
            compat_notes = ["The fitment could not be converted (see the server log). The draft has no compatibility table."]
    else:
        compat_notes = ["This part is marked universal or exempt from fitment, so no compatibility table was made."]

    data = {
        "title": title, "description": description, "price": price,
        "sku": (j.get("custom_label_sku") or "").strip(),
        "photos": photos, "original_photos": photos,
        "item_specifics": _specifics_of(db, body.product_id, brand, mpn),
        "currency": "USD", "category_id": "", "category_name": "",
        "condition_id": condition_id(j.get("condition")), "condition_note": "",
        "compat": compat, "compat_notes": compat_notes,
        "brand": brand, "mpn": mpn,
        # drop ship is on by default for a product that has a distributor part: the quantity then counts their warehouses
        "dropship": bool(db.execute(text("select exists (select 1 from product_kit_parts k where k.product_id = :p and k.supplier_item_id is not null)"), {"p": body.product_id}).scalar()),
        "product_category_id": j.get("category_id"),
    }
    try:
        new_id = db.execute(
            text("insert into ebay_listing_copies (store, source_item_id, product_id, status, data, created_by) "
                 "values ('', '', :p, 'draft', cast(:d as jsonb), :u) returning id"),
            {"p": body.product_id, "d": json.dumps(data), "u": user.email or user.user_id},
        ).scalar()
        db.commit()
    except Exception as e:
        db.rollback()
        log.exception("could not save the new draft")
        raise HTTPException(503, f"The draft could not be saved ({str(e.__cause__ or e)[:160]}). {NO_TABLE}")
    warnings = []
    if not price:
        warnings.append("The product has no price. Enter one on the draft.")
    if not data["condition_id"]:
        warnings.append("The product's condition did not match an eBay condition; it is taken from the model listing when you choose a store.")
    return {"id": new_id, "photos": len(photos), "compat_rows": len(compat), "notes": compat_notes, "warnings": warnings}


# --------------------------------------------------------------------------
# choosing the store: category, policies and the rest come from a similar live listing there
# --------------------------------------------------------------------------
class StoreIn(BaseModel):
    store: str
    model_item_id: Optional[str] = None   # an eBay item number from ANY of our stores, used for the category


def _model_candidates(db: Session, store: str, product_id: int, category_id) -> list[str]:
    rows = db.execute(
        text(
            f"""
            select {_DIG} as item_id
            from channel_listings cl join products p2 on p2.id = cl.product_id
            where lower(coalesce(cl.store, '')) = lower(:st) and lower(coalesce(cl.status, '')) = 'active'
              and {_DIG} <> '' and cl.product_id <> :p
            order by
              (cast(:cat as bigint) is not null and p2.category_id = cast(:cat as bigint)) desc,
              (cast(:cat as bigint) is not null and p2.category_id in (
                  select c2.id from categories c2
                  where c2.parent_id is not distinct from (select c3.parent_id from categories c3 where c3.id = cast(:cat as bigint)))) desc,
              cl.id desc
            limit {MODEL_CANDIDATES}
            """
        ),
        {"st": store, "p": product_id, "cat": category_id},
    ).all()
    return [r.item_id for r in rows]


_COPIED = ("category_id", "category_name", "country", "location", "postal_code", "dispatch_days",
           "ship_profile", "pay_profile", "return_profile", "category2_id", "store_cat1", "store_cat2", "currency",
           "shipping", "returns", "payment_methods", "ship_to")


@router.post("/{copy_id}/store")
def choose_store(copy_id: int, body: StoreIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    r = _row(db, copy_id)
    if r.status != "draft":
        raise HTTPException(409, "This draft is already listed.")
    if r.source_item_id:
        raise HTTPException(409, "This draft is a copy of a live listing, so its store is fixed. Start a new draft from the product to use another store.")
    row = _store_row(db, body.store)
    d = dict(r.data or {})

    # The item typed in only supplies the CATEGORY, and may belong to any of our stores. Shipping, returns and policies always come
    # from a live listing in the store chosen here.
    cat_item = None
    if body.model_item_id and body.model_item_id.strip():
        mid = body.model_item_id.strip()
        if not ITEM_RE.fullmatch(mid):
            raise HTTPException(400, "That does not look like an eBay item number.")
        cat_item = mid
    cands = _model_candidates(db, row.store, r.product_id, d.get("product_category_id"))
    cat_src = cat_ex = cat_store = None
    if cat_item:
        known = db.execute(
            text(f"select cl.store from channel_listings cl where {_DIG} = :i order by cl.id desc limit 1"), {"i": cat_item}).scalar()
        others = [x.store for x in db.execute(text("select store from ebay_accounts where refresh_token_enc is not null order by store")).all()]
        order = [row.store] + [o for o in others if o.lower() != row.store.lower()]
        if known:
            order = [known] + [o for o in order if o.lower() != known.lower()]
        why = ""
        for st in order:
            try:
                cat_src = _read_item(db, st, cat_item)
                cat_ex = _extras(db, st, cat_item)
                cat_store = st
                break
            except HTTPException as e:
                why = f"{st}: {e.detail}"
                continue
        if not cat_src or not cat_ex or not cat_ex.get("category_id"):
            raise HTTPException(409, f"Item {cat_item} could not be read from any connected store ({why or 'it has no category'}).")
        if cat_store.lower() == row.store.lower():
            cands = [cat_item]   # same store: the item also supplies the shipping and policy settings, as before
    if not cands:
        raise HTTPException(409, f"{row.store} has no active listing to take the shipping settings from. Type an item number from {row.store} to use as the model.")

    chosen, last = None, ""
    for item in cands:
        try:
            src = _read_item(db, row.store, item)
            if src["has_variations"] or src["listing_type"] not in ("FixedPriceItem", "StoresFixedPrice"):
                last = f"{item} has variations or is not fixed price"
                continue
            ex = _extras(db, row.store, item)
        except HTTPException as e:
            last = f"{item}: {e.detail}"
            continue
        if not ex["category_id"]:
            last = f"{item} has no category"
            continue
        if _terms_problem(ex):
            last = f"{item}: calculated shipping"
            continue
        if not _has_profiles(ex) and not _has_terms(ex):
            last = f"{item} has no shipping services and return rules (and no business policies)"
            continue
        chosen = (item, src, ex)
        break
    if not chosen:
        raise HTTPException(409, f"None of the listings tried could be used as a model ({last}). Type an eBay item number from {row.store} to use instead.")

    item, src, ex = chosen
    for k in _COPIED:  # a value the model does not have clears the old one, so profiles and older terms never mix
        d[k] = ex.get(k) or ({} if k in ("shipping", "returns") else [] if k in ("payment_methods", "ship_to") else "")
    d["best_offer"] = bool(ex.get("best_offer"))
    d["best_offer_accept"] = d["best_offer_decline"] = ""  # prices differ per part; never copied
    if not d.get("condition_id") and ex.get("condition_id"):
        d["condition_id"] = ex["condition_id"]
    d["model_item_id"] = item
    if not d.get("brand"):   # drafts made before the brand fallback existed
        d["brand"] = _brand_for(db, r.product_id)
        _remember_brand(db, r.product_id, d["brand"])
    specs_now = list(d.get("item_specifics") or [])
    if d.get("brand") and not any(str(x.get("name", "")).lower() == "brand" for x in specs_now) and len(d["brand"]) <= 65:
        specs_now.insert(0, {"name": "Brand", "values": [d["brand"]]})   # Brand is always the first row
        d["item_specifics"] = specs_now
    d["site"] = ex.get("site") or ""
    if cat_item and cat_store and cat_store.lower() != row.store.lower():
        d["category_id"] = cat_ex.get("category_id") or d.get("category_id", "")
        d["category_name"] = cat_ex.get("category_name") or ""
        d["category2_id"] = ""
        d["site"] = cat_ex.get("site") or d.get("site", "")   # the site belongs to the category
        d["category_item_id"] = cat_item

    compat_note = ""
    if not d.get("compat_edited") and d.get("category_id"):
        try:
            from app.ebay_vocab import convert_with_ebay
            rows, notes = convert_with_ebay(db, r.product_id, d["category_id"])
            d["compat"], d["compat_notes"] = rows, notes
            compat_note = f"{len(rows)} compatible vehicle rows were made from our fitment using eBay's own list for this category."
        except Exception as e:
            db.rollback()
            compat_note = f"eBay's vehicle list could not be read ({getattr(e, 'detail', None) or getattr(e, 'message', None) or e}); the table made from the saved eBay tables is kept."

    have = {s["name"].lower() for s in d.get("item_specifics", [])}
    hint_src = cat_src if (cat_item and cat_store and cat_store.lower() != row.store.lower()) else src
    hints = [{"name": s["name"], "value": "; ".join(s["values"])} for s in hint_src["item_specifics"]
             if s.get("name") and s.get("values") and s["name"].lower() not in have]
    db.execute(
        text("update ebay_listing_copies set store = :s, data = cast(:d as jsonb), updated_at = now() where id = :i"),
        {"s": row.store, "d": json.dumps(d), "i": copy_id},
    )
    db.commit()
    return {
        "store": row.store, "model_item_id": item, "category_id": d.get("category_id", ""), "category_name": d.get("category_name", ""),
        "hint_specifics": hints[:40], "tried": len(cands), "compat_note": compat_note,
        "category_item_id": d.get("category_item_id", ""), "category_store": cat_store or row.store,
        "brand": d.get("brand") or "",
    }


class RebuildIn(BaseModel):
    mode: str = "add"   # "add": keep the rows already on the draft and add the missing ones; "replace": start over from our fitment


@router.post("/{copy_id}/rebuild-compat")
def rebuild_compat(copy_id: int, body: RebuildIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Make the compatible-vehicles table again from the product's fitment, using eBay's own list for the draft's category."""
    r = _row(db, copy_id)
    if r.status != "draft" or r.source_item_id or not r.product_id:
        raise HTTPException(409, "Only a draft made from a product can be rebuilt from its fitment.")
    d = dict(r.data or {})
    if not d.get("category_id"):
        raise HTTPException(409, "Choose the store first so the eBay category is known.")
    from app.ebay_vocab import convert_with_ebay
    try:
        rows, notes = convert_with_ebay(db, r.product_id, d["category_id"])
    except Exception as e:
        db.rollback()
        raise HTTPException(502, f"eBay's vehicle list could not be read: {getattr(e, 'detail', None) or getattr(e, 'message', None) or e}")
    have = list(d.get("compat") or [])
    if body.mode == "replace":
        merged, added = rows, len(rows)
    else:
        keys = {tuple(map(tuple, c.get("pairs") or [])) for c in have}
        new = [c for c in rows if tuple(map(tuple, c["pairs"])) not in keys]
        merged, added = (have + new)[:MAX_COMPAT_ROWS], len(new)
    d["compat"], d["compat_notes"] = merged, notes
    d.pop("compat_edited", None)
    db.execute(text("update ebay_listing_copies set data = cast(:d as jsonb), updated_at = now() where id = :i"),
               {"d": json.dumps(d), "i": copy_id})
    db.commit()
    return {"rows": len(merged), "added": added, "notes": notes}


# --------------------------------------------------------------------------
# picking the category by searching eBay's own category tree (no model item number needed)
# --------------------------------------------------------------------------
@router.get("/categories")
def find_categories(q: str = Query(..., min_length=2, max_length=100)):
    """eBay's suggested leaf categories for a few words ('fork seals'), eBay Motors first, then eBay US."""
    from app import ebay_vocab as v
    if not v.configured():
        raise HTTPException(503, "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API, so eBay's categories cannot be searched.")
    out, seen, problems = [], set(), []
    key = " ".join(q.lower().split())
    for tree in v.TREES:
        try:
            res = v._cached(("catsug", tree, key), lambda t=tree: v._get(t, "get_category_suggestions", {"q": q.strip()}))
        except v.VocabError as e:
            problems.append(e.message)
            continue
        for sug in (res.get("categorySuggestions") or [])[:12]:
            c = sug.get("category") or {}
            cid = str(c.get("categoryId") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            anc = sorted(sug.get("categoryTreeNodeAncestors") or [], key=lambda a: a.get("categoryTreeNodeLevel") or 0)
            path = [a.get("categoryName") for a in anc if a.get("categoryName")] + [c.get("categoryName") or cid]
            out.append({"category_id": cid, "name": c.get("categoryName") or cid, "path": " > ".join(path),
                        "tree": tree, "motors": tree == "100"})
    if not out and problems:
        raise HTTPException(502, f"eBay's category search said: {problems[0]}")
    return {"categories": out}


class CategoryIn(BaseModel):
    category_id: str
    category_name: str = ""
    tree: str = "0"


@router.post("/{copy_id}/category")
def choose_category(copy_id: int, body: CategoryIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Use a category found by the search. Shipping and policies stay as the store's model listing gave them; the eBay site follows
    the category's tree (eBay Motors categories are listed on the eBayMotors site), and the compatibility table is made again for it."""
    from app import ebay_vocab as v
    r = _row(db, copy_id)
    if r.status != "draft":
        raise HTTPException(409, "This draft is already listed.")
    if r.source_item_id:
        raise HTTPException(409, "This draft is a copy of a live listing, so its category is fixed.")
    if not r.store:
        raise HTTPException(409, "Choose the store first, then the category.")
    cid = body.category_id.strip()
    if not cid.isdigit() or body.tree not in v.TREES:
        raise HTTPException(400, "That does not look like an eBay category.")
    d = dict(r.data or {})
    d["category_id"], d["category_name"] = cid, body.category_name.strip()
    d["category2_id"] = ""
    d["site"] = "eBayMotors" if body.tree == "100" else "US"
    d.pop("category_item_id", None)
    v._tree_of[cid] = body.tree
    compat_note = ""
    try:
        rows, notes = v.convert_with_ebay(db, r.product_id, cid)
        d["compat"], d["compat_notes"] = rows, notes
        d.pop("compat_edited", None)
        compat_note = f"{len(rows)} compatible vehicle rows were made again from our fitment for this category."
    except Exception as e:
        db.rollback()
        d["compat"], d["compat_notes"] = [], []
        compat_note = f"This category has no usable vehicle list ({getattr(e, 'detail', None) or getattr(e, 'message', None) or e}); the compatibility table was cleared."
    db.execute(text("update ebay_listing_copies set data = cast(:d as jsonb), updated_at = now() where id = :i"),
               {"d": json.dumps(d), "i": copy_id})
    db.commit()
    return {"category_id": cid, "category_name": d["category_name"], "site": d["site"], "compat_note": compat_note}
