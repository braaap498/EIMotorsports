"""
"Sell similar": copy a live eBay listing into a NEW listing (Trading API, AddFixedPriceItem).

Mounted at /admin/ebay-copy/* (owner/admin only).

  GET    ""               the drafts in progress (newest first), so none gets lost
  POST   /create          {store, item_id}  read the listing from eBay and save a copy as a DRAFT on the website only
  GET    /{id}            the draft
  PUT    /{id}            save edits to the draft (nothing goes to eBay)
  DELETE /{id}            throw the draft away
  POST   /{id}/preview    asks eBay to CHECK the new listing (VerifyAddFixedPriceItem): nothing is listed, nothing is paid;
                          returns eBay's errors, warnings and the fees it would charge, plus a fingerprint
  POST   /{id}/list       lists it for real, only if (a) the store's live-push switch is ON and (b) it is the very same
                          listing that was previewed. The new item number is then linked to the same product the
                          original is linked to.

What is copied: title, description, price, custom label (SKU), photos (the original's eBay pictures), item specifics,
the parts compatibility list, category, condition, location, handling time, the shipping / payment / return
business policies, second category, store categories, best-offer setting with its auto-accept / auto-decline prices,
product identifiers (UPC / EAN / ISBN / brand + MPN). Quantity is NOT copied: the website owns stock, so the new listing gets the
product's available stock (at least 1 is needed).
Every preview and listing is written to ebay_push_log.
"""
import hashlib
import json
import logging
import re
from decimal import Decimal
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.admin_ebay_new import _insert_link
from app.auth import AuthUser, require_admin
from app.database import get_db
from app.ebay_edit import (
    ITEM_RE, NS, _cdata, _errors, _failed, _first_message, _log, _money, _read_item, _trading,
)
from app.ebay_oauth import _store_row
from app.availability import effective_sql
from app.stock import available_sql

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-copy", tags=["admin-ebay-copy"], dependencies=[Depends(require_admin)])

NO_TABLE = "Copies cannot be stored yet. Run sql/ebay_listing_copies.sql in Supabase, then try again."



# --------------------------------------------------------------------------
# shipping, handling and returns for sellers who use the older way (no business policies)
# --------------------------------------------------------------------------
def _tx(el, tag: str) -> str:
    return (el.findtext(f"{NS}{tag}") or "").strip() if el is not None else ""


def _terms_from_item(item) -> dict:
    """The shipping services, return rules, payment methods and ship-to list of a live listing, read from GetItem.
    Only fields that can be sent again are kept (no delivery estimates or display texts)."""
    shipping: dict = {}
    sd = item.find(f"{NS}ShippingDetails")
    if sd is not None:
        services = []
        for o in sd.findall(f"{NS}ShippingServiceOptions"):
            svc = _tx(o, "ShippingService")
            if svc:
                services.append({"priority": _tx(o, "ShippingServicePriority") or str(len(services) + 1), "service": svc,
                                 "cost": _tx(o, "ShippingServiceCost"), "additional": _tx(o, "ShippingServiceAdditionalCost"),
                                 "free": _tx(o, "FreeShipping").lower() == "true"})
        intl = []
        for o in sd.findall(f"{NS}InternationalShippingServiceOption"):
            svc = _tx(o, "ShippingService")
            if svc:
                intl.append({"priority": _tx(o, "ShippingServicePriority") or str(len(intl) + 1), "service": svc,
                             "cost": _tx(o, "ShippingServiceCost"), "additional": _tx(o, "ShippingServiceAdditionalCost"),
                             "ship_to": [(x.text or "").strip() for x in o.findall(f"{NS}ShipToLocation") if (x.text or "").strip()]})
        shipping = {"type": _tx(sd, "ShippingType"), "services": services, "international": intl,
                    "excluded": [(x.text or "").strip() for x in sd.findall(f"{NS}ExcludeShipToLocation") if (x.text or "").strip()]}
    returns: dict = {}
    rp = item.find(f"{NS}ReturnPolicy")
    if rp is not None:
        for key, tag in (("accepted", "ReturnsAcceptedOption"), ("within", "ReturnsWithinOption"), ("paid_by", "ShippingCostPaidByOption"),
                         ("refund", "RefundOption"), ("restocking", "RestockingFeeValueOption"), ("description", "Description")):
            v = _tx(rp, tag)
            if v:
                returns[key] = v
    return {
        "shipping": shipping, "returns": returns,
        "payment_methods": [(x.text or "").strip() for x in item.findall(f"{NS}PaymentMethods") if (x.text or "").strip()],
        "ship_to": [(x.text or "").strip() for x in item.findall(f"{NS}ShipToLocations") if (x.text or "").strip()],
    }


def _terms_problem(d: dict) -> str:
    """Why the shipping terms of this draft cannot be sent as they are, or ''."""
    if str((d.get("shipping") or {}).get("type", "")).lower().startswith("calculated"):
        return ("The model listing uses calculated shipping, which depends on this part's weight and size, and those are not copied. "
                "Use a flat-rate listing as the model.")
    return ""


def _has_profiles(d: dict) -> bool:
    return all(d.get(k) for k in ("ship_profile", "pay_profile", "return_profile"))


def _has_terms(d: dict) -> bool:
    sh, rt = d.get("shipping") or {}, d.get("returns") or {}
    return bool(sh.get("services")) and bool(rt.get("accepted")) and not _terms_problem(d)


def terms_summary(d: dict) -> list[str]:
    """Plain-language lines about shipping, handling and returns, for the screen."""
    out: list[str] = []
    cur = d.get("currency") or "USD"
    if _has_profiles(d):
        return ["Shipping, payment and return rules come from the store's business policies (as on the model listing)."]
    sh, rt = d.get("shipping") or {}, d.get("returns") or {}
    for o in sh.get("services", []):
        if o.get("free"):
            out.append(f"Shipping {o['priority']}: {o['service']}, free")
        else:
            extra = f" (+{o['additional']} each extra)" if o.get("additional") not in (None, "", "0", "0.0", "0.00") else ""
            out.append(f"Shipping {o['priority']}: {o['service']}, {o.get('cost') or '0.00'} {cur}{extra}")
    for o in sh.get("international", []):
        out.append(f"International: {o['service']}, {o.get('cost') or '0.00'} {cur}" + (f" to {', '.join(o['ship_to'])}" if o.get("ship_to") else ""))
    if d.get("dispatch_days"):
        out.append(f"Handling time: {d['dispatch_days']} business day{'s' if str(d['dispatch_days']) != '1' else ''}")
    if rt.get("accepted"):
        bits = [rt["accepted"]] + [rt[k] for k in ("within", "paid_by", "refund") if rt.get(k)]
        out.append("Returns: " + ", ".join(bits))
    if sh.get("excluded"):
        out.append("Does not ship to: " + ", ".join(sh["excluded"]))
    return out


def _money_text(v: str, default: str = "0.00") -> str:
    v = (v or "").strip()
    if not v:
        return default
    try:
        return str(_money(v))
    except HTTPException:
        return default


def _legacy_xml(d: dict, cur: str) -> str:
    """ShippingDetails, ReturnPolicy, PaymentMethods and ShipToLocations, as the older listings carry them."""
    c = escape(cur)
    sh, rt = d.get("shipping") or {}, d.get("returns") or {}
    sd = ""
    if sh.get("type"):
        sd += f"<ShippingType>{escape(sh['type'])}</ShippingType>"
    for o in sh.get("services", []):
        cost = "0.00" if o.get("free") else _money_text(o.get("cost", ""))
        x = f"<ShippingService>{escape(o['service'])}</ShippingService>"
        if o.get("additional"):
            x += f'<ShippingServiceAdditionalCost currencyID="{c}">{_money_text(o["additional"])}</ShippingServiceAdditionalCost>'
        x += f'<ShippingServiceCost currencyID="{c}">{cost}</ShippingServiceCost>'
        x += f"<ShippingServicePriority>{escape(str(o['priority']))}</ShippingServicePriority>"
        if o.get("free"):
            x += "<FreeShipping>true</FreeShipping>"
        sd += f"<ShippingServiceOptions>{x}</ShippingServiceOptions>"
    for o in sh.get("international", []):
        x = f"<ShippingService>{escape(o['service'])}</ShippingService>"
        if o.get("additional"):
            x += f'<ShippingServiceAdditionalCost currencyID="{c}">{_money_text(o["additional"])}</ShippingServiceAdditionalCost>'
        x += f'<ShippingServiceCost currencyID="{c}">{_money_text(o.get("cost", ""))}</ShippingServiceCost>'
        x += f"<ShippingServicePriority>{escape(str(o['priority']))}</ShippingServicePriority>"
        x += "".join(f"<ShipToLocation>{escape(z)}</ShipToLocation>" for z in o.get("ship_to", []))
        sd += f"<InternationalShippingServiceOption>{x}</InternationalShippingServiceOption>"
    sd += "".join(f"<ExcludeShipToLocation>{escape(z)}</ExcludeShipToLocation>" for z in sh.get("excluded", []))
    out = f"<ShippingDetails>{sd}</ShippingDetails>" if sd else ""
    if rt.get("accepted"):
        r = f"<ReturnsAcceptedOption>{escape(rt['accepted'])}</ReturnsAcceptedOption>"
        for tag, key in (("RefundOption", "refund"), ("ReturnsWithinOption", "within"), ("ShippingCostPaidByOption", "paid_by"),
                         ("RestockingFeeValueOption", "restocking")):
            if rt.get(key):
                r += f"<{tag}>{escape(rt[key])}</{tag}>"
        if rt.get("description"):
            r += f"<Description>{escape(rt['description'])}</Description>"
        out += f"<ReturnPolicy>{r}</ReturnPolicy>"
    out += "".join(f"<PaymentMethods>{escape(m)}</PaymentMethods>" for m in d.get("payment_methods", []))
    out += "".join(f"<ShipToLocations>{escape(z)}</ShipToLocations>" for z in d.get("ship_to", []))
    return out


# --------------------------------------------------------------------------
# reading what a copy needs from the original
# --------------------------------------------------------------------------
def _extras(db: Session, store: str, item_id: str) -> dict:
    """The parts of the original that a new listing needs and the editor does not show."""
    root = _trading(
        db, store, "GetItem",
        f"<ItemID>{item_id}</ItemID><DetailLevel>ReturnAll</DetailLevel>"
        "<IncludeItemSpecifics>true</IncludeItemSpecifics><IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>",
    )
    if _failed(root):
        raise HTTPException(502, _first_message(root))
    item = root.find(f"{NS}Item")
    if item is None:
        raise HTTPException(502, "eBay did not return the listing.")

    def t(path: str) -> str:
        return (item.findtext("/".join(NS + p for p in path.split("/"))) or "").strip()

    compat: list[dict] = []
    for c in item.findall(f"{NS}ItemCompatibilityList/{NS}Compatibility"):
        pairs = []
        for n in c.findall(f"{NS}NameValueList"):
            nm = (n.findtext(f"{NS}Name") or "").strip()
            if nm:
                pairs.append([nm, (n.findtext(f"{NS}Value") or "").strip()])
        notes = (c.findtext(f"{NS}CompatibilityNotes") or "").strip()
        if pairs:
            compat.append({"pairs": pairs, "notes": notes})

    return {
        "category_id": t("PrimaryCategory/CategoryID"),
        "category_name": t("PrimaryCategory/CategoryName"),
        "condition_id": t("ConditionID"),
        "condition_note": t("ConditionDescription"),
        "currency": (item.find(f"{NS}SellingStatus/{NS}CurrentPrice").get("currencyID") if item.find(f"{NS}SellingStatus/{NS}CurrentPrice") is not None else "USD"),
        "country": t("Country"),
        "location": t("Location"),
        "postal_code": t("PostalCode"),
        "dispatch_days": t("DispatchTimeMax"),
        "ship_profile": t("SellerProfiles/SellerShippingProfile/ShippingProfileID"),
        "pay_profile": t("SellerProfiles/SellerPaymentProfile/PaymentProfileID"),
        "return_profile": t("SellerProfiles/SellerReturnProfile/ReturnProfileID"),
        "best_offer": t("BestOfferDetails/BestOfferEnabled").lower() == "true",
        "best_offer_accept": t("ListingDetails/BestOfferAutoAcceptPrice"),
        "best_offer_decline": t("ListingDetails/MinimumBestOfferPrice"),
        "category2_id": t("SecondaryCategory/CategoryID"),
        "store_cat1": t("Storefront/StoreCategoryID"),
        "store_cat2": t("Storefront/StoreCategory2ID"),
        "upc": t("ProductListingDetails/UPC"),
        "ean": t("ProductListingDetails/EAN"),
        "isbn": t("ProductListingDetails/ISBN"),
        "brand": t("ProductListingDetails/BrandMPN/Brand"),
        "mpn": t("ProductListingDetails/BrandMPN/MPN"),
        "compat": compat,
        **_terms_from_item(item),
    }


# --------------------------------------------------------------------------
# the draft
# --------------------------------------------------------------------------
class DraftIn(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    price: Optional[str] = None
    sku: Optional[str] = None
    photos: Optional[list[str]] = Field(default=None, max_length=24)  # the pictures to keep, in order (all must be the original's)
    item_specifics: Optional[list[dict]] = Field(default=None, max_length=100)  # [{name, values:[...]}]
    compat: Optional[list[dict]] = Field(default=None, max_length=3000)  # the complete list: [{pairs:[[name, value], ...], notes}]


def _row(db: Session, copy_id: int):
    try:
        r = db.execute(
            text("select id, store, source_item_id, product_id, status, data, new_item_id, created_by, updated_at "
                 "from ebay_listing_copies where id = :i"),
            {"i": copy_id},
        ).first()
    except Exception:
        db.rollback()
        raise HTTPException(503, NO_TABLE)
    if not r:
        raise HTTPException(404, "That copy does not exist (it may have been deleted).")
    return r


def _compat_clean(rows: list[dict]) -> list[dict]:
    out, seen = [], set()
    for row in rows:
        pairs = []
        for pr in row.get("pairs") or []:
            if not isinstance(pr, (list, tuple)) or len(pr) != 2:
                raise HTTPException(400, "A compatible vehicle row is not in the expected shape. Reload the page.")
            n, v = " ".join(str(pr[0]).split()), " ".join(str(pr[1]).split())
            if not n or not v:
                raise HTTPException(400, "A compatible vehicle row has an empty name or value.")
            if len(n) > 65 or len(v) > 65:
                raise HTTPException(400, f"'{n}' has a name or value over 65 characters (eBay's limit).")
            pairs.append([n, v])
        if not pairs:
            continue
        notes = " ".join(str(row.get("notes") or "").split())[:500]
        key = (tuple(sorted(map(tuple, pairs))), notes)
        if key in seen:
            continue
        seen.add(key)
        out.append({"pairs": pairs, "notes": notes})
    return out


def _shape(r) -> dict:
    d = dict(r.data or {})
    return {
        "id": r.id, "store": r.store, "source_item_id": r.source_item_id, "product_id": r.product_id,
        "status": r.status, "new_item_id": r.new_item_id, "created_by": r.created_by,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        "title": d.get("title", ""), "description": d.get("description", ""), "price": d.get("price", ""),
        "sku": d.get("sku", ""), "photos": d.get("photos", []), "original_photos": d.get("original_photos", []),
        "item_specifics": d.get("item_specifics", []), "currency": d.get("currency", "USD"),
        "category_name": d.get("category_name", ""), "compat_count": len(d.get("compat", [])),
        "condition_note": d.get("condition_note", ""),
        # a draft started from a product (not copied from a live listing) has no store until one is chosen
        "category_id": d.get("category_id", ""), "compat_notes": d.get("compat_notes", []),
        "needs_store": not r.store, "model_item_id": d.get("model_item_id", ""),
        "compat": d.get("compat", []), "compat_edited": bool(d.get("compat_edited")),
        "terms": terms_summary(d), "terms_problem": _terms_problem(d),
    }


class CreateIn(BaseModel):
    store: str
    item_id: str


@router.post("/create")
def create(body: CreateIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    row = _store_row(db, body.store)
    item_id = body.item_id.strip()
    if not ITEM_RE.fullmatch(item_id):
        raise HTTPException(400, "That does not look like an eBay item number.")
    src = _read_item(db, row.store, item_id)
    if src["has_variations"]:
        raise HTTPException(409, "This listing has variations. Copying listings with variations is not supported yet.")
    if src["listing_type"] not in ("FixedPriceItem", "StoresFixedPrice"):
        raise HTTPException(409, "Only fixed-price listings can be copied.")
    pid = db.execute(
        text("select product_id from channel_listings where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i "
             "and lower(coalesce(store, '')) = lower(:s) limit 1"),
        {"i": item_id, "s": row.store},
    ).scalar()
    if not pid:
        raise HTTPException(409, "This listing is not linked to a product on the website, so the copy would have nothing to link to. Link it first.")
    ex = _extras(db, row.store, item_id)
    if not ex["category_id"]:
        raise HTTPException(409, "eBay did not say which category this listing uses. Nothing was copied.")
    if _terms_problem(ex):
        raise HTTPException(409, _terms_problem(ex) + " Nothing was copied.")
    if not _has_profiles(ex) and not _has_terms(ex):
        raise HTTPException(
            409,
            "eBay gave neither business policies nor shipping services and return rules for this listing, so the new listing would have no "
            "shipping or returns. Nothing was copied.",
        )
    photos = [p["url"] for p in src["photos"] if p.get("url")]
    if not photos:
        raise HTTPException(409, "The listing has no photos to copy.")
    data = {
        "title": src["title"], "description": src["description"], "price": src["price"] or "",
        "sku": _product_label(db, pid) or src["sku"],  # the custom label comes from the product
        "photos": photos, "original_photos": photos,
        "item_specifics": [{"name": s["name"], "values": s["values"]} for s in src["item_specifics"]],
        **ex,
    }
    try:
        new_id = db.execute(
            text("insert into ebay_listing_copies (store, source_item_id, product_id, status, data, created_by) "
                 "values (:s, :i, :p, 'draft', cast(:d as jsonb), :u) returning id"),
            {"s": row.store, "i": item_id, "p": pid, "d": json.dumps(data), "u": user.email or user.user_id},
        ).scalar()
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(503, NO_TABLE)
    return {"id": new_id}


@router.get("")
def list_drafts(db: Session = Depends(get_db)):
    """The drafts still in progress, newest first, so a started copy is never lost."""
    try:
        rows = db.execute(
            text("select id, store, source_item_id, data->>'title' as title, updated_at from ebay_listing_copies "
                 "where status = 'draft' order by updated_at desc limit 30")
        ).all()
    except Exception:
        db.rollback()
        return {"drafts": []}
    return {"drafts": [{"id": r.id, "store": r.store, "source_item_id": r.source_item_id, "title": r.title or "",
                        "updated_at": r.updated_at.isoformat() if r.updated_at else None} for r in rows]}


def _duplicates(db: Session, store: str, title: str, source_item_id: str) -> list[str]:
    """Item numbers already in this store with exactly the same title: eBay tends to refuse identical duplicates."""
    try:
        rows = db.execute(
            text("select item_id from ebay_report_rows where lower(store) = lower(:s) and lower(title) = lower(:t) and item_id <> :i limit 5"),
            {"s": store, "t": title, "i": source_item_id},
        ).all()
        return [r.item_id for r in rows]
    except Exception:
        db.rollback()
        return []


@router.get("/{copy_id}")
def get_copy(copy_id: int, db: Session = Depends(get_db)):
    return _shape(_row(db, copy_id))


def _spec_clean(specs: list[dict]) -> list[dict]:
    out, seen = [], set()
    for sp in specs:
        name = " ".join(str(sp.get("name", "")).split())
        vals = []
        for v in sp.get("values", []) or []:
            v = " ".join(str(v).split())
            if v and v not in vals:
                vals.append(v)
        if not name and not vals:
            continue
        if not name:
            raise HTTPException(400, "An item specific has a value but no name.")
        if not vals:
            raise HTTPException(400, f"'{name}' has no value. Give it one or remove it.")
        if len(name) > 65 or any(len(v) > 65 for v in vals):
            raise HTTPException(400, f"'{name}' has a name or value over 65 characters (eBay's limit).")
        if name.lower() in seen:
            raise HTTPException(400, f"'{name}' appears twice.")
        seen.add(name.lower())
        out.append({"name": name, "values": vals[:30]})
    return out


@router.put("/{copy_id}")
def save_copy(copy_id: int, body: DraftIn, db: Session = Depends(get_db)):
    r = _row(db, copy_id)
    if r.status != "draft":
        raise HTTPException(409, "This copy is already listed on eBay. Edit the new listing from Live listings.")
    d = dict(r.data or {})
    if body.title is not None:
        t = " ".join(body.title.split())
        if not t:
            raise HTTPException(400, "The title cannot be empty.")
        if len(t) > 80:
            raise HTTPException(400, f"The title is {len(t)} characters; eBay allows 80.")
        d["title"] = t
    if body.description is not None:
        d["description"] = body.description
    if body.price is not None:
        d["price"] = str(_money(body.price))
    if body.photos is not None:
        allowed = set(d.get("original_photos", []))
        if not body.photos:
            raise HTTPException(400, "A listing needs at least one photo.")
        if any(u not in allowed for u in body.photos) or len(set(body.photos)) != len(body.photos):
            raise HTTPException(400, "A photo in the request is not one of the original's. Reload the page.")
        d["photos"] = body.photos
    if body.item_specifics is not None:
        d["item_specifics"] = _spec_clean(body.item_specifics)
    if body.compat is not None:
        d["compat"] = _compat_clean(body.compat)
        d["compat_edited"] = True  # from now on choosing a store does not rebuild the list from our fitment
    db.execute(
        text("update ebay_listing_copies set data = cast(:d as jsonb), updated_at = now() where id = :i"),
        {"d": json.dumps(d), "i": copy_id},
    )
    db.commit()
    return _shape(_row(db, copy_id))


@router.delete("/{copy_id}")
def delete_copy(copy_id: int, db: Session = Depends(get_db)):
    r = _row(db, copy_id)
    if r.status != "draft":
        raise HTTPException(409, "This copy is already listed on eBay, so the record is kept.")
    db.execute(text("delete from ebay_listing_copies where id = :i"), {"i": copy_id})
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# building the request
# --------------------------------------------------------------------------
def _product_label(db: Session, product_id: int) -> str:
    v = db.execute(text("select custom_label_sku from products where id = :p"), {"p": product_id}).scalar()
    return (str(v).strip() if v is not None else "")


def _quantity(db: Session, store: str, product_id: int) -> int:
    avail = db.execute(text(f"select {effective_sql('p')} from products p where p.id = :p"), {"p": product_id}).scalar()
    avail = int(avail or 0)
    if avail < 1:
        raise HTTPException(
            409,
            "The website shows nothing available to sell for this part (stock on hand minus parts set aside), and eBay "
            "needs at least 1 to list. Receive stock first. Nothing was sent.",
        )
    return min(avail, 9999)


def _ready(store: str, d: dict) -> None:
    """A draft started from a product cannot be checked or listed until a store has been chosen for it."""
    if not store:
        raise HTTPException(409, "Choose the store to list this in first. The category and shipping settings come from a listing there.")
    if not d.get("category_id"):
        raise HTTPException(409, "The draft has no category yet. Choose the store again, or pick a different model listing.")
    if _terms_problem(d):
        raise HTTPException(409, _terms_problem(d))
    if not _has_profiles(d) and not _has_terms(d):
        raise HTTPException(409, "The draft has no shipping and return rules yet. Choose the store again, or pick a different model listing.")


def _quantity_parts(db: Session, product_id: int) -> dict:
    """Where the quantity comes from: our shelf (on hand minus set aside) plus the drop-ship warehouses, when the rule counts them."""
    own = int(db.execute(text(f"select {available_sql('p')} from products p where p.id = :p"), {"p": product_id}).scalar() or 0)
    total = int(db.execute(text(f"select {effective_sql('p')} from products p where p.id = :p"), {"p": product_id}).scalar() or 0)
    try:
        counted = (db.execute(text("select value from availability_settings where key = 'use_distributor_stock'")).scalar() or "off") == "on"
    except Exception:
        db.rollback()
        counted = False
    return {"quantity_own": own, "quantity_distributor": max(total - own, 0), "distributor_counted": counted}


def _item_xml(d: dict, qty: int) -> str:
    price = _money(str(d.get("price") or ""))
    title = " ".join((d.get("title") or "").split())
    if not title or len(title) > 80:
        raise HTTPException(400, "The title must be 1 to 80 characters.")
    if not d.get("photos"):
        raise HTTPException(400, "A listing needs at least one photo.")
    cur = escape(d.get("currency") or "USD")
    parts = [
        f"<Title>{escape(title)}</Title>",
        f"<Description>{_cdata(d.get('description') or '')}</Description>",
        f"<PrimaryCategory><CategoryID>{escape(d['category_id'])}</CategoryID></PrimaryCategory>",
        f'<StartPrice currencyID="{cur}">{price}</StartPrice>',
        f"<Quantity>{qty}</Quantity>",
        "<ListingDuration>GTC</ListingDuration>",
        "<ListingType>FixedPriceItem</ListingType>",
        f"<Currency>{cur}</Currency>",
        "<Site>US</Site>",
    ]
    if d.get("sku"):
        parts.append(f"<SKU>{escape(d['sku'])}</SKU>")
    if d.get("condition_id"):
        parts.append(f"<ConditionID>{escape(d['condition_id'])}</ConditionID>")
    if d.get("condition_note"):
        parts.append(f"<ConditionDescription>{escape(d['condition_note'])}</ConditionDescription>")
    if d.get("country"):
        parts.append(f"<Country>{escape(d['country'])}</Country>")
    if d.get("location"):
        parts.append(f"<Location>{escape(d['location'])}</Location>")
    if d.get("postal_code"):
        parts.append(f"<PostalCode>{escape(d['postal_code'])}</PostalCode>")
    if d.get("dispatch_days"):
        parts.append(f"<DispatchTimeMax>{escape(d['dispatch_days'])}</DispatchTimeMax>")
    if _has_profiles(d):
        parts.append(
            "<SellerProfiles>"
            f"<SellerShippingProfile><ShippingProfileID>{escape(d['ship_profile'])}</ShippingProfileID></SellerShippingProfile>"
            f"<SellerPaymentProfile><PaymentProfileID>{escape(d['pay_profile'])}</PaymentProfileID></SellerPaymentProfile>"
            f"<SellerReturnProfile><ReturnProfileID>{escape(d['return_profile'])}</ReturnProfileID></SellerReturnProfile>"
            "</SellerProfiles>"
        )
    else:  # a store that has not opted into business policies: shipping and returns are written on the listing itself
        parts.append(_legacy_xml(d, d.get("currency") or "USD"))
    if d.get("best_offer"):
        parts.append("<BestOfferDetails><BestOfferEnabled>true</BestOfferEnabled></BestOfferDetails>")
        ld = ""
        if d.get("best_offer_accept"):
            ld += f'<BestOfferAutoAcceptPrice currencyID="{cur}">{escape(d["best_offer_accept"])}</BestOfferAutoAcceptPrice>'
        if d.get("best_offer_decline"):
            ld += f'<MinimumBestOfferPrice currencyID="{cur}">{escape(d["best_offer_decline"])}</MinimumBestOfferPrice>'
        if ld:
            parts.append(f"<ListingDetails>{ld}</ListingDetails>")
    if d.get("category2_id"):
        parts.append(f"<SecondaryCategory><CategoryID>{escape(d['category2_id'])}</CategoryID></SecondaryCategory>")
    if d.get("store_cat1") or d.get("store_cat2"):
        sc = ""
        if d.get("store_cat1"):
            sc += f"<StoreCategoryID>{escape(d['store_cat1'])}</StoreCategoryID>"
        if d.get("store_cat2"):
            sc += f"<StoreCategory2ID>{escape(d['store_cat2'])}</StoreCategory2ID>"
        parts.append(f"<Storefront>{sc}</Storefront>")
    pl = ""
    for tag, key in (("UPC", "upc"), ("EAN", "ean"), ("ISBN", "isbn")):
        if d.get(key):
            pl += f"<{tag}>{escape(d[key])}</{tag}>"
    if d.get("brand") and d.get("mpn"):
        pl += f"<BrandMPN><Brand>{escape(d['brand'])}</Brand><MPN>{escape(d['mpn'])}</MPN></BrandMPN>"
    if pl:
        parts.append(f"<ProductListingDetails>{pl}</ProductListingDetails>")
    parts.append("<PictureDetails>" + "".join(f"<PictureURL>{escape(u)}</PictureURL>" for u in d["photos"]) + "</PictureDetails>")
    specs = d.get("item_specifics") or []
    if specs:
        parts.append(
            "<ItemSpecifics>" + "".join(
                "<NameValueList><Name>" + escape(s["name"]) + "</Name>"
                + "".join(f"<Value>{escape(v)}</Value>" for v in s["values"]) + "</NameValueList>"
                for s in specs
            ) + "</ItemSpecifics>"
        )
    comp = d.get("compat") or []
    if comp:
        parts.append(
            "<ItemCompatibilityList>" + "".join(
                "<Compatibility>"
                + (f"<CompatibilityNotes>{escape(c['notes'])}</CompatibilityNotes>" if c.get("notes") else "")
                + "".join(f"<NameValueList><Name>{escape(n)}</Name><Value>{escape(v)}</Value></NameValueList>" for n, v in c["pairs"])
                + "</Compatibility>"
                for c in comp
            ) + "</ItemCompatibilityList>"
        )
    return "<Item>" + "".join(parts) + "</Item>"


def _fingerprint(store: str, xml: str) -> str:
    return hashlib.sha256((store + "|" + xml).encode("utf-8")).hexdigest()[:32]


def _live_on(db: Session, store: str) -> bool:
    return bool(db.execute(text("select live_pushes_enabled from ebay_accounts where store = :s"), {"s": store}).scalar())


def _log_safe(db, user, store, item_id, action, dry, request, response, ok, error):
    try:
        _log(db, user, store, item_id, action, dry, request, response, ok, error)
    except Exception:
        db.rollback()
        log.exception("could not write the push log for copy of %s", item_id)


# --------------------------------------------------------------------------
# preview (eBay checks it, nothing is listed) and list
# --------------------------------------------------------------------------
@router.post("/{copy_id}/preview")
def preview(copy_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    r = _row(db, copy_id)
    if r.status != "draft":
        raise HTTPException(409, "This copy is already listed.")
    d = dict(r.data or {})
    _ready(r.store, d)
    d["sku"] = _product_label(db, r.product_id) or d.get("sku", "")
    qty = _quantity(db, r.store, r.product_id)
    xml = _item_xml(d, qty)
    root = _trading(db, r.store, "VerifyAddFixedPriceItem", xml)
    errs = _errors(root)
    ok = not _failed(root)
    fees = []
    for f in root.findall(f"{NS}Fees/{NS}Fee"):
        amt = f.findtext(f"{NS}Fee") or "0"
        try:
            if Decimal(amt) > 0:
                fees.append({"name": f.findtext(f"{NS}Name") or "", "amount": amt})
        except Exception:
            pass
    _log_safe(db, user, r.store, r.source_item_id, "verify_add_fixed_price_item", True, {"xml": xml}, {"ack": root.findtext(f"{NS}Ack"), "errors": errs, "fees": fees}, ok, None if ok else _first_message(root))
    return {
        "ok": ok,
        "errors": [e for e in errs if e["severity"] == "Error"],
        "warnings": [e for e in errs if e["severity"] == "Warning"],
        "fees": fees,
        "quantity": qty,
        **_quantity_parts(db, r.product_id),
        "duplicates": _duplicates(db, r.store, " ".join((d.get("title") or "").split()), ""),
        "fingerprint": _fingerprint(r.store, xml) if ok else None,
        "live_pushes_enabled": _live_on(db, r.store),
        "sent": False,
    }


class ListIn(BaseModel):
    fingerprint: str


@router.post("/{copy_id}/list")
def list_it(copy_id: int, body: ListIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    r = _row(db, copy_id)
    if r.status != "draft":
        raise HTTPException(409, "This copy is already listed.")
    d = dict(r.data or {})
    _ready(r.store, d)
    if not _live_on(db, r.store):
        raise HTTPException(403, f"Live pushes are OFF for {r.store}. Turn them on in the Stores box first.")
    d["sku"] = _product_label(db, r.product_id) or d.get("sku", "")
    qty = _quantity(db, r.store, r.product_id)
    xml = _item_xml(d, qty)
    if _fingerprint(r.store, xml) != body.fingerprint:
        raise HTTPException(409, "The listing is not the same as in the preview (or the stock changed). Nothing was sent. Preview again.")

    root = _trading(db, r.store, "AddFixedPriceItem", xml)
    errs = _errors(root)
    ok = not _failed(root)
    new_id = (root.findtext(f"{NS}ItemID") or "").strip()
    _log_safe(db, user, r.store, new_id or r.source_item_id, "add_fixed_price_item", False, {"xml": xml}, {"ack": root.findtext(f"{NS}Ack"), "errors": errs, "item_id": new_id}, ok, None if ok else _first_message(root))
    if not ok:
        raise HTTPException(502, "eBay did not accept the new listing: " + _first_message(root))
    if not ITEM_RE.fullmatch(new_id):
        raise HTTPException(502, "eBay accepted the listing but did not give its item number. Look at the store on eBay and re-import the listings.")

    notes: list[str] = []
    try:
        _insert_link(db, new_id, r.product_id, r.store)
        db.commit()
        notes.append(f"Linked eBay item {new_id} to product {r.product_id}.")
    except Exception as e:
        db.rollback()
        log.exception("listed %s but could not link it to product %s", new_id, r.product_id)
        detail = getattr(e, "detail", None) or "an unexpected error"
        notes.append(f"The listing is live on eBay as item {new_id}, but linking it to product {r.product_id} failed ({detail}). Link it on the Link tab.")
    try:
        db.execute(
            text("update ebay_listing_copies set status = 'listed', new_item_id = :n, updated_at = now() where id = :i"),
            {"n": new_id, "i": copy_id},
        )
        db.commit()
    except Exception:
        db.rollback()
    return {"ok": True, "item_id": new_id, "warnings": [e for e in errs if e["severity"] == "Warning"], "notes": notes, "store": r.store}
