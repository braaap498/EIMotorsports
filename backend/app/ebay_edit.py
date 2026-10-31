"""
Editing existing eBay listings (older "Trading" API) from the Live listings tab.

Mounted at /admin/ebay-edit/* (owner/admin only).

  GET  /item?store=&item_id=      the listing as it is on eBay right now
  POST /preview                   exactly what would be sent. NOTHING is sent. Logged as a dry run.
  POST /push                      sends it, but only if (a) the store's live-push switch is ON and
                                  (b) it is the very same request that was previewed
  PUT  /live-pushes               the per-store on/off switch (off by default)
  GET  /log?store=                the last pushes and previews

What can be changed in this first version: title, price, available quantity, custom label (SKU).
Descriptions, photos and item specifics are shown as read only for now.

Safety rules built in:
  * the listing must belong to the connected account of the store (checked on every read)
  * only fixed-price listings without variations can be edited
  * a quantity of 0 is refused (it can end the listing; out-of-stock handling comes later)
  * a push must carry the fingerprint of its preview; if the listing or the changes differ, it is refused
  * every preview and push is written to ebay_push_log
"""
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
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin
from app.database import get_db
from app.ebay_oauth import TRADING_URL, _access, _expected_username, _store_row, get_user_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-edit", tags=["admin-ebay-edit"], dependencies=[Depends(require_admin)])

ITEM_RE = re.compile(r"\d{9,15}")
NS = "{urn:ebay:apis:eBLBaseComponents}"
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
    has_var = item.find(f"{NS}Variations") is not None
    status = t("SellingStatus/ListingStatus")

    reason = None
    if status and status != "Active":
        reason = f"This listing is {status}, not Active."
    elif listing_type not in ("FixedPriceItem", "StoresFixedPrice"):
        reason = f"This is a {listing_type or 'non fixed-price'} listing; only fixed-price listings can be edited here."
    elif has_var:
        reason = "This listing has variations (several options in one listing). Those are not editable here yet."

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
        "description_chars": len(t("Description")),
        "editable": reason is None,
        "not_editable_reason": reason,
    }


@router.get("/item")
def get_item(store: str = Query(...), item_id: str = Query(...), db: Session = Depends(get_db)):
    return _read_item(db, store, item_id.strip())


# --------------------------------------------------------------------------
# building the request
# --------------------------------------------------------------------------
class Changes(BaseModel):
    title: Optional[str] = None
    price: Optional[str] = None
    quantity: Optional[int] = None  # the AVAILABLE quantity, as shown on the listing
    sku: Optional[str] = None


class EditIn(BaseModel):
    store: str
    item_id: str
    changes: Changes


class PushIn(EditIn):
    fingerprint: str


def _build(current: dict, ch: Changes) -> tuple[str, list[dict]]:
    """The exact request, plus the before/after rows. Raises 400 with a readable reason."""
    if not current["editable"]:
        raise HTTPException(409, current["not_editable_reason"])
    parts: list[str] = []
    diff: list[dict] = []

    if ch.title is not None and ch.title.strip() != current["title"]:
        title = " ".join(ch.title.split())
        if not title or len(title) > 80:
            raise HTTPException(400, "A title must be 1 to 80 characters.")
        parts.append(f"<Title>{escape(title)}</Title>")
        diff.append({"field": "Title", "before": current["title"], "after": title})

    if ch.price is not None and ch.price.strip():
        try:
            new_price = Decimal(ch.price.strip().replace(",", "").lstrip("$")).quantize(Decimal("0.01"))
        except InvalidOperation:
            raise HTTPException(400, "That price is not a number.")
        if new_price <= 0 or new_price > Decimal("99999.99"):
            raise HTTPException(400, "The price must be above 0 and below 100,000.")
        if current["price"] is None or Decimal(current["price"]).quantize(Decimal("0.01")) != new_price:
            parts.append(f'<StartPrice currencyID="{escape(current["currency"])}">{new_price}</StartPrice>')
            diff.append({"field": "Price", "before": current["price"], "after": str(new_price)})

    if ch.quantity is not None and ch.quantity != current["quantity_available"]:
        if ch.quantity < 1:
            raise HTTPException(
                400,
                "A quantity of 0 is not allowed yet: it can end the listing. Out-of-stock handling comes in a later step.",
            )
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

    if not parts:
        raise HTTPException(400, "Nothing is different from what is on eBay, so there is nothing to send.")
    xml = (
        '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents"><Item>'
        f"<ItemID>{current['item_id']}</ItemID>" + "".join(parts) + "</Item></ReviseFixedPriceItemRequest>"
    )
    return xml, diff


def _fingerprint(store: str, xml: str) -> str:
    return hashlib.sha256(f"{store}|{xml}".encode()).hexdigest()[:24]


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
# preview and push
# --------------------------------------------------------------------------
@router.post("/preview")
def preview(body: EditIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    row = _store_row(db, body.store)
    current = _read_item(db, row.store, body.item_id.strip())
    xml, diff = _build(current, body.changes)
    fp = _fingerprint(row.store, xml)
    _log(db, user, row.store, current["item_id"], "revise_fixed_price_item", True, {"xml": xml, "diff": diff}, {}, True, None)
    enabled = bool(
        db.execute(text("select live_pushes_enabled from ebay_accounts where store = :s"), {"s": row.store}).scalar()
    )
    return {
        "store": row.store,
        "item_id": current["item_id"],
        "call": "ReviseFixedPriceItem (Trading API)",
        "diff": diff,
        "xml": xml,
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
    xml, diff = _build(current, body.changes)
    if _fingerprint(row.store, xml) != body.fingerprint:
        raise HTTPException(409, "The listing or the changes are not the same as in the preview. Nothing was sent. Preview again.")

    inner = xml.split("<Item>", 1)[1].rsplit("</Item>", 1)[0]
    root = _trading(db, row.store, "ReviseFixedPriceItem", f"<Item>{inner}</Item>")
    errs = _errors(root)
    ok = not _failed(root)
    resp = {"ack": root.findtext(f"{NS}Ack"), "errors": errs}
    _log(db, user, row.store, current["item_id"], "revise_fixed_price_item", False, {"xml": xml, "diff": diff}, resp, ok, None if ok else _first_message(root))
    if not ok:
        raise HTTPException(502, "eBay did not accept the change: " + _first_message(root))
    db.execute(
        text(
            "update channel_listings set last_pushed_at = now() "
            "where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i and store = :s"
        ),
        {"i": current["item_id"], "s": row.store},
    )
    db.commit()
    fresh = _read_item(db, row.store, current["item_id"])
    return {"ok": True, "warnings": [e for e in errs if e["severity"] == "Warning"], "item": fresh}


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
        text("update ebay_accounts set live_pushes_enabled = :e, updated_at = now() where store = :s"),
        {"e": body.enabled, "s": row.store},
    )
    db.commit()
    return {"ok": True, "store": row.store, "live_pushes_enabled": body.enabled}


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
    return {"store": row.store, "rows": [dict(r._mapping) for r in rows]}
