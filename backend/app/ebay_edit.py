"""
Editing existing eBay listings (older "Trading" API) from the Live listings tab.

Mounted at /admin/ebay-edit/* (owner/admin only).

  GET  /item?store=&item_id=      the listing as it is on eBay right now, plus any edits saved on the website (draft)
  PUT  /draft                     save edits on the website only (nothing goes to eBay)
  DELETE /draft?store=&item_id=   throw the saved edits away
  POST /end                       end a live listing on eBay (needs the store's live-push switch ON)
  POST /set-zero                  set a live listing's quantity to 0 WITHOUT ending it (needs eBay's out-of-stock setting ON)
  GET/PUT /oos                    read / turn on eBay's out-of-stock preference for a store's account
  POST /restore-qty               take a listing off quantity 0: the website's stock on hand, or an override quantity
  GET  /zero/status, POST /zero/run   the zero-quantity keeper (resets listings before eBay's zero-days limit)
  POST /photo-upload              put a picture from the computer into the website's photo storage (nothing goes to eBay)
  POST /preview                   exactly what would be sent. NOTHING is sent. Logged as a dry run.
                                  (action "relist" = bring an ended listing back on eBay; the listing gets a new item number)
  POST /push                      sends it, but only if (a) the store's live-push switch is ON and
                                  (b) it is the very same request that was previewed
  PUT  /live-pushes               the per-store on/off switch (off by default)
  GET  /log?store=                the last pushes and previews

What can be changed: title, description (whole HTML replaced), price, available quantity and custom label (SKU)
on single listings; price and available quantity per variation on listings with variations.
Photos (single listings): remove, reorder, make main, add from the website's photo storage or by upload.
Item specifics (the whole list is replaced when sent) on single listings.
Condition and the parts compatibility list are shown as read only for now.

Safety rules built in:
  * the listing must belong to the connected account of the store (checked on every read)
  * only fixed-price listings can be edited; on variation listings only price/quantity per variation
  * a quantity of 0 in the edit form is refused; use /set-zero, which first checks eBay's out-of-stock setting
  * a push must carry the fingerprint of its preview; if the listing or the changes differ, it is refused
  * every preview and push is written to ebay_push_log
"""
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app import images, photo_store
from app.auth import AuthUser, require_admin
from app.database import get_db
from app.stock import available_sql
from app.ebay_oauth import TRADING_URL, _access, _expected_username, _store_row, get_user_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-edit", tags=["admin-ebay-edit"], dependencies=[Depends(require_admin)])

ITEM_RE = re.compile(r"\d{9,15}")
NS = "{urn:ebay:apis:eBLBaseComponents}"
KEY_RE = re.compile(r"[0-9a-f]{2}/[0-9a-f]{8,64}\.jpg")  # a stored photo's key, see app/photo_store.py
MEDIA_URL = "https://apim.ebay.com/commerce/media/v1_beta/image"
MAX_PHOTOS = 24
BAD_TOKEN_CODES = {"931", "932", "16110"}  # eBay: the token was not accepted -> get a fresh one once


# --------------------------------------------------------------------------
# talking to the Trading API
# --------------------------------------------------------------------------
def _trading(db: Session, store: str, call: str, inner: str) -> ET.Element:
    xml = f'<?xml version="1.0" encoding="utf-8"?><{call}Request xmlns="urn:ebay:apis:eBLBaseComponents">{inner}</{call}Request>'
    for attempt in (1, 2):
        token = get_user_token(db, store)
        req = urllib.request.Request(
            TRADING_URL,
            data=xml.encode("utf-8"),
            method="POST",
            headers={
                "X-EBAY-API-IAF-TOKEN": token,
                "X-EBAY-API-CALL-NAME": call,
                "X-EBAY-API-SITEID": "0",
                "X-EBAY-API-COMPATIBILITY-LEVEL": "1193",
                "Content-Type": "text/xml",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise HTTPException(502, f"eBay answered with an error ({e.code}).")
        except (urllib.error.URLError, TimeoutError):
            raise HTTPException(502, "Could not reach eBay.")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise HTTPException(502, "eBay's answer could not be read.")
        codes = {(e.findtext(f"{NS}ErrorCode") or "") for e in root.findall(f"{NS}Errors")}
        if attempt == 1 and codes & BAD_TOKEN_CODES:
            _access.pop(store, None)
            continue
        return root
    return root  # unreachable, keeps type checkers calm


def _errors(root: ET.Element) -> list[dict]:
    out = []
    for e in root.findall(f"{NS}Errors"):
        out.append(
            {
                "code": e.findtext(f"{NS}ErrorCode") or "",
                "severity": e.findtext(f"{NS}SeverityCode") or "",
                "message": e.findtext(f"{NS}LongMessage") or e.findtext(f"{NS}ShortMessage") or "",
            }
        )
    return out


def _failed(root: ET.Element) -> bool:
    return (root.findtext(f"{NS}Ack") or "") not in ("Success", "Warning")


def _first_message(root: ET.Element) -> str:
    errs = [e for e in _errors(root) if e["severity"] == "Error"] or _errors(root)
    return "; ".join(f"{e['message']} (eBay {e['code']})" for e in errs)[:500] or "eBay did not accept the request."


# --------------------------------------------------------------------------
# reading a listing
# --------------------------------------------------------------------------
def _read_item(db: Session, store: str, item_id: str) -> dict:
    if not ITEM_RE.fullmatch(item_id or ""):
        raise HTTPException(400, "That does not look like an eBay item number.")
    row = _store_row(db, store)
    root = _trading(
        db, row.store, "GetItem", f"<ItemID>{item_id}</ItemID><DetailLevel>ReturnAll</DetailLevel>"
        "<IncludeItemSpecifics>true</IncludeItemSpecifics><IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>"
    )
    if _failed(root):
        raise HTTPException(502, _first_message(root))
    item = root.find(f"{NS}Item")
    if item is None:
        raise HTTPException(502, "eBay did not return the listing.")

    def t(path: str) -> str:
        return (item.findtext("/".join(NS + p for p in path.split("/"))) or "").strip()

    seller = t("Seller/UserID")
    expected = _expected_username(row)
    if seller.lower() != expected.lower():
        raise HTTPException(403, f"This listing belongs to '{seller}', not to {row.store} ('{expected}'). Nothing was read further.")

    price_el = item.find(f"{NS}SellingStatus/{NS}CurrentPrice")
    price = price_el.text if price_el is not None else None
    currency = price_el.get("currencyID") if price_el is not None else None
    total = int(t("Quantity") or 0)
    sold = int(t("SellingStatus/QuantitySold") or 0)
    listing_type = t("ListingType")
    var_root = item.find(f"{NS}Variations")
    has_var = var_root is not None
    status = t("SellingStatus/ListingStatus")

    variations = []
    if has_var:
        for v in var_root.findall(f"{NS}Variation"):
            v_total = int(v.findtext(f"{NS}Quantity") or 0)
            v_sold = int(v.findtext(f"{NS}SellingStatus/{NS}QuantitySold") or 0)
            v_price = v.findtext(f"{NS}StartPrice")
            specifics = ", ".join(
                f"{(n.findtext(NS + 'Name') or '').strip()}: {(n.findtext(NS + 'Value') or '').strip()}"
                for n in v.findall(f"{NS}VariationSpecifics/{NS}NameValueList")
            )
            variations.append(
                {
                    "sku": (v.findtext(f"{NS}SKU") or "").strip(),
                    "price": v_price.strip() if v_price else None,
                    "quantity_available": max(v_total - v_sold, 0),
                    "quantity_sold": v_sold,
                    "specifics": specifics,
                }
            )
        # the listing-level numbers on eBay are not meaningful for variation listings; sum the variations
        total = sum(v["quantity_available"] + v["quantity_sold"] for v in variations)
        sold = sum(v["quantity_sold"] for v in variations)

    photos = [
        {"url": (p.text or "").strip()}
        for p in item.findall(f"{NS}PictureDetails/{NS}PictureURL")
        if (p.text or "").strip()
    ]

    specifics_list = []
    for n in item.findall(f"{NS}ItemSpecifics/{NS}NameValueList"):
        name = (n.findtext(f"{NS}Name") or "").strip()
        vals = [(x.text or "").strip() for x in n.findall(f"{NS}Value")]
        vals = [v for v in vals if v]
        if name:
            specifics_list.append({"name": name, "values": vals})

    compat_rows: list[dict] = []
    for c in item.findall(f"{NS}ItemCompatibilityList/{NS}Compatibility"):
        crow = {}
        for n in c.findall(f"{NS}NameValueList"):
            nm = (n.findtext(f"{NS}Name") or "").strip()
            if nm:
                crow[nm] = (n.findtext(f"{NS}Value") or "").strip()
        crow["Notes"] = (c.findtext(f"{NS}CompatibilityNotes") or "").strip()
        compat_rows.append(crow)
    try:
        compat_count = int(t("ItemCompatibilityCount") or len(compat_rows))
    except ValueError:
        compat_count = len(compat_rows)

    structural = None
    if listing_type not in ("FixedPriceItem", "StoresFixedPrice"):
        structural = f"This is a {listing_type or 'non fixed-price'} listing; only fixed-price listings can be edited here."
    elif has_var and any(not v["sku"] for v in variations):
        structural = "A variation on this listing has no SKU, so it cannot be addressed safely. Give each variation a SKU on eBay first."
    elif has_var and len({v["sku"] for v in variations}) != len(variations):
        structural = "Two variations on this listing share a SKU, so they cannot be told apart. Fix that on eBay first."
    ended = bool(status) and status != "Active"
    # an ended (Completed) fixed-price listing can be brought back with eBay's relist call; a scheduled one cannot
    relistable = ended and status in ("Completed", "Ended") and structural is None
    reason = f"This listing is {status}, not Active." if ended else structural
    if relistable:
        reason = None  # it can be edited and relisted, just not revised in place

    return {
        "store": row.store,
        "item_id": item_id,
        "title": t("Title"),
        "price": price,
        "currency": currency or "USD",
        "quantity_available": max(total - sold, 0),
        "quantity_sold": sold,
        "quantity_total": total,
        "sku": t("SKU"),
        "listing_type": listing_type,
        "duration": t("ListingDuration"),
        "status": status,
        "url": t("ListingDetails/ViewItemURL"),
        "image_url": t("PictureDetails/GalleryURL") or t("PictureDetails/PictureURL"),
        "description": item.findtext(f"{NS}Description") or "",
        "has_variations": has_var,
        "variations": variations,
        "item_specifics": specifics_list,
        "ended": ended,
        "relistable": relistable,
        "specifics_editable": reason is None and not has_var,
        "condition": {
            "id": t("ConditionID"),
            "name": t("ConditionDisplayName"),
            "note": t("ConditionDescription"),
        },
        "compatibility": {"count": compat_count, "rows": compat_rows[:300], "truncated": len(compat_rows) > 300},
        "photos": photos,
        "photos_editable": reason is None and not has_var,
        "editable": reason is None,
        "not_editable_reason": reason,
    }


def _read_draft(db: Session, store: str, item_id: str) -> Optional[dict]:
    """Edits saved on the website for this listing (None when there are none, or the table is not there yet)."""
    try:
        r = db.execute(
            text("select changes, updated_by, updated_at from ebay_listing_drafts where store = :s and item_id = :i"),
            {"s": store, "i": item_id},
        ).first()
    except Exception:
        db.rollback()
        return None
    if not r:
        return None
    return {"changes": r.changes, "updated_by": r.updated_by, "updated_at": r.updated_at.isoformat() if r.updated_at else None}


def _sync_listing_status(db: Session, item: dict) -> None:
    """The listing was just read from eBay itself, so make the status we show in the Listings list say the same
    (active, or ended for Completed/Ended). Other words eBay may use are left alone. Never raises."""
    try:
        said = (item.get("status") or "").strip().lower()
        word = "active" if said == "active" else "ended" if said in ("completed", "ended") else None
        if not word:
            return
        db.execute(
            text(
                "update channel_listings set status = :w "
                "where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i "
                "and lower(store) = lower(:s) and lower(coalesce(status, '')) <> :w"
            ),
            {"w": word, "i": item["item_id"], "s": item["store"]},
        )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not refresh the stored status of %s", item.get("item_id"))


def _product_label_for(db: Session, store: str, item_id: str) -> Optional[str]:
    """The custom label of the product this listing is linked to (the website's label is the one that counts)."""
    try:
        v = db.execute(
            text("select p.custom_label_sku from channel_listings cl join products p on p.id = cl.product_id "
                 "where regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = :i "
                 "and lower(coalesce(cl.store, '')) = lower(:s) limit 1"),
            {"i": item_id, "s": store},
        ).scalar()
    except Exception:
        db.rollback()
        return None
    v = (str(v).strip() if v is not None else "")
    return v or None


@router.get("/item")
def get_item(store: str = Query(...), item_id: str = Query(...), db: Session = Depends(get_db)):
    item = _read_item(db, store, item_id.strip())
    _sync_listing_status(db, item)
    _note_zero_state(db, item)
    item["draft"] = _read_draft(db, item["store"], item["item_id"])
    item["product_label"] = _product_label_for(db, item["store"], item["item_id"])
    return item


# --------------------------------------------------------------------------
# building the request
# --------------------------------------------------------------------------
class VarChange(BaseModel):
    sku: str
    price: Optional[str] = None
    quantity: Optional[int] = None  # AVAILABLE quantity of that variation


class PhotoRef(BaseModel):
    url: Optional[str] = None  # a picture already on the listing (its eBay address, as read from eBay)
    key: Optional[str] = None  # a picture in the website's storage, to be sent to eBay
    thumb: Optional[str] = None  # a small picture for the screen (kept in saved edits only; never sent to eBay)


class SpecIn(BaseModel):
    name: str
    values: list[str]


class Changes(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    price: Optional[str] = None
    quantity: Optional[int] = None  # the AVAILABLE quantity, as shown on the listing
    sku: Optional[str] = None
    variations: Optional[list[VarChange]] = None
    photos: Optional[list[PhotoRef]] = Field(default=None, max_length=60)  # the complete list, main photo first
    item_specifics: Optional[list[SpecIn]] = Field(default=None, max_length=100)  # the complete list


def _no_qty(ch: "Changes") -> "Changes":
    """The website owns stock, so a quantity sent from the edit form (an old saved edit, or any other caller) is ignored.
    Revise never changes the quantity; Relist takes it from the website's available stock (see _relist_quantity)."""
    upd: dict = {"quantity": None, "sku": None}  # the custom label comes from the product, never from an edit
    if ch.variations:
        upd["variations"] = [v.model_copy(update={"quantity": None}) if hasattr(v, "model_copy") else v.copy(update={"quantity": None}) for v in ch.variations]
    return ch.model_copy(update=upd) if hasattr(ch, "model_copy") else ch.copy(update=upd)


class EditIn(BaseModel):
    store: str
    item_id: str
    changes: Changes
    action: str = Field("revise", pattern="^(revise|relist)$")  # revise a live listing, or relist an ended one


class PushIn(EditIn):
    fingerprint: str


def _norm(s: str) -> str:
    return (s or "").replace("\r\n", "\n").strip()


def _money(raw: str) -> Decimal:
    try:
        v = Decimal(raw.strip().replace(",", "").lstrip("$")).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise HTTPException(400, "That price is not a number.")
    if v <= 0 or v > Decimal("99999.99"):
        raise HTTPException(400, "The price must be above 0 and below 100,000.")
    return v


def _cdata(html: str) -> str:
    return "<![CDATA[" + html.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _photos_part(current: dict, refs: list[PhotoRef]) -> tuple[str, dict]:
    """<PictureDetails> for the new list of photos, or ("", {}) when it is the list eBay already has.
    A picture from the website is written as a {{NEW:key}} marker; at send time it is first copied to
    eBay's picture service and the marker is replaced by the address eBay gives back."""
    if not current["photos_editable"]:
        raise HTTPException(409, "Photos cannot be changed on a listing with variations here yet.")
    have = [p["url"] for p in current["photos"]]
    if not refs:
        raise HTTPException(400, "A listing needs at least one photo.")
    if len(refs) > MAX_PHOTOS:
        raise HTTPException(400, f"A listing can have at most {MAX_PHOTOS} photos.")
    out: list[str] = []
    seen: set[str] = set()
    for r in refs:
        if bool(r.url) == bool(r.key):
            raise HTTPException(400, "Each photo needs either an eBay address or a stored key.")
        if r.url:
            if r.url not in have:
                raise HTTPException(400, "A photo in the request is not on the listing any more. Reload the listing.")
            ident = r.url
            token = escape(r.url)
        else:
            if not KEY_RE.fullmatch(r.key or ""):
                raise HTTPException(400, "That stored photo key is not valid.")
            ident = "key:" + r.key
            token = "{{NEW:" + r.key + "}}"
        if ident in seen:
            raise HTTPException(400, "The same photo is in the list twice.")
        seen.add(ident)
        out.append(token)
    new = [r for r in refs if r.key]
    kept = [r.url for r in refs if r.url]
    if not new and kept == have:
        return "", {}
    removed = len([u for u in have if u not in kept])
    bits = []
    if new:
        bits.append(f"{len(new)} new (copied to eBay's picture service when sent)")
    if removed:
        bits.append(f"{removed} removed")
    if kept != [u for u in have if u in kept]:
        bits.append("order changed")
    if not bits:
        bits.append("a new photo is now first")
    xml = "<PictureDetails>" + "".join(f"<PictureURL>{t}</PictureURL>" for t in out) + "</PictureDetails>"
    return xml, {"field": "Photos", "before": f"{len(have)} photos", "after": f"{len(refs)} photos", "note": ", ".join(bits)}


def _specifics_part(current: dict, specs: list[SpecIn]) -> tuple[str, list[dict]]:
    """<ItemSpecifics> for the new list, or ("", []) when it is the list eBay already has.
    eBay replaces the whole set, so everything that stays is sent again."""
    if not current["specifics_editable"]:
        raise HTTPException(409, "Item specifics cannot be changed on a listing with variations here yet.")
    clean: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for sp in specs:
        name = " ".join(sp.name.split())
        vals = []
        for v in sp.values:
            v = " ".join(v.split())
            if v and v not in vals:
                vals.append(v)
        if not name and not vals:
            continue  # an empty row the person never filled in
        if not name:
            raise HTTPException(400, "An item specific has a value but no name.")
        if len(name) > 65:
            raise HTTPException(400, f"The item specific name '{name[:30]}…' is longer than 65 characters.")
        if not vals:
            raise HTTPException(400, f"'{name}' has no value. Give it one or remove it.")
        if any(len(v) > 65 for v in vals):
            raise HTTPException(400, f"A value of '{name}' is longer than 65 characters (eBay's limit).")
        if len(vals) > 30:
            raise HTTPException(400, f"'{name}' has too many values.")
        if name.lower() in seen:
            raise HTTPException(400, f"'{name}' appears twice.")
        seen.add(name.lower())
        clean.append((name, vals))
    before = {sp["name"].lower(): sp for sp in current["item_specifics"]}
    after = {n.lower(): (n, v) for n, v in clean}
    diff: list[dict] = []
    for key, (n, v) in after.items():
        old = before.get(key)
        if old is None:
            diff.append({"field": f"Item specific: {n}", "before": None, "after": "; ".join(v), "note": "added"})
        elif old["values"] != v or old["name"] != n:
            diff.append({"field": f"Item specific: {n}", "before": "; ".join(old["values"]), "after": "; ".join(v)})
    for key, old in before.items():
        if key not in after:
            diff.append({"field": f"Item specific: {old['name']}", "before": "; ".join(old["values"]), "after": "(removed)"})
    if not diff:
        return "", []
    xml = "<ItemSpecifics>" + "".join(
        "<NameValueList><Name>" + escape(n) + "</Name>" + "".join(f"<Value>{escape(x)}</Value>" for x in v) + "</NameValueList>"
        for n, v in clean
    ) + "</ItemSpecifics>"
    return xml, diff


def _build(current: dict, ch: Changes, allow_empty: bool = False, call: str = "ReviseFixedPriceItem") -> tuple[list[dict], list[dict]]:
    """The exact request(s), plus the before/after rows. Raises 400/409 with a readable reason."""
    if not current["editable"]:
        raise HTTPException(409, current["not_editable_reason"])
    parts: list[str] = []
    diff: list[dict] = []
    has_var = current["has_variations"]

    if ch.title is not None and " ".join(ch.title.split()) != current["title"]:
        title = " ".join(ch.title.split())
        if not title or len(title) > 80:
            raise HTTPException(400, "A title must be 1 to 80 characters.")
        if current["quantity_sold"] > 0:
            raise HTTPException(400, "Something on this listing has already sold, so eBay will not allow the title to be changed.")
        parts.append(f"<Title>{escape(title)}</Title>")
        diff.append({"field": "Title", "before": current["title"], "after": title})

    if ch.description is not None and _norm(ch.description) != _norm(current["description"]):
        desc = ch.description.strip()
        if not desc:
            raise HTTPException(400, "The description cannot be empty.")
        if len(desc) > 500_000:
            raise HTTPException(400, "The description is too long (500,000 characters is eBay's limit).")
        parts.append(f"<Description>{_cdata(desc)}</Description>")
        diff.append(
            {
                "field": "Description",
                "before": f"{len(current['description']):,} characters",
                "after": f"{len(desc):,} characters",
                "note": "the whole description is replaced",
            }
        )

    if ch.photos is not None:
        pic_xml, pic_diff = _photos_part(current, ch.photos)
        if pic_xml:
            parts.append(pic_xml)
            diff.append(pic_diff)

    if ch.item_specifics is not None:
        sp_xml, sp_diff = _specifics_part(current, ch.item_specifics)
        if sp_xml:
            parts.append(sp_xml)
            diff += sp_diff

    if not has_var:
        if ch.price is not None and ch.price.strip():
            new_price = _money(ch.price)
            if current["price"] is None or Decimal(current["price"]).quantize(Decimal("0.01")) != new_price:
                parts.append(f'<StartPrice currencyID="{escape(current["currency"])}">{new_price}</StartPrice>')
                diff.append({"field": "Price", "before": current["price"], "after": str(new_price)})

        if ch.quantity is not None and ch.quantity != current["quantity_available"]:
            if ch.quantity < 1:
                raise HTTPException(400, "To make the quantity 0 use the Set 0 Qty button; it checks eBay's out-of-stock setting first so the listing does not end.")
            if ch.quantity > 9999:
                raise HTTPException(400, "That quantity is too large.")
            # eBay's Quantity counts what was already sold, so send available + sold.
            parts.append(f"<Quantity>{ch.quantity + current['quantity_sold']}</Quantity>")
            diff.append(
                {
                    "field": "Available quantity",
                    "before": str(current["quantity_available"]),
                    "after": str(ch.quantity),
                    "note": f"eBay is sent {ch.quantity + current['quantity_sold']} because {current['quantity_sold']} already sold",
                }
            )

        if ch.sku is not None and ch.sku.strip() != current["sku"]:
            sku = ch.sku.strip()
            if not sku or len(sku) > 50:
                raise HTTPException(400, "A custom label must be 1 to 50 characters.")
            parts.append(f"<SKU>{escape(sku)}</SKU>")
            diff.append({"field": "Custom label (SKU)", "before": current["sku"], "after": sku})
    else:
        by_sku = {v["sku"]: v for v in current["variations"]}
        seen: set[str] = set()
        var_xml = ""
        for vc in ch.variations or []:
            cur = by_sku.get(vc.sku)
            if cur is None:
                raise HTTPException(400, f"Variation '{vc.sku}' is not on this listing any more. Reload the listing.")
            if vc.sku in seen:
                raise HTTPException(400, f"Variation '{vc.sku}' appears twice in the request.")
            seen.add(vc.sku)
            inner = ""
            label = cur["specifics"] or vc.sku
            if vc.price is not None and vc.price.strip():
                new_price = _money(vc.price)
                if cur["price"] is None or Decimal(cur["price"]).quantize(Decimal("0.01")) != new_price:
                    inner += f'<StartPrice currencyID="{escape(current["currency"])}">{new_price}</StartPrice>'
                    diff.append({"field": f"Price, {label}", "before": cur["price"], "after": str(new_price)})
            if vc.quantity is not None and vc.quantity != cur["quantity_available"]:
                if vc.quantity < 1:
                    raise HTTPException(400, f"Variation '{label}': a quantity of 0 is not allowed yet; eBay may drop the variation from the listing.")
                if vc.quantity > 9999:
                    raise HTTPException(400, "That quantity is too large.")
                inner += f"<Quantity>{vc.quantity + cur['quantity_sold']}</Quantity>"
                diff.append(
                    {
                        "field": f"Available, {label}",
                        "before": str(cur["quantity_available"]),
                        "after": str(vc.quantity),
                        "note": f"eBay is sent {vc.quantity + cur['quantity_sold']} because {cur['quantity_sold']} already sold",
                    }
                )
            if inner:
                var_xml += f"<Variation><SKU>{escape(vc.sku)}</SKU>{inner}</Variation>"
        if var_xml:
            parts.append(f"<Variations>{var_xml}</Variations>")

    if not parts and not allow_empty:
        if has_var and ch.price is not None and ch.price.strip() and not ch.variations:
            raise HTTPException(400, "This listing has variations, so each variation has its own price. Change them on the listing's own edit screen.")
        raise HTTPException(400, "Nothing is different from what is on eBay, so there is nothing to send.")
    xml = (
        f'<{call}Request xmlns="urn:ebay:apis:eBLBaseComponents"><Item>'
        f"<ItemID>{current['item_id']}</ItemID>" + "".join(parts) + f"</Item></{call}Request>"
    )
    return [{"call": call, "xml": xml}], diff


def _plan(current: dict, ch: Changes, action: str) -> tuple[list[dict], list[dict]]:
    """The request(s) for either revising a live listing or relisting an ended one."""
    if action != "relist":
        if current["ended"]:
            raise HTTPException(409, f"This listing is {current['status']}, not live, so it cannot be revised. Use Relist to bring it back.")
        return _build(current, ch)
    if not current["relistable"]:
        raise HTTPException(409, current["not_editable_reason"] or f"This listing is {current['status']} and cannot be relisted.")
    # a relisted item starts fresh: nothing has sold on it yet, so quantities are sent as they are and a title change is allowed
    cur = dict(current, editable=True, quantity_sold=0, variations=[dict(v, quantity_sold=0) for v in current["variations"]])
    calls, diff = _build(cur, ch, allow_empty=True, call="RelistFixedPriceItem")
    if not current["has_variations"] and "<Quantity>" not in calls[0]["xml"]:
        qty = ch.quantity if ch.quantity is not None else current["quantity_available"]
        if qty < 1 or qty > 9999:
            raise HTTPException(400, "The website has no stock figure for this listing and eBay needs at least 1 to relist. Link it to a product and receive stock first. Nothing was sent.")
        calls[0]["xml"] = calls[0]["xml"].replace("</Item>", f"<Quantity>{qty}</Quantity></Item>", 1)
        diff.append({"field": "Available quantity", "before": str(current["quantity_available"]), "after": str(qty), "note": "sent so the relisted item has stock"})
    diff.insert(0, {"field": "Listing", "before": current["status"], "after": "Active on eBay", "note": "relisting gives the listing a new item number"})
    return calls, diff


def _pic_id(url: str) -> str:
    """eBay's id for a picture, so the same picture at another size compares equal."""
    m = re.search(r"/images/g/([^/]+)/", url) or re.search(r"/z/([^/]+)/", url)
    return m.group(1) if m else url.split("?")[0]


def _differences(fresh: dict, ch: Changes) -> list[str]:
    """After a push: what eBay now shows versus what was asked for. Empty list = exactly as sent."""
    out: list[str] = []
    if ch.title is not None and " ".join(ch.title.split()) != fresh["title"]:
        out.append(f"Title is now '{fresh['title']}'")
    if ch.description is not None and _norm(ch.description) != _norm(fresh["description"]):
        out.append("The description on eBay is not identical to what was sent (eBay may clean up HTML)")
    if ch.item_specifics is not None and not fresh["has_variations"]:
        want = {" ".join(sp.name.split()).lower(): [" ".join(v.split()) for v in sp.values if v.strip()] for sp in ch.item_specifics if sp.name.strip()}
        got = {sp["name"].lower(): sp["values"] for sp in fresh["item_specifics"]}
        for name, vals in want.items():
            if got.get(name) != vals:
                out.append(f"Item specific '{name}' is now: {'; '.join(got.get(name, [])) or '(missing)'}")
        for name in got:
            if name not in want:
                out.append(f"Item specific '{name}' is still on the listing")
    if ch.photos is not None and not fresh["has_variations"]:
        now = [p["url"] for p in fresh["photos"]]
        if len(now) != len(ch.photos):
            out.append(f"eBay now shows {len(now)} photos, {len(ch.photos)} were sent")
        else:
            for i, r in enumerate(ch.photos):
                if r.url and _pic_id(now[i]) != _pic_id(r.url):
                    out.append(f"Photo {i + 1} is not the one that was sent in that position")
                    break
    if not fresh["has_variations"]:
        if ch.price and ch.price.strip():
            try:
                if Decimal(fresh["price"] or "0") != _money(ch.price):
                    out.append(f"Price is now {fresh['price']}")
            except HTTPException:
                pass
        if ch.quantity is not None and ch.quantity != fresh["quantity_available"]:
            out.append(f"Available quantity is now {fresh['quantity_available']}")
        if ch.sku is not None and ch.sku.strip() != fresh["sku"]:
            out.append(f"Custom label is now '{fresh['sku']}'")
    else:
        by = {v["sku"]: v for v in fresh["variations"]}
        for vc in ch.variations or []:
            v = by.get(vc.sku)
            if v is None:
                out.append(f"Variation '{vc.sku}' is no longer on the listing")
                continue
            if vc.price and vc.price.strip():
                try:
                    if Decimal(v["price"] or "0") != _money(vc.price):
                        out.append(f"Variation '{vc.sku}' price is now {v['price']}")
                except HTTPException:
                    pass
            if vc.quantity is not None and vc.quantity != v["quantity_available"]:
                out.append(f"Variation '{vc.sku}' available is now {v['quantity_available']}")
    return out


def _fingerprint(store: str, calls: list[dict]) -> str:
    return hashlib.sha256((store + "|" + "|".join(c["call"] + c["xml"] for c in calls)).encode()).hexdigest()[:24]


def _log(db: Session, user: AuthUser, store: str, item_id: str, action: str, dry: bool, request: dict, response: dict, ok: bool, error: Optional[str]):
    pid = db.execute(
        text(
            "select product_id from channel_listings "
            "where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i limit 1"
        ),
        {"i": item_id},
    ).scalar()
    db.execute(
        text(
            """
            insert into ebay_push_log (user_email, store, item_id, product_id, api, action, dry_run, request, response, ok, error)
            values (:u, :s, :i, :p, 'trading', :a, :d, cast(:rq as jsonb), cast(:rs as jsonb), :ok, :er)
            """
        ),
        {
            "u": user.email or user.user_id,
            "s": store,
            "i": item_id,
            "p": pid,
            "a": action,
            "d": dry,
            "rq": json.dumps(request),
            "rs": json.dumps(response),
            "ok": ok,
            "er": error,
        },
    )
    db.commit()


# --------------------------------------------------------------------------
# photos: website storage -> eBay's picture service (Media API)
# --------------------------------------------------------------------------
def _media_upload(db: Session, store: str, key: str) -> str:
    """Has eBay fetch one stored photo from its public address and returns the eBay picture address.
    eBay does not accept a mix of its own and self-hosted pictures on one listing, so every new photo
    goes through here first. (The older UploadSiteHostedPictures call is gone since 30 Sep 2026.)"""
    src = images.full_url(key)
    if not src or not src.startswith("https://"):
        raise HTTPException(502, "The photo storage address is not an https address, which eBay requires.")
    payload = json.dumps({"imageUrl": src}).encode()
    for attempt in (1, 2):
        token = get_user_token(db, store)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
        req = urllib.request.Request(MEDIA_URL + "/create_image_from_url", data=payload, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
                location = r.headers.get("Location") or ""
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1:
                _access.pop(store, None)
                continue
            detail = e.read().decode("utf-8", "replace")[:300]
            raise HTTPException(502, f"eBay would not take a photo ({e.code}): {detail}")
        except (urllib.error.URLError, TimeoutError):
            raise HTTPException(502, "Could not reach eBay to upload a photo.")
        try:
            url = (json.loads(body or "{}").get("imageUrl") or "").strip()
        except ValueError:
            url = ""
        if not url and location.startswith(MEDIA_URL + "/"):
            try:
                greq = urllib.request.Request(location, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
                with urllib.request.urlopen(greq, timeout=30) as r:
                    url = (json.loads(r.read().decode("utf-8", "replace")).get("imageUrl") or "").strip()
            except (urllib.error.URLError, TimeoutError, ValueError):
                url = ""
        if not url.startswith("https://"):
            raise HTTPException(502, "eBay accepted a photo but did not return its address.")
        return url
    raise HTTPException(502, "eBay did not accept the photo upload.")  # unreachable


def _record_photo_urls(db: Session, store: str, item_id: str, refs: list[PhotoRef], fresh: dict) -> list[str]:
    """After a push: remember eBay's address for each photo we sent, in sent order, on the product linked to
    this listing, so the photo check in the import tab recognises them instead of importing them again."""
    notes: list[str] = []
    pid = db.execute(
        text(
            "select product_id from channel_listings "
            "where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i and store = :s limit 1"
        ),
        {"i": item_id, "s": store},
    ).scalar()
    now = [p["url"] for p in fresh["photos"]]
    sent = [(i, r.key) for i, r in enumerate(refs) if r.key]
    if not sent:
        return notes
    if not pid:
        return [f"{len(sent)} new photo(s) were sent, but this listing is not linked to a website product, so nothing was recorded."]
    missing = 0
    for i, key in sent:
        if i >= len(now):
            continue
        n = db.execute(
            text("update product_photos set source_url = :u where product_id = :p and image_key = :k"),
            {"u": now[i], "p": pid, "k": key},
        ).rowcount
        if not n:
            missing += 1
    db.commit()
    if missing:
        notes.append(f"{missing} new photo(s) are on the eBay listing but not on the website product.")
    return notes


class UploadIn(BaseModel):
    data: str = Field(..., max_length=21 * 1024 * 1024, description="the picture, base64 encoded")


@router.post("/photo-upload")
def photo_upload(body: UploadIn):
    """Stores a picture in the website's photo storage only. Nothing is sent to eBay here."""
    data = body.data
    if "," in data[:100]:
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "The picture could not be read. Try a different file.")
    try:
        p = photo_store.process_image(raw)
        photo_store.upload(p)
    except photo_store.PhotoError as e:
        raise HTTPException(400, str(e))
    except photo_store.StorageError as e:
        raise HTTPException(502, str(e))
    return {"key": p.key, "url": images.full_url(p.key), "thumb": images.thumb_url(p.key)}


# --------------------------------------------------------------------------
# preview and push
# --------------------------------------------------------------------------
_ACTION = {"revise": "revise_fixed_price_item", "relist": "relist_fixed_price_item"}


@router.post("/preview")
def preview(body: EditIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    row = _store_row(db, body.store)
    current = _read_item(db, row.store, body.item_id.strip())
    base = _no_qty(body.changes)
    changes = _relist_changes(db, row.store, current, _relist_quantity(db, row.store, current, base)) if body.action == "relist" else base
    calls, diff = _plan(current, changes, body.action)
    fp = _fingerprint(row.store, calls)
    _log(db, user, row.store, current["item_id"], _ACTION[body.action], True, {"calls": calls, "diff": diff}, {}, True, None)
    enabled = bool(
        db.execute(text("select live_pushes_enabled from ebay_accounts where store = :s"), {"s": row.store}).scalar()
    )
    return {
        "store": row.store,
        "item_id": current["item_id"],
        "calls": calls,
        "diff": diff,
        "fingerprint": fp,
        "live_pushes_enabled": enabled,
        "sent": False,
    }


@router.post("/push")
def push(body: PushIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    row = _store_row(db, body.store)
    enabled = db.execute(text("select live_pushes_enabled from ebay_accounts where store = :s"), {"s": row.store}).scalar()
    if not enabled:
        raise HTTPException(403, f"Live pushes are OFF for {row.store}. Turn them on in the Stores box first.")
    current = _read_item(db, row.store, body.item_id.strip())
    base = _no_qty(body.changes)
    changes = _relist_changes(db, row.store, current, _relist_quantity(db, row.store, current, base)) if body.action == "relist" else base
    calls, diff = _plan(current, changes, body.action)
    act = _ACTION[body.action]
    if _fingerprint(row.store, calls) != body.fingerprint:
        raise HTTPException(409, "The listing or the changes are not the same as in the preview. Nothing was sent. Preview again.")

    uploaded: dict[str, str] = {}
    try:
        for r in changes.photos or []:
            if r.key and r.key not in uploaded:
                uploaded[r.key] = _media_upload(db, row.store, r.key)
    except HTTPException as e:
        _log(db, user, row.store, current["item_id"], act, False, {"calls": calls, "diff": diff}, {"stage": "photo upload"}, False, str(e.detail)[:500])
        raise HTTPException(e.status_code, f"{e.detail} Nothing on the listing was changed.")

    warnings: list[dict] = []
    for c in calls:
        xml = c["xml"]
        for k, u in uploaded.items():
            xml = xml.replace("{{NEW:" + k + "}}", escape(u))
        inner = xml.split("<Item>", 1)[1].rsplit("</Item>", 1)[0]
        root = _trading(db, row.store, c["call"], f"<Item>{inner}</Item>")
        errs = _errors(root)
        ok = not _failed(root)
        resp = {"ack": root.findtext(f"{NS}Ack"), "errors": errs, "uploaded_photos": uploaded}
        _log(db, user, row.store, current["item_id"], act, False, {"calls": calls, "diff": diff}, resp, ok, None if ok else _first_message(root))
        if not ok:
            raise HTTPException(502, "eBay did not accept the change: " + _first_message(root))
        warnings += [e for e in errs if e["severity"] == "Warning"]
        new_id = (root.findtext(f"{NS}ItemID") or "").strip() if body.action == "relist" else ""
    live_id = current["item_id"]
    move_notes: list[str] = []
    if body.action == "relist":
        if not new_id or not ITEM_RE.fullmatch(new_id):
            raise HTTPException(502, "eBay accepted the relist but did not say the new item number. Look at the store on eBay and re-import the listings.")
        move_notes = _move_listing(db, row.store, current["item_id"], new_id)
        live_id = new_id
    else:
        db.execute(
            text(
                "update channel_listings set last_pushed_at = now() "
                "where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i and store = :s"
            ),
            {"i": current["item_id"], "s": row.store},
        )
        db.commit()
    _drop_draft(db, row.store, current["item_id"])  # what was saved is on eBay now
    fresh = _read_item(db, row.store, live_id)
    notes: list[str] = list(move_notes)
    if body.action == "relist" and fresh.get("duration") and fresh["duration"] != "GTC":
        notes.append(f"WARNING: the relisted item is not Good 'Til Cancelled (eBay shows '{fresh['duration']}'), so it will not renew by itself. Fix it on eBay.")
    if changes.photos is not None:
        try:
            notes += _record_photo_urls(db, row.store, live_id, changes.photos, fresh)
        except Exception:  # eBay already has the change; a bookkeeping problem must not look like a failed push
            log.exception("could not record photo addresses for %s", current["item_id"])
            db.rollback()
            notes.append("The change is on eBay, but the photo addresses could not be recorded on the website.")
    return {"ok": True, "warnings": warnings, "item": fresh, "differences": _differences(fresh, changes), "notes": notes}


def _drop_draft(db: Session, store: str, item_id: str) -> None:
    try:
        db.execute(text("delete from ebay_listing_drafts where store = :s and item_id = :i"), {"s": store, "i": item_id})
        db.commit()
    except Exception:
        db.rollback()


def _move_listing(db: Session, store: str, old_id: str, new_id: str) -> list[str]:
    """After a relist the listing has a new item number. Point everything that held the old number at the new one:
    the store link (channel_listings), the saved listing record (ebay_listings), the product's own eBay item
    number, and any quantity update still waiting for the old number. The old number is kept in ebay_item_aliases
    so orders placed on it (late orders, returns, a 90-day re-import) still find their product; the maintenance
    step in ebay_item_aliases.sql removes those rows after 6 months. Returns plain-language notes for anything
    that could not be moved. The column may hold a number or text with a prefix; the new value is written the
    way the old one was."""
    notes: list[str] = []
    row = db.execute(
        text(
            "select id, cast(external_id as text) as ext, product_id from channel_listings "
            "where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i and store = :s"
        ),
        {"i": old_id, "s": store},
    ).all()
    product_id = next((r.product_id for r in row if r.product_id), None)
    if row:
        kind = db.execute(
            text("select data_type from information_schema.columns where table_name = 'channel_listings' and column_name = 'external_id'")
        ).scalar()
        cast = "bigint" if kind in ("bigint", "integer", "smallint", "numeric") else "text"
        for r in row:
            value = re.sub(r"\d+$", new_id, r.ext or "")
            db.execute(
                text(f"update channel_listings set external_id = cast(:v as {cast}), status = 'active', last_pushed_at = now() where id = :id"),
                {"v": value, "id": r.id},
            )
        db.commit()
    else:
        notes.append(
            f"The relist worked, but no product was linked to the old item number {old_id}, so that link was not moved. "
            "Link the new listing to its product on the Link new eBay listings page."
        )

    # Each step below stands alone: if one cannot be done the others still are, and the relist itself is never undone.
    def step(label: str, sql: str, params: dict) -> int:
        try:
            with db.begin_nested():
                return db.execute(text(sql), params).rowcount or 0
        except Exception:
            log.exception("relist %s -> %s: could not update %s", old_id, new_id, label)
            notes.append(f"The listing is live as {new_id}, but its {label} could not be moved from the old number; check it on the website.")
            return -1

    saved = db.execute(
        text("select product_id from ebay_listings where ebay_item_id = :o and store = :s limit 1"), {"o": old_id, "s": store}
    ).first()
    if product_id is None and saved is not None:
        product_id = saved.product_id
    if saved is not None:
        n = step(
            "saved listing record",
            "update ebay_listings set ebay_item_id = :n, updated_at = now() "
            "where ebay_item_id = :o and store = :s and not exists (select 1 from ebay_listings where ebay_item_id = :n)",
            {"o": old_id, "n": new_id, "s": store},
        )
        if n == 0:
            notes.append(f"A saved listing record already exists for {new_id}, so the one under {old_id} was left as it was.")
    step(
        "product's eBay item number",
        "update products set ebay_item_id = regexp_replace(cast(ebay_item_id as text), '[0-9]+', :n) "
        "where regexp_replace(coalesce(cast(ebay_item_id as text), ''), '[^0-9]', '', 'g') = :o",
        {"o": old_id, "n": new_id},
    )
    step(
        "waiting quantity update",
        "update ebay_qty_queue set ebay_item_id = :n where store = :s and ebay_item_id = :o and status = 'pending'",
        {"o": old_id, "n": new_id, "s": store},
    )
    if product_id is not None:
        step(
            "old-number record",
            "insert into ebay_item_aliases (old_item_id, new_item_id, store, product_id) values (:o, :n, :s, :p) "
            "on conflict (old_item_id) do update set new_item_id = excluded.new_item_id, store = excluded.store, "
            "product_id = excluded.product_id, replaced_at = now()",
            {"o": old_id, "n": new_id, "s": store, "p": product_id},
        )
    db.commit()
    return notes


# --------------------------------------------------------------------------
# relists send the website's own photos (eBay only keeps an ended listing's pictures for about 90 days)
# --------------------------------------------------------------------------
_PHOTO_ORDER_COLS = ("position", "sort_order", "sort_index", "display_order", "sort", "ord", "idx", "rank", "id")


def _stored_photos(db: Session, store: str, item_id: str) -> list[dict]:
    """The photos the website holds for the product linked to this listing, main photo first:
    [{"key": stored key, "source_url": the eBay address it came from or was last sent as}]. [] when none."""
    try:
        pid = db.execute(
            text(
                "select product_id from channel_listings "
                "where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i and store = :s and product_id is not null limit 1"
            ),
            {"i": item_id, "s": store},
        ).scalar()
        if not pid:
            return []
        cols = {r[0] for r in db.execute(text("select column_name from information_schema.columns where table_name = 'product_photos'")).all()}
        order = next((c for c in _PHOTO_ORDER_COLS if c in cols), None)
        src = "source_url" if "source_url" in cols else "null"
        rows = db.execute(
            text(f"select image_key, {src} as source_url from product_photos where product_id = :p" + (f" order by {order}" if order else "")),
            {"p": pid},
        ).all()
        return [{"key": r.image_key, "source_url": r.source_url} for r in rows if r.image_key and KEY_RE.fullmatch(r.image_key)]
    except Exception:
        log.exception("could not read stored photos for %s", item_id)
        db.rollback()
        return []


def _relist_quantity(db: Session, store: str, current: dict, ch: Changes) -> Changes:
    """For a relist (the Relist button): the quantity comes from the WEBSITE, not from the ended listing. The website owns stock:
    the figure sent is what is available to sell (stock on hand minus parts set aside), up to 9999. A quantity the
    person typed that differs from what eBay showed on the ended listing is respected. A part with nothing available
    cannot be relisted (eBay needs at least 1), and the message says so. A listing with variations, or one not linked
    to a product, is left as it was."""
    if current["has_variations"]:
        return ch
    if ch.quantity is not None and ch.quantity != current["quantity_available"]:
        return ch  # typed on purpose
    avail = db.execute(
        text(
            f"select {available_sql('p')} from channel_listings cl join products p on p.id = cl.product_id "
            "where regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = :i and lower(cl.store) = lower(:s) limit 1"
        ),
        {"i": current["item_id"], "s": store},
    ).scalar()
    if avail is None:
        return ch  # not linked to a product: nothing to go by, keep the old behaviour
    avail = int(avail)
    if avail < 1:
        raise HTTPException(
            409,
            "The website shows nothing available to sell for this part (stock on hand minus parts set aside for open orders), "
            "and eBay needs at least 1 to relist. Receive stock first. Nothing was sent.",
        )
    q = min(avail, 9999)
    return ch.model_copy(update={"quantity": q}) if hasattr(ch, "model_copy") else ch.copy(update={"quantity": q})


def _relist_changes(db: Session, store: str, current: dict, ch: Changes) -> Changes:
    """For a relist: swap the pictures eBay would copy from the ended listing for the website's own copies.
    No photo list sent -> all the product's stored photos, in order. A list sent -> each eBay picture in it that
    matches a stored photo is replaced by that stored photo; anything that does not match is left as it is.
    No stored photos (or a listing with variations) -> unchanged, eBay copies its own."""
    if current["has_variations"]:
        return ch
    label = _product_label_for(db, store, current["item_id"])
    if label and label != current["sku"]:
        ch = ch.model_copy(update={"sku": label}) if hasattr(ch, "model_copy") else ch.copy(update={"sku": label})
    stored = _stored_photos(db, store, current["item_id"])
    if not stored:
        return ch
    by_pic = {_pic_id(p["source_url"]): p["key"] for p in stored if p["source_url"]}
    refs: list[PhotoRef] = []
    seen: set[str] = set()
    if ch.photos is None:
        wanted = [PhotoRef(key=p["key"]) for p in stored]
    else:
        wanted = []
        for r in ch.photos:
            if r.key:
                wanted.append(PhotoRef(key=r.key))
            elif r.url and _pic_id(r.url) in by_pic:
                wanted.append(PhotoRef(key=by_pic[_pic_id(r.url)]))
            else:
                wanted.append(r)
    for r in wanted:
        ident = r.key or r.url or ""
        if ident in seen:
            continue
        seen.add(ident)
        refs.append(r)
    refs = refs[:MAX_PHOTOS]
    if not refs:
        return ch
    return ch.model_copy(update={"photos": refs}) if hasattr(ch, "model_copy") else ch.copy(update={"photos": refs})


# --------------------------------------------------------------------------
# quantity 0 without ending the listing, and the zero-quantity keeper
# --------------------------------------------------------------------------
# eBay lets a listing sit at quantity 0 only while the account's "out of stock" preference is ON, and for a limited
# time (180 consecutive days on the pages we found; older sources say 90). The keeper bounces a listing to 1 and
# straight back to 0 before the limit. Settings are environment variables so the numbers are easy to change:
#   EBAY_ZERO_START_DAYS (default 150)  start trying this many days after the listing reached 0
#   EBAY_ZERO_RETRY_HOURS (default 20)  how long to wait before trying the same listing again
_zero_ready = False


def _ensure_zero_table(db: Session) -> None:
    global _zero_ready
    if _zero_ready:
        return
    db.execute(
        text(
            """
            create table if not exists ebay_zero_qty (
                store text not null,
                item_id text not null,
                zero_since timestamptz not null default now(),
                last_reset_at timestamptz,
                last_attempt_at timestamptz,
                bounce_open boolean not null default false,
                last_error text,
                primary key (store, item_id)
            )
            """
        )
    )
    # held = set to 0 on purpose (Set 0 Qty): sales of the product do not push stock onto it until the product's
    # stock on hand rises above held_onhand (see _hold_gate in ebay_sales.py)
    db.execute(text("alter table ebay_zero_qty add column if not exists held boolean not null default false"))
    db.execute(text("alter table ebay_zero_qty add column if not exists held_onhand integer"))
    db.commit()
    _zero_ready = True


def _note_zero_state(db: Session, item: dict) -> None:
    """Keep the zero-quantity list in step with what eBay shows: add a live single listing at 0 (the date it was first
    seen at 0), drop it once it has stock again or has ended. Never raises."""
    try:
        _ensure_zero_table(db)
        if item["has_variations"]:
            return
        if not item["ended"] and item["quantity_available"] == 0:
            db.execute(
                text("insert into ebay_zero_qty (store, item_id) values (:s, :i) on conflict (store, item_id) do nothing"),
                {"s": item["store"], "i": item["item_id"]},
            )
        else:
            db.execute(text("delete from ebay_zero_qty where store = :s and item_id = :i"), {"s": item["store"], "i": item["item_id"]})
        db.commit()
    except Exception:
        log.exception("could not update the zero-quantity list")
        db.rollback()


def _oos_preference(db: Session, store: str) -> Optional[bool]:
    """True/False = eBay's out-of-stock preference for the account; None when eBay would not say."""
    try:
        root = _trading(db, store, "GetUserPreferences", "<ShowOutOfStockControlPreference>true</ShowOutOfStockControlPreference>")
        if _failed(root):
            return None
        v = root.findtext(f"{NS}OutOfStockControlPreference")
        return None if v is None else v.strip().lower() == "true"
    except HTTPException:
        return None


def _set_qty(db: Session, store: str, item_id: str, qty: int) -> ET.Element:
    return _trading(db, store, "ReviseInventoryStatus", f"<InventoryStatus><ItemID>{item_id}</ItemID><Quantity>{qty}</Quantity></InventoryStatus>")


def _mark_held(db: Session, store: str, item_id: str) -> None:
    """Record the listing as held at 0 on purpose, with the product's stock on hand as the mark to rise above."""
    _ensure_zero_table(db)
    onhand = db.execute(
        text(
            "select coalesce(p.quantity_on_hand, 0) from channel_listings cl join products p on p.id = cl.product_id "
            "where regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = :i and cl.store = :s limit 1"
        ),
        {"i": item_id, "s": store},
    ).scalar()
    db.execute(
        text(
            "insert into ebay_zero_qty (store, item_id, zero_since, held, held_onhand) values (:s, :i, now(), true, :o) "
            "on conflict (store, item_id) do update set held = true, held_onhand = :o, bounce_open = false, last_error = null"
        ),
        {"s": store, "i": item_id, "o": onhand},
    )
    db.commit()


class SetZeroIn(BaseModel):
    store: str
    item_id: str = Field(..., max_length=20)
    oos_checked: bool = False  # true = the caller (a bulk run) already confirmed eBay's out-of-stock setting is ON for this store


@router.post("/set-zero")
def set_zero(body: SetZeroIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Set a live single listing's quantity to 0 WITHOUT ending it (the Set 0 Qty button)."""
    row = _store_row(db, body.store)
    enabled = db.execute(text("select live_pushes_enabled from ebay_accounts where store = :s"), {"s": row.store}).scalar()
    if not enabled:
        raise HTTPException(403, f"Live pushes are OFF for {row.store}. Turn them on in the Stores box first.")
    current = _read_item(db, row.store, body.item_id.strip())
    if current["ended"]:
        raise HTTPException(409, f"This listing is already {current['status']}; quantity 0 only applies to a live listing.")
    if current["has_variations"]:
        raise HTTPException(409, "Listings with variations are not supported here yet (each variation needs its own quantity).")
    if not current["editable"]:
        raise HTTPException(409, current["not_editable_reason"] or "This listing cannot be changed here.")
    if current["quantity_available"] == 0:  # already at 0 (sold down): nothing to send, but now it is held on purpose
        try:
            _mark_held(db, row.store, current["item_id"])
        except Exception:
            log.exception("could not record the hold for %s", current["item_id"])
            db.rollback()
        return {"ok": True, "item_id": current["item_id"], "already": True, "warnings": []}
    if not body.oos_checked and _oos_preference(db, row.store) is False:
        raise HTTPException(
            409,
            f"The out-of-stock setting is OFF in the {row.store} eBay account, so quantity 0 would not be allowed. Turn it on first "
            "(eBay: Account settings > Selling > Selling preferences). Nothing was sent.",
        )
    root = _set_qty(db, row.store, current["item_id"], 0)
    errs = _errors(root)
    ok = not _failed(root)
    _log(db, user, row.store, current["item_id"], "set_zero_qty", False, {"item_id": current["item_id"], "quantity": 0},
         {"ack": root.findtext(f"{NS}Ack"), "errors": errs}, ok, None if ok else _first_message(root))
    if not ok:
        raise HTTPException(502, "eBay did not accept quantity 0: " + _first_message(root))
    try:
        _mark_held(db, row.store, current["item_id"])
        db.execute(
            text("update ebay_zero_qty set zero_since = now() where store = :s and item_id = :i"),
            {"s": row.store, "i": current["item_id"]},
        )
        db.execute(
            text(
                "update channel_listings set last_pushed_at = now() "
                "where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i and store = :s"
            ),
            {"i": current["item_id"], "s": row.store},
        )
        db.commit()
    except Exception:
        log.exception("could not record the zero-quantity date for %s", current["item_id"])
        db.rollback()
    return {"ok": True, "item_id": current["item_id"], "warnings": [e for e in errs if e["severity"] == "Warning"]}


@router.get("/oos")
def oos_status(store: str = Query(...), db: Session = Depends(get_db)):
    """eBay's out-of-stock preference for the store's account: {"enabled": true | false | null}. null = eBay did not say."""
    row = _store_row(db, store)
    return {"store": row.store, "enabled": _oos_preference(db, row.store)}


class OosIn(BaseModel):
    store: str
    enabled: bool


@router.put("/oos")
def oos_set(body: OosIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Turn the out-of-stock preference on (or off) in the store's eBay account, then read it back to confirm."""
    row = _store_row(db, body.store)
    root = _trading(
        db, row.store, "SetUserPreferences",
        f"<OutOfStockControlPreference>{'true' if body.enabled else 'false'}</OutOfStockControlPreference>",
    )
    ok = not _failed(root)
    _log(db, user, row.store, "", "set_out_of_stock_pref", False, {"enabled": body.enabled},
         {"ack": root.findtext(f"{NS}Ack"), "errors": _errors(root)}, ok, None if ok else _first_message(root))
    if not ok:
        raise HTTPException(502, "eBay did not change the setting: " + _first_message(root))
    now = _oos_preference(db, row.store)
    if now is not None and now != body.enabled:
        raise HTTPException(502, "eBay accepted the request but still shows the setting as " + ("ON" if now else "OFF") + ". Change it in eBay: Account settings > Selling > Selling preferences.")
    return {"store": row.store, "enabled": now if now is not None else body.enabled}


class RestoreIn(BaseModel):
    store: str
    item_id: str = Field(..., max_length=20)
    qty: Optional[int] = Field(default=None, ge=1, le=9999)  # leave out = the website's available stock (on hand minus set aside); 1 = the "keep alive" quantity


@router.post("/restore-qty")
def restore_qty(body: RestoreIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Take a listing back off quantity 0 (the other half of Set 0 Qty). Without qty it uses the website's available stock (same figure the sales quantity push sends)."""
    row = _store_row(db, body.store)
    enabled = db.execute(text("select live_pushes_enabled from ebay_accounts where store = :s"), {"s": row.store}).scalar()
    if not enabled:
        raise HTTPException(403, f"Live pushes are OFF for {row.store}. Turn them on in the Stores box first.")
    current = _read_item(db, row.store, body.item_id.strip())
    if current["ended"]:
        raise HTTPException(409, f"This listing is {current['status']}; use Relist to bring it back.")
    if current["has_variations"]:
        raise HTTPException(409, "Listings with variations are not supported here yet.")
    if current["quantity_available"] > 0:
        _note_zero_state(db, current)
        return {"ok": True, "item_id": current["item_id"], "quantity": current["quantity_available"], "already": True}
    qty = body.qty
    if qty is None:
        qty = db.execute(
            text(
                f"select {available_sql('p')} from channel_listings cl join products p on p.id = cl.product_id "
                "where regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = :i and cl.store = :s limit 1"
            ),
            {"i": current["item_id"], "s": row.store},
        ).scalar()
        if not qty or qty < 1:
            raise HTTPException(409, "The website shows nothing available to sell for this part (stock on hand minus parts set aside for open orders), so it was left at 0. Receive stock first, or restore it with a quantity of 1.")
        qty = min(int(qty), 9999)
    root = _set_qty(db, row.store, current["item_id"], qty)
    ok = not _failed(root)
    _log(db, user, row.store, current["item_id"], "restore_qty", False, {"item_id": current["item_id"], "quantity": qty},
         {"ack": root.findtext(f"{NS}Ack"), "errors": _errors(root)}, ok, None if ok else _first_message(root))
    if not ok:
        raise HTTPException(502, "eBay did not accept the quantity: " + _first_message(root))
    try:
        _ensure_zero_table(db)
        db.execute(text("delete from ebay_zero_qty where store = :s and item_id = :i"), {"s": row.store, "i": current["item_id"]})
        db.execute(
            text(
                "update channel_listings set last_pushed_at = now() "
                "where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i and store = :s"
            ),
            {"i": current["item_id"], "s": row.store},
        )
        db.commit()
    except Exception:
        log.exception("could not clear the zero-quantity record for %s", current["item_id"])
        db.rollback()
    return {"ok": True, "item_id": current["item_id"], "quantity": qty}


def _reset_one(db: Session, store: str, item_id: str) -> str:
    """Bounce one listing 0 -> 1 -> 0 to restart eBay's zero-days count. Returns "" when done, else why not."""
    item = _read_item(db, store, item_id)
    if item["ended"] or item["has_variations"] or item["quantity_available"] > 0:
        db.execute(text("delete from ebay_zero_qty where store = :s and item_id = :i"), {"s": store, "i": item_id})
        db.commit()
        return ""  # nothing to keep alive any more
    up = _set_qty(db, store, item_id, 1)
    if _failed(up):
        return "could not raise quantity to 1: " + _first_message(up)
    db.execute(text("update ebay_zero_qty set bounce_open = true where store = :s and item_id = :i"), {"s": store, "i": item_id})
    db.commit()
    last = ""
    for attempt in range(4):  # back to 0 straight away; if eBay hiccups try again, a few seconds apart
        down = _set_qty(db, store, item_id, 0)
        if not _failed(down):
            db.execute(
                text("update ebay_zero_qty set zero_since = now(), last_reset_at = now(), bounce_open = false, last_error = null where store = :s and item_id = :i"),
                {"s": store, "i": item_id},
            )
            db.commit()
            return ""
        last = _first_message(down)
        time.sleep(3)
    return "quantity was raised to 1 but could not be set back to 0: " + last + " (will retry; check the listing on eBay)"


def run_zero_keeper(db: Session) -> dict:
    """One pass: reset every listing that has sat at 0 long enough, and finish any bounce left open. Safe to call often
    and from several servers: each listing is claimed with one UPDATE, so it is tried at most once per retry window."""
    _ensure_zero_table(db)
    start = int(os.environ.get("EBAY_ZERO_START_DAYS", "150"))
    retry = int(os.environ.get("EBAY_ZERO_RETRY_HOURS", "20"))
    due = db.execute(
        text(
            "select store, item_id, bounce_open from ebay_zero_qty "
            "where bounce_open or zero_since <= now() - make_interval(days => :d)"
        ),
        {"d": start},
    ).all()
    out = {"checked": len(due), "reset": 0, "failed": 0}
    for r in due:
        wait = "5 minutes" if r.bounce_open else f"{retry} hours"
        claimed = db.execute(
            text(
                "update ebay_zero_qty set last_attempt_at = now() where store = :s and item_id = :i "
                f"and (last_attempt_at is null or last_attempt_at < now() - interval '{wait}')"
            ),
            {"s": r.store, "i": r.item_id},
        ).rowcount
        db.commit()
        if not claimed:
            continue
        try:
            on = db.execute(text("select live_pushes_enabled from ebay_accounts where store = :s"), {"s": r.store}).scalar()
            if not on:
                continue
            why = _reset_one(db, r.store, r.item_id)
        except HTTPException as e:
            why = str(e.detail)
        except Exception as e:  # one bad listing must not stop the rest
            log.exception("zero keeper: %s", r.item_id)
            db.rollback()
            why = str(e)[:300]
        if why:
            out["failed"] += 1
            db.execute(text("update ebay_zero_qty set last_error = :e where store = :s and item_id = :i"), {"e": why[:500], "s": r.store, "i": r.item_id})
            db.commit()
        else:
            out["reset"] += 1
    return out


@router.post("/zero/run")
def zero_run(db: Session = Depends(get_db)):
    """Run the keeper once now (the same pass the background loop does)."""
    return run_zero_keeper(db)


@router.get("/zero/status")
def zero_status(db: Session = Depends(get_db)):
    """Every live listing the keeper is watching: when it reached 0, last reset, any problem."""
    _ensure_zero_table(db)
    start = int(os.environ.get("EBAY_ZERO_START_DAYS", "150"))
    rows = db.execute(
        text(
            "select store, item_id, zero_since, last_reset_at, last_attempt_at, bounce_open, last_error, held, "
            "floor(extract(epoch from (now() - zero_since)) / 86400)::int as days_at_zero "
            "from ebay_zero_qty order by zero_since"
        )
    ).all()
    return {
        "start_days": start,
        "items": [
            {
                "store": r.store, "item_id": r.item_id, "days_at_zero": r.days_at_zero,
                "zero_since": r.zero_since.isoformat() if r.zero_since else None,
                "last_reset_at": r.last_reset_at.isoformat() if r.last_reset_at else None,
                "bounce_open": r.bounce_open, "last_error": r.last_error, "held": r.held,
            }
            for r in rows
        ],
    }


def start_zero_keeper() -> None:
    """Background loop: one keeper pass about a minute after start-up, then every hour. Call once from main.py's start-up."""
    def loop() -> None:
        time.sleep(60)
        while True:
            gen = get_db()
            try:
                db = next(gen)
                res = run_zero_keeper(db)
                if res["reset"] or res["failed"]:
                    log.info("zero keeper: %s", res)
            except Exception:
                log.exception("zero keeper pass failed")
            finally:
                try:
                    gen.close()
                except Exception:
                    pass
            time.sleep(3600)

    threading.Thread(target=loop, name="ebay-zero-keeper", daemon=True).start()


class DraftIn(BaseModel):
    store: str
    item_id: str
    changes: Changes


@router.put("/draft")
def save_draft(body: DraftIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Save edits on the website only. Nothing is sent to eBay."""
    row = _store_row(db, body.store)
    item_id = body.item_id.strip()
    if not ITEM_RE.fullmatch(item_id):
        raise HTTPException(400, "That does not look like an eBay item number.")
    try:
        db.execute(
            text(
                "insert into ebay_listing_drafts (store, item_id, changes, updated_by, updated_at) "
                "values (:s, :i, cast(:c as jsonb), :u, now()) "
                "on conflict (store, item_id) do update set changes = excluded.changes, updated_by = excluded.updated_by, updated_at = now()"
            ),
            {"s": row.store, "i": item_id, "c": json.dumps(body.changes.model_dump(exclude_none=True)), "u": user.email or user.user_id},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(503, "Saved edits cannot be stored yet. Run sql/ebay_listing_drafts.sql in Supabase, then try again.")
    return {"ok": True, "draft": _read_draft(db, row.store, item_id)}


class BulkPriceIn(BaseModel):
    price: str
    listing_ids: list[int] = Field(min_length=1, max_length=2000)  # every listing whose new price is to be kept on the website
    unsent_ids: list[int] = Field(default_factory=list, max_length=2000)  # of those, the ones NOT on eBay (ended, or the push failed)
    set_website_price: bool = True  # also set the product's own (single-unit) website price; False leaves it alone


@router.post("/bulk-price")
def bulk_price(body: BulkPriceIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """The website side of the Listings tab's "Change price" button. The eBay side is the ordinary /preview and /push, one
    listing at a time; this is called afterwards. It sets the single-unit website price of every product behind the listings (the Listings
    tab shows the product's price; skipped when set_website_price is false), and for the listings that did NOT go to eBay (ended listings, or a push that failed)
    it keeps the new price as a saved edit, so it fills the price box when that listing is opened or relisted. Saved edits
    already there are kept; only their price is replaced. Never calls eBay."""
    price = _money(body.price)
    ids = list(dict.fromkeys(body.listing_ids))
    rows = db.execute(
        text(
            "select cl.id as listing_id, cl.store, regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') as item_id, cl.product_id "
            "from channel_listings cl where cl.id in :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    if not rows:
        raise HTTPException(404, "Those listings were not found.")
    product_ids = sorted({r.product_id for r in rows if r.product_id is not None})
    if product_ids and body.set_website_price:
        db.execute(
            text("update products set list_price = :p, updated_at = now() where id in :ids").bindparams(bindparam("ids", expanding=True)),
            {"p": price, "ids": product_ids},
        )
    db.commit()

    unsent = set(body.unsent_ids)
    who = user.email or user.user_id
    store_names: dict[str, str] = {}
    saved = 0
    note: Optional[str] = None
    for r in rows:
        if r.listing_id not in unsent or not ITEM_RE.fullmatch(r.item_id or ""):
            continue
        if r.store not in store_names:
            try:
                store_names[r.store] = _store_row(db, r.store).store
            except Exception:
                store_names[r.store] = r.store
        try:
            db.execute(
                text(
                    "insert into ebay_listing_drafts (store, item_id, changes, updated_by, updated_at) "
                    "values (:s, :i, cast(:c as jsonb), :u, now()) "
                    "on conflict (store, item_id) do update set "
                    "changes = coalesce(ebay_listing_drafts.changes, '{}'::jsonb) || excluded.changes, "
                    "updated_by = excluded.updated_by, updated_at = now()"
                ),
                {"s": store_names[r.store], "i": r.item_id, "c": json.dumps({"price": str(price)}), "u": who},
            )
            db.commit()
            saved += 1
        except Exception:
            db.rollback()
            note = "The website price was set, but the new price could not be saved as an edit on the listings that were not sent to eBay (run sql/ebay_listing_drafts.sql in Supabase, then change the price again)."
            break
    return {"ok": True, "price": str(price), "products_updated": len(product_ids) if body.set_website_price else 0, "saved_edits": saved, "note": note}


@router.delete("/draft")
def delete_draft(store: str = Query(...), item_id: str = Query(...), db: Session = Depends(get_db)):
    row = _store_row(db, store)
    _drop_draft(db, row.store, item_id.strip())
    return {"ok": True}


class EndIn(BaseModel):
    store: str
    item_id: str


@router.post("/end")
def end_listing(body: EndIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Ends a live fixed-price listing on eBay (the End listing button). It can be brought back with Relist."""
    row = _store_row(db, body.store)
    enabled = db.execute(text("select live_pushes_enabled from ebay_accounts where store = :s"), {"s": row.store}).scalar()
    if not enabled:
        raise HTTPException(403, f"Live pushes are OFF for {row.store}. Turn them on in the Stores box first.")
    current = _read_item(db, row.store, body.item_id.strip())  # also proves the listing is this store's
    if current["ended"]:
        raise HTTPException(409, f"This listing is already {current['status']}.")
    if current["listing_type"] not in ("FixedPriceItem", "StoresFixedPrice"):
        raise HTTPException(409, "Only fixed-price listings can be ended here.")
    request = {"call": "EndFixedPriceItem", "item_id": current["item_id"], "reason": "NotAvailable"}
    root = _trading(db, row.store, "EndFixedPriceItem", f"<ItemID>{current['item_id']}</ItemID><EndingReason>NotAvailable</EndingReason>")
    ok = not _failed(root)
    _log(db, user, row.store, current["item_id"], "end_fixed_price_item", False, request,
         {"ack": root.findtext(f"{NS}Ack"), "errors": _errors(root)}, ok, None if ok else _first_message(root))
    if not ok:
        raise HTTPException(502, "eBay did not end the listing: " + _first_message(root))
    db.execute(
        text(
            "update channel_listings set status = 'ended', last_pushed_at = now() "
            "where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i and store = :s"
        ),
        {"i": current["item_id"], "s": row.store},
    )
    db.commit()
    try:
        _ensure_zero_table(db)
        db.execute(text("delete from ebay_zero_qty where store = :s and item_id = :i"), {"s": row.store, "i": current["item_id"]})
        db.commit()
    except Exception:
        db.rollback()
    item = _read_item(db, row.store, current["item_id"])
    item["draft"] = _read_draft(db, row.store, current["item_id"])
    return {"ok": True, "item": item}


# --------------------------------------------------------------------------
# switch and log
# --------------------------------------------------------------------------
class SwitchIn(BaseModel):
    store: str
    enabled: bool


@router.put("/live-pushes")
def live_pushes(body: SwitchIn, db: Session = Depends(get_db)):
    row = _store_row(db, body.store)
    if body.enabled and not row.refresh_token_enc:
        raise HTTPException(409, f"{row.store} has not been approved yet.")
    db.execute(
        text(
            "update ebay_accounts set live_pushes_enabled = :e, "
            "sales_qty_push_enabled = case when :e then sales_qty_push_enabled else false end, "
            "updated_at = now() where store = :s"
        ),
        {"e": body.enabled, "s": row.store},
    )
    db.commit()
    return {"ok": True, "store": row.store, "live_pushes_enabled": body.enabled}


def _names_for(db: Session, emails: set[str]) -> dict[str, str]:
    """Display names for the people in the log, keyed by lower-cased email. Anyone not found simply keeps
    showing their email; a lookup problem must never break the log."""
    wanted = sorted({e.lower() for e in emails if e and "@" in e})
    if not wanted:
        return {}
    try:
        with db.begin_nested():  # a failed lookup must not poison the surrounding transaction
            rows = db.execute(
                text(
                    """
                    select lower(email) as e,
                           coalesce(nullif(trim(raw_user_meta_data->>'full_name'), ''),
                                    nullif(trim(raw_user_meta_data->>'name'), '')) as n
                    from auth.users
                    where lower(email) = any(cast(:e as text[]))
                    """
                ),
                {"e": wanted},
            ).all()
    except Exception:
        log.warning("could not look up display names for the push log", exc_info=True)
        return {}
    return {r.e: r.n for r in rows if r.n}


@router.get("/log")
def push_log(store: str = Query(...), item_id: Optional[str] = None, limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    row = _store_row(db, store)
    rows = db.execute(
        text(
            """
            select id, created_at, user_email, item_id, action, dry_run, ok, error, request
            from ebay_push_log
            where store = :s and (cast(:i as text) is null or item_id = :i)
            order by id desc limit :n
            """
        ),
        {"s": row.store, "i": (item_id or "").strip() or None, "n": limit},
    ).all()
    out = [dict(r._mapping) for r in rows]
    names = _names_for(db, {(r["user_email"] or "") for r in out})
    for r in out:
        r["user_name"] = names.get((r["user_email"] or "").lower())
    return {"store": row.store, "rows": out}
