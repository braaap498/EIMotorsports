"""
Look up an eBay listing to see what eBay will tell us about it: item specifics and the compatible-vehicle table.

A READ-ONLY TEST pathway on the New listing page, separate from "New listing from a product". It writes nothing to our database
and sends nothing to eBay except reads. It exists to answer one question before anything is built on it: can we pull the item
specifics and the compatibility table of a listing that is NOT ours?

Mounted at /admin/ebay-lookup/* (owner/admin only).

  GET  /search?q=&gtin=         Browse API search (public app token) for listings, newest relevance first. Parts category 6000 by default.
  GET  /item/{item_id}          Browse API get-item by item number. Returns the item specifics (eBay calls them "aspects") and says
                                which top-level fields eBay returned and whether any of them looks like a compatibility list.
  POST /compat-probe            {item_id, store}  Trading API GetItem with the compatibility list and item specifics switched on,
                                called with a CONNECTED store's token. Says whether the listing belongs to that store and returns
                                the compatibility rows eBay sent back (or that it sent none).

Why two calls: the Browse API needs no seller login and works on any listing, but is not documented to return the compatibility
table. The Trading call is documented to return it, and is known to work on our own listings; whether it works on another seller's
listing is what the probe shows.
"""
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin
from app.database import get_db
from app.ebay_edit import NS, _errors, _failed, _trading
from app.ebay_feedback import FeedbackError, _token, configured

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-lookup", tags=["admin-ebay-lookup"], dependencies=[Depends(require_admin)])

BROWSE = "https://api.ebay.com/buy/browse/v1"
ITEM_RE = re.compile(r"\d{9,15}")
MAX_COMPAT_SHOWN = 200


def _browse_get(path: str, params: dict) -> dict:
    """One read from the Browse API with the public app token. A refusal is turned into a readable message that includes eBay's reason."""
    try:
        tok = _token()
    except FeedbackError as e:
        raise HTTPException(502, str(e))
    req = urllib.request.Request(
        f"{BROWSE}{path}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"Bearer {tok}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        why = ""
        try:
            errs = json.loads(body).get("errors") or []
            why = "; ".join(f"{x.get('message', '')} (eBay {x.get('errorId', '')})" for x in errs)[:300]
        except ValueError:
            why = body[:200]
        log.warning("eBay Browse %s -> %s %s", path, e.code, why)
        raise HTTPException(502, f"eBay answered with an error ({e.code}). {why}".strip())
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise HTTPException(502, "Could not reach eBay.")


def _price(p: Optional[dict]) -> Optional[str]:
    return f"{p.get('value')} {p.get('currency', '')}".strip() if isinstance(p, dict) and p.get("value") is not None else None


@router.get("/search")
def search(q: str = Query("", max_length=120), gtin: str = Query("", max_length=20), category: str = Query("6000", max_length=40)):
    if not configured():
        raise HTTPException(409, "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API.")
    q, gtin = q.strip(), re.sub(r"\D", "", gtin)
    if len(q) < 2 and not gtin:
        raise HTTPException(400, "Type a part number or words to search for, or a UPC / barcode.")
    params: dict = {"limit": "20"}
    if q:
        params["q"] = q
    if gtin:
        params["gtin"] = gtin
    if category.strip() and re.fullmatch(r"[\d,]+", category.strip()):
        params["category_ids"] = category.strip()
    data = _browse_get("/item_summary/search", params)
    items = []
    for s in data.get("itemSummaries") or []:
        legacy = str(s.get("legacyItemId") or "")
        if not legacy.isdigit():
            parts = str(s.get("itemId") or "").split("|")
            legacy = parts[1] if len(parts) >= 2 and parts[1].isdigit() else ""
        if not legacy:
            continue
        items.append(
            {
                "item_id": legacy,
                "title": s.get("title") or "",
                "price": _price(s.get("price")),
                "image": (s.get("image") or {}).get("imageUrl"),
                "seller": (s.get("seller") or {}).get("username"),
                "condition": s.get("condition"),
                "url": s.get("itemWebUrl"),
            }
        )
    return {"total": int(data.get("total") or 0), "items": items}


def _find_compat_keys(obj, path: str = "") -> list[str]:
    """Every key anywhere in eBay's answer whose name mentions compatibility, so we can see whether the table is in there at all."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if "compat" in str(k).lower():
                out.append(p)
            out += _find_compat_keys(v, p)
    elif isinstance(obj, list):
        for v in obj[:3]:
            out += _find_compat_keys(v, path + "[]")
    return out


@router.get("/item/{item_id}")
def item(item_id: str):
    if not ITEM_RE.fullmatch(item_id or ""):
        raise HTTPException(400, "That does not look like an eBay item number.")
    # PRODUCT adds eBay's catalog product details (and its own aspects) when the listing is matched to one
    data = _browse_get("/item/get_item_by_legacy_id", {"legacy_item_id": item_id, "fieldgroups": "PRODUCT"})
    aspects = [
        {"name": a.get("name") or "", "value": a.get("value") or ""}
        for a in (data.get("localizedAspects") or [])
        if a.get("name")
    ]
    product = data.get("product") or {}
    product_aspects = []
    for g in product.get("aspectGroups") or []:
        for a in g.get("aspects") or []:
            if a.get("localizedName"):
                product_aspects.append({"name": a.get("localizedName"), "values": a.get("localizedValues") or []})
    return {
        "item_id": item_id,
        "title": data.get("title"),
        "seller": (data.get("seller") or {}).get("username"),
        "condition": data.get("condition"),
        "price": _price(data.get("price")),
        "category_id": data.get("categoryId"),
        "category_path": data.get("categoryPath"),
        "epid": product.get("epid") or data.get("epid"),
        "aspects": aspects,
        "product_aspects": product_aspects,
        "keys_returned": sorted(data.keys()),
        "compat_fields": _find_compat_keys(data),
        "url": data.get("itemWebUrl"),
    }


class ProbeIn(BaseModel):
    item_id: str = Field(..., min_length=9, max_length=15)
    store: str = Field(..., min_length=1, max_length=60)


def _read_reference(db: Session, store: str, item_id: str) -> dict:
    """Trading API GetItem on any listing, with its item specifics and compatibility list, using one of our stores' tokens. Read only."""
    root = _trading(
        db, store, "GetItem",
        f"<ItemID>{item_id}</ItemID><DetailLevel>ReturnAll</DetailLevel>"
        "<IncludeItemSpecifics>true</IncludeItemSpecifics><IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>",
    )
    errs = _errors(root)
    ack = root.findtext(f"{NS}Ack") or ""
    it = root.find(f"{NS}Item")
    if _failed(root) or it is None:
        return {"ok": False, "ack": ack, "errors": errs or [{"code": "", "severity": "", "message": "eBay did not return the listing."}],
                "compat_count": 0, "compat": [], "specifics": []}
    seller = (it.findtext(f"{NS}Seller/{NS}UserID") or "").strip()
    row = db.execute(text("select seller_username from ebay_accounts where lower(store) = lower(:s)"), {"s": store}).first()
    mine = bool(row and row.seller_username and row.seller_username.lower() == seller.lower())
    specifics = []
    for nv in it.findall(f"{NS}ItemSpecifics/{NS}NameValueList"):
        name = (nv.findtext(f"{NS}Name") or "").strip()
        vals = [(v.text or "").strip() for v in nv.findall(f"{NS}Value") if (v.text or "").strip()]
        if name:
            specifics.append({"name": name, "values": vals})
    compat = []
    for c in it.findall(f"{NS}ItemCompatibilityList/{NS}Compatibility"):
        vals = {}
        for nv in c.findall(f"{NS}NameValueList"):
            n = (nv.findtext(f"{NS}Name") or "").strip()
            if n:
                vals[n] = (nv.findtext(f"{NS}Value") or "").strip()
        compat.append({"values": vals, "notes": (c.findtext(f"{NS}CompatibilityNotes") or "").strip()})
    declared = (it.findtext(f"{NS}ItemCompatibilityCount") or "").strip()
    return {
        "ok": True,
        "ack": ack,
        "errors": errs,  # warnings are kept: eBay sometimes explains why a list was left out
        "item_id": item_id,
        "seller": seller,
        "belongs_to_store": mine,
        "title": (it.findtext(f"{NS}Title") or "").strip(),
        "category_id": (it.findtext(f"{NS}PrimaryCategory/{NS}CategoryID") or "").strip(),
        "category_name": (it.findtext(f"{NS}PrimaryCategory/{NS}CategoryName") or "").strip(),
        "specifics": specifics,
        "compat_declared_count": int(declared) if declared.isdigit() else None,
        "compat_count": len(compat),
        "compat_all": compat,
    }


def _shown(ref: dict) -> dict:
    out = {k: v for k, v in ref.items() if k != "compat_all"}
    allc = ref.get("compat_all") or []
    out["compat"] = allc[:MAX_COMPAT_SHOWN]
    out["compat_truncated"] = len(allc) > MAX_COMPAT_SHOWN
    return out


@router.post("/compat-probe")
def compat_probe(body: ProbeIn, db: Session = Depends(get_db)):
    """Ask eBay's Trading API for the listing WITH its compatibility list, using one of our connected stores' tokens. Read only."""
    item_id = body.item_id.strip()
    if not ITEM_RE.fullmatch(item_id):
        raise HTTPException(400, "That does not look like an eBay item number.")
    return _shown(_read_reference(db, body.store.strip(), item_id))


# --------------------------------------------------------------------------
# one click: a draft from one of our products, filled with what the looked-up listing carries
# --------------------------------------------------------------------------
class MakeIn(BaseModel):
    item_id: str = Field(..., min_length=9, max_length=15)     # the listing to take specifics and compatibility from
    product_id: int                                            # OUR product the new listing is for
    store: str = Field(..., min_length=1, max_length=60)       # the store the new listing goes to (also the login used to read)


# Specifics that identify the OTHER seller's part, not ours: never copied. Our own product supplies these.
_SKIP_SPECIFICS = {"upc", "ean", "isbn", "gtin", "manufacturer part number", "mpn", "brand"}


@router.post("/make-draft")
def make_draft(body: MakeIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Everything between 'I read a listing' and 'a draft ready to check', in one go:
         1. read the listing (item specifics and compatibility table);
         2. make a draft from our product (title, description, price, photos, specifics, our own fitment as eBay rows);
         3. choose the store (category, shipping and return rules from a similar live listing of ours there);
         4. add the listing's compatibility rows that the draft does not have yet, and its item specifics that the draft lacks.
       The other seller's title, description and photos are NOT taken: those come from our product. NOTHING is listed on eBay. The draft is
       then checked with eBay and listed from the draft screen, as for any other draft."""
    from app.ebay_copy import _row
    from app.ebay_newdraft import MAX_COMPAT_ROWS, CreateIn, StoreIn, _pairs, choose_store, create

    item_id = body.item_id.strip()
    if not ITEM_RE.fullmatch(item_id):
        raise HTTPException(400, "That does not look like an eBay item number.")
    store = body.store.strip()
    ref = _read_reference(db, store, item_id)
    if not ref["ok"]:
        raise HTTPException(502, "eBay would not give us that listing: " + "; ".join(e["message"] for e in ref["errors"])[:300])

    made = create(CreateIn(product_id=body.product_id), db=db, user=user)   # refuses (409) a product that already has eBay listings
    copy_id = int(made["id"])
    warnings: list[str] = list(made.get("warnings") or [])

    store_done = False
    try:
        choose_store(copy_id, StoreIn(store=store), db=db, user=user)
        store_done = True
    except HTTPException as e:
        db.rollback()
        warnings.append(f"The store step did not finish: {e.detail} Choose the store on the draft.")

    added_rows = 0
    added_specs: list[str] = []
    try:
        r = _row(db, copy_id)
        d = dict(r.data or {})
        # item specifics the draft does not have
        have = {str(x.get("name", "")).lower() for x in d.get("item_specifics", [])}
        specs = list(d.get("item_specifics") or [])
        for sp in ref["specifics"]:
            n = " ".join(str(sp["name"]).split())
            vals = [" ".join(v.split()) for v in sp["values"] if v and len(" ".join(v.split())) <= 65]
            if not n or not vals or len(n) > 65 or n.lower() in have or n.lower() in _SKIP_SPECIFICS:
                continue
            specs.append({"name": n, "values": vals})
            have.add(n.lower())
            added_specs.append(n)
        d["item_specifics"] = specs[:100]
        # compatibility rows the draft does not have
        rows = list(d.get("compat") or [])
        seen = {tuple(sorted(map(tuple, x.get("pairs") or []))) for x in rows}
        skipped_long = 0
        for c in ref["compat_all"]:
            pairs = _pairs(c["values"])
            if not pairs:
                continue
            if any(len(a) > 65 or len(b) > 65 for a, b in pairs):
                skipped_long += 1
                continue
            key = tuple(sorted(map(tuple, pairs)))
            if key in seen:
                continue
            seen.add(key)
            rows.append({"pairs": pairs, "notes": (c.get("notes") or "")[:500]})
            added_rows += 1
        if len(rows) > MAX_COMPAT_ROWS:
            warnings.append(f"eBay takes at most {MAX_COMPAT_ROWS} compatibility rows; {len(rows) - MAX_COMPAT_ROWS} were cut.")
            rows = rows[:MAX_COMPAT_ROWS]
        if skipped_long:
            warnings.append(f"{skipped_long} compatibility row(s) had a value over eBay's 65-character limit and were left out.")
        d["compat"] = rows
        d["compat_edited"] = True   # so choosing a store later does not throw these rows away
        d["reference_item_id"] = item_id
        notes = list(d.get("compat_notes") or [])
        notes.append(f"{added_rows} compatibility row(s) were added from eBay listing {item_id} (seller {ref['seller']}); the rest came from our own fitment.")
        d["compat_notes"] = notes
        db.execute(
            text("update ebay_listing_copies set data = cast(:d as jsonb), updated_at = now() where id = :i"),
            {"d": json.dumps(d), "i": copy_id},
        )
        db.commit()
        total_rows = len(rows)
    except Exception:
        db.rollback()
        log.exception("could not add the looked-up listing's details to draft %s", copy_id)
        warnings.append("The draft was made, but the listing's specifics and compatibility rows could not be added to it. Check the draft.")
        total_rows = int(made.get("compat_rows") or 0)
    if not ref["compat_all"]:
        warnings.append("eBay returned no compatibility rows for that listing, so the table comes only from our own fitment.")
    return {
        "id": copy_id,
        "store_done": store_done,
        "compat_rows": total_rows,
        "compat_from_listing": added_rows,
        "specifics_added": added_specs,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# eBay compatibility rows -> a product's WEBSITE fitment
# --------------------------------------------------------------------------
class FitIn(BaseModel):
    product_id: int
    item_id: Optional[str] = Field(None, max_length=15)   # read this listing's table from eBay (needs store)
    store: Optional[str] = Field(None, max_length=60)
    from_listings: bool = False                            # use the saved compatibility tables of the product's OWN eBay listings
    apply: bool = False                                    # False: show what would be added; True: add it


_YEAR = re.compile(r"(19|20)\d{2}")


def _resolve_model(db: Session, make: str, model: str):
    """Our model for an eBay make + model, the same way the compat-to-fitment conversion decides it: the decisions table
    (fitment_ebay_model_map) first, then an exact match on the normalised names when exactly one model matches. (id or None, skipped)."""
    r = db.execute(
        text(
            """
            select case when m.skip then null
                        when exists (select 1 from vehicle_models z where z.id = m.model_id) then m.model_id
                        when ex.n = 1 then ex.model_id end as model_id,
                   coalesce(m.skip, false) as skip
            from (select fitment_key(cast(:mk as text)) as make_key, fitment_key(cast(:mo as text)) as model_key) k
            left join fitment_ebay_model_map m on m.make_key = k.make_key and m.model_key = k.model_key
            left join lateral (
                select min(vmo.id) as model_id, count(*) as n
                from vehicle_makes vm join vehicle_models vmo on vmo.make_id = vm.id
                where fitment_key(vm.name) = k.make_key and fitment_key(vmo.name) = k.model_key) ex on true
            """
        ),
        {"mk": make, "mo": model},
    ).first()
    return (r.model_id, bool(r.skip)) if r else (None, False)


def _fitment_plan(db: Session, product_id: int, rows: list[dict]) -> dict:
    """What adding these eBay rows (attribute dicts with Make, Model, Year ...) to the product's fitment would do. Writes nothing."""
    import datetime

    maxyear = datetime.date.today().year + 1
    groups: dict[tuple, dict] = {}
    no_year = 0
    for a in rows:
        mk, mo = " ".join(str(a.get("Make") or "").split()), " ".join(str(a.get("Model") or "").split())
        y = str(a.get("Year") or "").strip()
        if not mk or not mo:
            continue
        if not _YEAR.fullmatch(y) or int(y) > maxyear:
            no_year += 1
            continue
        g = groups.setdefault((mk, mo), {"years": set(), "subs": {}})
        g["years"].add(int(y))
        sub = " ".join(str(a.get("Submodel") or a.get("Trim") or "").split())
        if sub:
            g["subs"].setdefault(int(y), set()).add(sub)
    models, unresolved, skipped = [], [], []
    for (mk, mo), g in sorted(groups.items()):
        model_id, skip = _resolve_model(db, mk, mo)
        if skip:
            skipped.append(f"{mk} {mo}")
            continue
        if model_id is None:
            unresolved.append({"make": mk, "model": mo, "years": sorted(g["years"])})
            continue
        names = db.execute(
            text("select vmk.name as make, vmo.name as model from vehicle_models vmo join vehicle_makes vmk on vmk.id = vmo.make_id where vmo.id = :m"),
            {"m": model_id},
        ).first()
        have = {int(r.year) for r in db.execute(
            text("select v.year from product_fitment pf join vehicles v on v.id = pf.vehicle_id where pf.product_id = :p and v.model_id = :m and v.year is not null"),
            {"p": product_id, "m": model_id}).all()}
        new = sorted(y for y in g["years"] if y not in have)
        models.append({
            "model_id": int(model_id), "make": names.make, "model": names.model, "ebay_make": mk, "ebay_model": mo,
            "new_years": new, "already_years": sorted(y for y in g["years"] if y in have),
            "subs": {str(y): sorted(g["subs"].get(y, ())) for y in new if g["subs"].get(y)},
        })
    return {"models": models, "unresolved": unresolved, "skipped": skipped, "no_year": no_year}


def _fitment_apply(db: Session, product_id: int, plan: dict) -> dict:
    """Add the planned years. Only ADDS: missing vehicle years for a model that already exists, and product_fitment links.
    Nothing is deleted or changed, and no make or model is created."""
    vehicles_made = links = 0
    for m in plan["models"]:
        for y in m["new_years"]:
            vid = db.execute(
                text("select min(id) from vehicles where model_id = :m and year = :y and submodel is null"), {"m": m["model_id"], "y": y}
            ).scalar()
            if vid is None:
                vid = db.execute(
                    text("insert into vehicles (model_id, year) values (:m, :y) returning id"), {"m": m["model_id"], "y": y}
                ).scalar()
                vehicles_made += 1
            subs = m["subs"].get(str(y))
            ins = db.execute(
                text(
                    "insert into product_fitment (product_id, vehicle_id, notes, source) select :p, :v, :n, 'ebay' "
                    "where not exists (select 1 from product_fitment pf where pf.product_id = :p and pf.vehicle_id = :v) returning vehicle_id"
                ),
                {"p": product_id, "v": vid, "n": ("ebay: " + ", ".join(subs))[:400] if subs else None},
            ).first()
            links += 1 if ins else 0
    return {"links_added": links, "vehicle_years_added": vehicles_made}


def _saved_tables(db: Session, product_id: int) -> tuple[list[dict], int, int]:
    """The compatibility tables the compat import saved for this product's own eBay listings: (rows, listings with a table, listings)."""
    from app.admin_ebay_fitment import _DIGITS, _LIST

    total = db.execute(
        text(f"select count(distinct {_DIGITS}) from channel_listings cl where cl.product_id = :p and {_DIGITS} <> ''"), {"p": product_id}
    ).scalar() or 0
    found = db.execute(
        text(
            f"select c.item_id, {_LIST.format(c='c')} as lst from ebay_compat_items c "
            f"join channel_listings cl on {_DIGITS} = c.item_id where cl.product_id = :p and c.status = 'ok'"
        ),
        {"p": product_id},
    ).all()
    rows, used = [], 0
    for f in found:
        lst = f.lst if isinstance(f.lst, list) else []
        got = [e.get("attrs") for e in lst if isinstance(e, dict) and isinstance(e.get("attrs"), dict)]
        if got:
            used += 1
            rows += got
    return rows, used, int(total)


@router.post("/fitment")
def add_to_fitment(body: FitIn, db: Session = Depends(get_db)):
    """Put eBay compatibility rows into ONE product's website fitment. With apply false it only shows what would be added; with
    apply true it adds exactly that. Rows can come from a listing read from eBay now, from the saved tables of the product's own
    listings, or both. A machine our vehicle list does not know is listed, never guessed (settle those in the compat import tab)."""
    pj = db.execute(text("select title, coalesce(is_universal, false) as u, coalesce(fitment_exempt, false) as x from products where id = :p"), {"p": body.product_id}).first()
    if not pj:
        raise HTTPException(404, "That product does not exist.")
    if pj.u or pj.x:
        raise HTTPException(409, "This product is marked universal or exempt from fitment, so no fitment is added to it.")
    if not body.item_id and not body.from_listings:
        raise HTTPException(400, "Choose where the compatibility rows come from.")
    rows: list[dict] = []
    info: dict = {}
    if body.item_id:
        item_id = body.item_id.strip()
        if not ITEM_RE.fullmatch(item_id) or not (body.store or "").strip():
            raise HTTPException(400, "A listing and a store are needed to read it from eBay.")
        ref = _read_reference(db, body.store.strip(), item_id)
        if not ref["ok"]:
            raise HTTPException(502, "eBay would not give us that listing: " + "; ".join(e["message"] for e in ref["errors"])[:300])
        rows += [c["values"] for c in ref["compat_all"]]
        info["from_listing_rows"] = len(ref["compat_all"])
    if body.from_listings:
        saved, used, total = _saved_tables(db, body.product_id)
        rows += saved
        info.update({"own_listings_with_table": used, "own_listings": total})
    plan = _fitment_plan(db, body.product_id, rows)
    plan.update(info)
    plan["rows_read"] = len(rows)
    plan["product"] = pj.title
    if not body.apply:
        plan["applied"] = False
        return plan
    try:
        plan["added"] = _fitment_apply(db, body.product_id, plan)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not add fitment to product %s", body.product_id)
        raise HTTPException(500, "The fitment could not be added, and nothing was changed.")
    plan["applied"] = True
    return plan
