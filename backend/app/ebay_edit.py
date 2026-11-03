"""
Editing existing eBay listings (older "Trading" API) from the Live listings tab.

Mounted at /admin/ebay-edit/* (owner/admin only).

  GET  /item?store=&item_id=      the listing as it is on eBay right now
  POST /photo-upload              put a picture from the computer into the website's photo storage (nothing goes to eBay)
  POST /preview                   exactly what would be sent. NOTHING is sent. Logged as a dry run.
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
  * a quantity of 0 is refused (it can end the listing or drop a variation; out-of-stock handling comes later)
  * a push must carry the fingerprint of its preview; if the listing or the changes differ, it is refused
  * every preview and push is written to ebay_push_log
"""
import base64
import binascii
import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import images, photo_store
from app.auth import AuthUser, require_admin
from app.database import get_db
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

    reason = None
    if status and status != "Active":
        reason = f"This listing is {status}, not Active."
    elif listing_type not in ("FixedPriceItem", "StoresFixedPrice"):
        reason = f"This is a {listing_type or 'non fixed-price'} listing; only fixed-price listings can be edited here."
    elif has_var and any(not v["sku"] for v in variations):
        reason = "A variation on this listing has no SKU, so it cannot be addressed safely. Give each variation a SKU on eBay first."
    elif has_var and len({v["sku"] for v in variations}) != len(variations):
        reason = "Two variations on this listing share a SKU, so they cannot be told apart. Fix that on eBay first."

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
        "status": status,
        "url": t("ListingDetails/ViewItemURL"),
        "image_url": t("PictureDetails/GalleryURL") or t("PictureDetails/PictureURL"),
        "description": item.findtext(f"{NS}Description") or "",
        "has_variations": has_var,
        "variations": variations,
        "item_specifics": specifics_list,
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


@router.get("/item")
def get_item(store: str = Query(...), item_id: str = Query(...), db: Session = Depends(get_db)):
    return _read_item(db, store, item_id.strip())


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


class EditIn(BaseModel):
    store: str
    item_id: str
    changes: Changes


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


def _build(current: dict, ch: Changes) -> tuple[list[dict], list[dict]]:
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
                raise HTTPException(400, "A quantity of 0 is not allowed yet: it can end the listing. Out-of-stock handling comes in a later step.")
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

    if not parts:
        raise HTTPException(400, "Nothing is different from what is on eBay, so there is nothing to send.")
    xml = (
        '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents"><Item>'
        f"<ItemID>{current['item_id']}</ItemID>" + "".join(parts) + "</Item></ReviseFixedPriceItemRequest>"
    )
    return [{"call": "ReviseFixedPriceItem", "xml": xml}], diff


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
@router.post("/preview")
def preview(body: EditIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    row = _store_row(db, body.store)
    current = _read_item(db, row.store, body.item_id.strip())
    calls, diff = _build(current, body.changes)
    fp = _fingerprint(row.store, calls)
    _log(db, user, row.store, current["item_id"], "revise_fixed_price_item", True, {"calls": calls, "diff": diff}, {}, True, None)
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
    calls, diff = _build(current, body.changes)
    if _fingerprint(row.store, calls) != body.fingerprint:
        raise HTTPException(409, "The listing or the changes are not the same as in the preview. Nothing was sent. Preview again.")

    uploaded: dict[str, str] = {}
    try:
        for r in body.changes.photos or []:
            if r.key and r.key not in uploaded:
                uploaded[r.key] = _media_upload(db, row.store, r.key)
    except HTTPException as e:
        _log(db, user, row.store, current["item_id"], "revise_fixed_price_item", False, {"calls": calls, "diff": diff}, {"stage": "photo upload"}, False, str(e.detail)[:500])
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
        _log(db, user, row.store, current["item_id"], "revise_fixed_price_item", False, {"calls": calls, "diff": diff}, resp, ok, None if ok else _first_message(root))
        if not ok:
            raise HTTPException(502, "eBay did not accept the change: " + _first_message(root))
        warnings += [e for e in errs if e["severity"] == "Warning"]
    db.execute(
        text(
            "update channel_listings set last_pushed_at = now() "
            "where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i and store = :s"
        ),
        {"i": current["item_id"], "s": row.store},
    )
    db.commit()
    fresh = _read_item(db, row.store, current["item_id"])
    notes: list[str] = []
    if body.changes.photos is not None:
        try:
            notes = _record_photo_urls(db, row.store, current["item_id"], body.changes.photos, fresh)
        except Exception:  # eBay already has the change; a bookkeeping problem must not look like a failed push
            log.exception("could not record photo addresses for %s", current["item_id"])
            db.rollback()
            notes = ["The change is on eBay, but the photo addresses could not be recorded on the website."]
    return {"ok": True, "warnings": warnings, "item": fresh, "differences": _differences(fresh, body.changes), "notes": notes}


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
