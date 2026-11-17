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

from app.auth import require_admin
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


@router.post("/compat-probe")
def compat_probe(body: ProbeIn, db: Session = Depends(get_db)):
    """Ask eBay's Trading API for the listing WITH its compatibility list, using one of our connected stores' tokens. Read only."""
    item_id = body.item_id.strip()
    if not ITEM_RE.fullmatch(item_id):
        raise HTTPException(400, "That does not look like an eBay item number.")
    root = _trading(
        db, body.store.strip(), "GetItem",
        f"<ItemID>{item_id}</ItemID><DetailLevel>ReturnAll</DetailLevel>"
        "<IncludeItemSpecifics>true</IncludeItemSpecifics><IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>",
    )
    errs = _errors(root)
    if _failed(root):
        return {"ok": False, "ack": root.findtext(f"{NS}Ack") or "", "errors": errs, "compat_count": 0, "compat": [], "specifics": []}
    it = root.find(f"{NS}Item")
    if it is None:
        return {"ok": False, "ack": root.findtext(f"{NS}Ack") or "", "errors": errs or [{"code": "", "severity": "", "message": "eBay did not return the listing."}], "compat_count": 0, "compat": [], "specifics": []}
    seller = (it.findtext(f"{NS}Seller/{NS}UserID") or "").strip()
    row = db.execute(text("select seller_username from ebay_accounts where lower(store) = lower(:s)"), {"s": body.store.strip()}).first()
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
        "ack": root.findtext(f"{NS}Ack") or "",
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
        "compat": compat[:MAX_COMPAT_SHOWN],
        "compat_truncated": len(compat) > MAX_COMPAT_SHOWN,
    }
