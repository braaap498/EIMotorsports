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

  POST /fitment/preview         {product_id, item_id?, store?, from_listings}  what the eBay rows would add to ONE product's website fitment,
                                matched to our makes / models / years. Writes nothing.
  POST /fitment/settle          {decisions}  settle machines our vehicle list does not know (map to a model, create make/model, skip).
  POST /fitment/commit          {product_id, item_id?, store?, from_listings, picks}  add exactly the years that were ticked in the review.
  GET  /catalog/search?q=&gtin=&mpn=   eBay Catalog API: find the catalog product (ePID) for a part number or UPC.
  POST /catalog/compat          {epid | upc}  Metadata API getProductCompatibilities: the catalog product's compatibility table. Read only.
  POST /make-draft              a draft listing from one of our products (a separate step from the fitment review).

Why two calls: the Browse API needs no seller login and works on any listing, but is not documented to return the compatibility
table. The Trading call is documented to return it, and is known to work on our own listings; whether it works on another seller's
listing is what the probe shows.
"""
import base64
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin
from app.database import get_db
from app.ebay_edit import NS, _errors, _failed, _trading
from app.ebay_feedback import TOKEN_URL, FeedbackError, _open, _token, configured

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


# --------------------------------------------------------------------------
# eBay's CATALOG: a catalog product (ePID) has its own compatibility table, apart from any seller's listing
#   Catalog API   GET  /commerce/catalog/v1_beta/product_summary/search   part number / UPC / words -> catalog products and their ePID
#   Metadata API  POST /sell/metadata/v1/compatibilities/get_product_compatibilities   ePID or UPC -> the compatibility table
# Both are read with the app token (no seller login). Read only.
# --------------------------------------------------------------------------
CATALOG = "https://api.ebay.com/commerce/catalog/v1_beta"
METADATA = "https://api.ebay.com/sell/metadata/v1"
_API_SCOPE = "https://api.ebay.com/oauth/api_scope"
_CATALOG_SCOPE = "https://api.ebay.com/oauth/api_scope/commerce.catalog.readonly"
MARKETPLACE = "EBAY_US"                # catalog search
COMPAT_MARKETPLACE = "EBAY_MOTORS_US"  # parts compatibility exists only on eBay Motors; EBAY_US is refused (eBay 47006)


def _scoped_token(scopes: str) -> str:
    cred = f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}".encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": scopes}).encode(),
        method="POST",
        headers={"Authorization": "Basic " + base64.b64encode(cred).decode(), "Content-Type": "application/x-www-form-urlencoded"},
    )
    tok = _open(req).get("access_token")
    if not tok:
        raise FeedbackError("eBay did not return an access token.")
    return tok


def _catalog_token() -> str:
    """The Catalog API can ask for its own read scope; if eBay will not grant it to this app, fall back to the plain one."""
    try:
        return _scoped_token(f"{_API_SCOPE} {_CATALOG_SCOPE}")
    except FeedbackError:
        return _token()


def _call(method: str, url: str, token: str, params: Optional[dict] = None, body: Optional[dict] = None, marketplace: str = MARKETPLACE) -> dict:
    """One call to a catalog or metadata endpoint. A refusal becomes a readable message that includes eBay's own reason."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace, "Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", "replace")
        why = ""
        try:
            errs = json.loads(body_txt).get("errors") or []
            why = "; ".join(f"{x.get('message', '')} (eBay {x.get('errorId', '')})" for x in errs)[:400]
        except ValueError:
            why = body_txt[:200]
        log.warning("eBay %s %s -> %s %s", method, url.split("?")[0], e.code, why)
        raise HTTPException(502, f"eBay answered with an error ({e.code}). {why}".strip())
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise HTTPException(502, "Could not reach eBay.")


def _epids_from_browse(q: str, mpn: str, gtin: str, category: str = "") -> list[dict]:
    """Fallback when the Catalog API is closed to us: live listings of this part carry the ePID eBay matched them to. Most common first."""
    params: dict = {"limit": "50"}
    if gtin:
        params["gtin"] = gtin
    else:
        params["q"] = mpn or q
    # Stay in the listing's own category: a bare part number like 56-132 also matches trading cards and anything else numbered that way.
    # If that category turns up no catalog product, try once without it.
    data: dict = {}
    for cat in ([category] if category else []) + [""]:
        p_ = dict(params)
        if cat:
            p_["category_ids"] = cat
        try:
            data = _browse_get("/item_summary/search", p_)
        except HTTPException:
            return []
        if any(i.get("epid") for i in data.get("itemSummaries") or []):
            break
    tally: dict[str, dict] = {}
    for it in data.get("itemSummaries") or []:
        e = str(it.get("epid") or "").strip()
        if not e:
            continue
        t = tally.setdefault(e, {"epid": e, "title": it.get("title") or "", "image": (it.get("image") or {}).get("imageUrl"), "brand": None, "mpn": [mpn] if mpn else [], "_n": 0})
        t["_n"] += 1
    ranked = sorted(tally.values(), key=lambda t: -t["_n"])[:5]
    # the top product is "clear" when strictly more listings carry it than the runner-up: the page reads it without a click
    if ranked:
        ranked[0]["best"] = len(ranked) == 1 or ranked[0]["_n"] > ranked[1]["_n"]
    for t in ranked:
        t["matches"] = t.pop("_n", 0)
    return ranked


@router.get("/catalog/search")
def catalog_search(q: str = Query("", max_length=120), gtin: str = Query("", max_length=20), mpn: str = Query("", max_length=60), category: str = Query("", max_length=20)):
    """Find eBay catalog products (and their ePID) for a part number, UPC or words."""
    if not configured():
        raise HTTPException(409, "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API.")
    q, mpn, gtin = q.strip(), mpn.strip(), re.sub(r"\D", "", gtin)
    if not (q or mpn or gtin):
        raise HTTPException(400, "Give a part number, UPC or words to find the catalog product.")
    params: dict = {"limit": "10"}
    if gtin:
        params["gtin"] = gtin
    if mpn:
        params["mpn"] = mpn
    if q and not (gtin or mpn):
        params["q"] = q
    note = ""
    out = []
    data: dict = {}
    try:
        data = _call("GET", f"{CATALOG}/product_summary/search", _catalog_token(), params=params)
    except FeedbackError as e:
        raise HTTPException(502, str(e))
    except HTTPException as e:
        if not _refused(e):
            raise
        # The Catalog API refused this app (403 / eBay 1100: the keys are not approved for it). Do not stop there: ask the Browse API
        # for live listings of the same part number / UPC and collect the catalog product (ePID) they are matched to.
        out = _epids_from_browse(q, mpn, gtin, category.strip() if re.fullmatch(r"\d+", category.strip()) else "")
        if not out:
            raise HTTPException(
                502,
                f"{e.detail} The Catalog API is not open to this app's keys, and no live listing for that part number or UPC carries an eBay catalog product either, "
                "so there is nothing to look up here. The listing's own Trading API table (above) is still read.",
            )
        note = "eBay's Catalog API refused this app, so these products were found from live listings of the same part number or UPC instead."
    for s_ in data.get("productSummaries") or []:
        if not s_.get("epid"):
            continue
        out.append({
            "epid": str(s_["epid"]),
            "title": s_.get("title") or "",
            "image": (s_.get("image") or {}).get("imageUrl"),
            "brand": s_.get("brand"),
            "mpn": s_.get("mpn") if isinstance(s_.get("mpn"), list) else ([s_["mpn"]] if s_.get("mpn") else []),
        })
    if note:
        return {"total": len(out), "products": out, "note": note}
    return {"total": int(data.get("total") or len(out)), "products": out}


def _pairs_in(node, notes: bool = False) -> list[tuple[str, str, bool]]:
    """Every {propertyName, propertyValue} pair anywhere under node, flagged when it sits under a notes container."""
    out: list[tuple[str, str, bool]] = []
    if isinstance(node, dict):
        if "propertyName" in node and ("propertyValue" in node or "value" in node):
            out.append((str(node["propertyName"]), str(node.get("propertyValue", node.get("value", ""))), notes))
        else:
            for k, v in node.items():
                out += _pairs_in(v, notes or "note" in str(k).lower())
    elif isinstance(node, list):
        for v in node:
            out += _pairs_in(v, notes)
    return out


def _refused(e: HTTPException) -> bool:
    """eBay said 403 / 'Access denied' (eBay 1100): the token used is not allowed to make this call."""
    d = str(e.detail)
    return "(403)" in d or "eBay 1100" in d


def _store_token_for_catalog(db: Session, store: str = "") -> str:
    """A seller token from one of our connected stores. The compatibility table call refuses the app-only token with
    'Access denied' (eBay 1100) for some catalog products, but accepts a connected seller's token."""
    from app.ebay_oauth import get_user_token

    if store.strip():
        return get_user_token(db, store.strip())
    rows = db.execute(text("select store from ebay_accounts where refresh_token_enc is not null order by store")).all()
    last: Optional[HTTPException] = None
    for r in rows:
        try:
            return get_user_token(db, r.store)
        except HTTPException as e:  # expired or not approved: try the next store
            last = e
    raise last or HTTPException(409, "No eBay store is connected, so there is no seller login to retry the catalog call with.")


PAGE_LIMIT = 1000        # rows asked per page; eBay's own default is 100, which is why only 100 rows used to come back
MAX_PAGES = 40           # safety stop (a listing never takes more than 3000 rows anyway)


def _total_in(data: dict) -> Optional[int]:
    """The total row count eBay declares, wherever it keeps it (paginationOutput.total or similar)."""
    for k, v in data.items():
        if k == "compatibilityDetails":
            continue
        if isinstance(v, dict) and ("page" in k.lower() or "pagin" in k.lower()):
            for kk, vv in v.items():
                if "total" in kk.lower() and "page" not in kk.lower() and str(vv).isdigit():
                    return int(vv)
        elif "total" in k.lower() and "page" not in k.lower() and str(v).isdigit():
            return int(v)
    return None


def _fetch_pages(token: str, ident: dict) -> tuple[list, dict, dict]:
    """Read EVERY page of a catalog product's compatibility table. eBay sends 100 rows unless told otherwise, so the request
    carries paginationInput {limit, offset} and keeps asking until the declared total is reached or a page comes back empty."""
    url_ = f"{METADATA}/compatibilities/get_product_compatibilities"
    limit = PAGE_LIMIT
    offset = 0
    details: list = []
    first: dict = {}
    declared: Optional[int] = None
    seen_first_rows: set[str] = set()
    pages = 0
    while pages < MAX_PAGES:
        body_ = {"productIdentifier": ident, "paginationInput": {"limit": limit, "offset": offset}}
        try:
            data = _call("POST", url_, token, body=body_, marketplace=COMPAT_MARKETPLACE)
        except HTTPException as e:
            # a page size eBay does not accept (400): fall back to its default size once, on the first page only
            if pages == 0 and limit != 100 and "(400)" in str(e.detail):
                limit = 100
                continue
            raise
        pages += 1
        if pages == 1:
            first = data
        page = data.get("compatibilityDetails") or []
        if not page:
            break
        sig = json.dumps(page[0], sort_keys=True)
        if sig in seen_first_rows:  # eBay ignored the offset and sent the same page again: stop rather than loop
            break
        seen_first_rows.add(sig)
        details += page
        t = _total_in(data)
        if t is not None:
            declared = t
        offset += len(page)
        if declared is not None and offset >= declared:
            break
        if declared is None and len(page) < min(limit, 100):  # no total sent: a short page is the last one
            break
    paging = {k: v for k, v in first.items() if k != "compatibilityDetails" and ("page" in k.lower() or "next" in k.lower() or "total" in k.lower())}
    return details, first, {"pages": pages, "declared_total": declared, "paging": paging, "limit": limit}


def _catalog_rows(epid: Optional[str], upc: Optional[str], db: Optional[Session] = None, store: str = "") -> dict:
    """The catalog product's compatibility table as rows of {Make, Model, Year ...}, ALL pages. Read only. Never raises for an empty
    table: the answer says what eBay sent, so an empty or odd answer can be seen rather than guessed at."""
    epid, upc = (epid or "").strip(), re.sub(r"\D", "", upc or "")
    if not epid and not upc:
        raise HTTPException(400, "An eBay catalog product (ePID) or a UPC is needed.")
    ident = {"epid": epid} if epid else {"upc": upc}
    try:
        try:
            details, first, info = _fetch_pages(_token(), ident)
        except HTTPException as e:
            # 403 / eBay 1100: the app-only token is not allowed this call. Retry once as a connected seller.
            if not (_refused(e) and db is not None):
                raise
            first_err = str(e.detail)
            try:
                seller = _store_token_for_catalog(db, store)
            except HTTPException as e_tok:
                raise HTTPException(
                    502,
                    f"eBay refused the app login for this catalog product ({first_err}), and no store login could be used to retry: {e_tok.detail}",
                )
            try:
                details, first, info = _fetch_pages(seller, ident)
            except HTTPException as e2:
                if _refused(e2):
                    raise HTTPException(
                        502,
                        f"eBay refused both logins for this catalog product's compatibility table. App login: {first_err} "
                        f"Store login ({store.strip() or 'first connected store'}): {e2.detail} "
                        "If the store login is refused too, either eBay limits this table to some catalog products, or the store was "
                        "connected without the sell.inventory permission this call accepts (reconnect it under Store API to test that).",
                    )
                raise
    except FeedbackError as e:
        raise HTTPException(502, str(e))
    rows = []
    for d in details:
        vals: dict[str, str] = {}
        notes: list[str] = []
        for n, v, is_note in _pairs_in(d):
            if is_note:
                if v:
                    notes.append(f"{n}: {v}" if n.lower() not in ("notes", "note", "fitmentcomments") else v)
            elif n and n not in vals:
                vals[n] = v
        if vals:
            rows.append({"values": vals, "notes": "; ".join(notes)[:500]})
    declared = info["declared_total"]
    return {
        "identifier": ident,
        "rows": rows,
        "declared_entries": len(details),
        "response_keys": sorted(first.keys()),
        "paging": info["paging"],
        "pages": info["pages"],
        "declared_total": declared,
        # False only when eBay says there are more rows than we collected
        "complete": declared is None or len(details) >= declared,
    }


class CatalogCompatIn(BaseModel):
    epid: Optional[str] = Field(None, max_length=30)
    upc: Optional[str] = Field(None, max_length=20)
    store: Optional[str] = Field(None, max_length=60)  # optional: which connected store's login to retry with


@router.post("/catalog/compat")
def catalog_compat(body: CatalogCompatIn, db: Session = Depends(get_db)):
    """The compatibility table of one eBay catalog product, as eBay's Metadata API gives it. Read only; saves nothing."""
    got = _catalog_rows(body.epid, body.upc, db, body.store or "")
    allrows = got["rows"]
    got["rows"] = allrows[:MAX_COMPAT_SHOWN]
    got["count"] = len(allrows)
    got["truncated"] = len(allrows) > MAX_COMPAT_SHOWN
    return got



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
    epid: Optional[str] = Field(None, max_length=30)           # also take the compatibility table of this eBay catalog product


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

    catalog_note = ""
    if body.epid:
        try:
            cat = _catalog_rows(body.epid, None, db, body.store)
            ref["compat_all"] = list(ref["compat_all"]) + cat["rows"]
            if not cat["rows"]:
                catalog_note = "eBay's catalog product has no compatibility rows."
        except HTTPException as e:
            catalog_note = f"The catalog table could not be read: {e.detail}"

    made = create(CreateIn(product_id=body.product_id), db=db, user=user)   # refuses (409) a product that already has eBay listings
    copy_id = int(made["id"])
    warnings: list[str] = list(made.get("warnings") or [])
    if catalog_note:
        warnings.append(catalog_note)

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
# eBay compatibility rows -> a product's WEBSITE fitment: preview, settle unknown machines, commit what was ticked
#
# Three separate calls so the person reviews everything by hand:
#   preview  reads the rows and shows, per model, the years that are NEW for the product and the years already on it.
#            Machines our vehicle list does not know are listed with a best guess. Nothing is written.
#   settle   saves the person's decisions for those machines (use an existing model / create the make and model with the
#            names they edited / skip), into the same decisions table the compat import uses. No fitment is added.
#   commit   adds ONLY the years the person ticked, and only years that are new for the product. Never deletes or changes.
# Making a listing is a different button and does not need any of this to have happened.
# --------------------------------------------------------------------------
class FitSource(BaseModel):
    product_id: int
    item_id: Optional[str] = Field(None, max_length=15)   # read this listing's table from eBay (needs store)
    store: Optional[str] = Field(None, max_length=60)
    from_listings: bool = False                            # use the saved compatibility tables of the product's OWN eBay listings
    epid: Optional[str] = Field(None, max_length=30)       # use the compatibility table of this eBay catalog product


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


def _unknown_machine(db: Session, make: str, model: str, subs: list[str]) -> dict:
    """For a machine we could not place: does the make exist, is the model name ambiguous, and our closest models of that make."""
    r = db.execute(
        text(
            """
            select exists (select 1 from vehicle_makes vm where fitment_key(vm.name) = fitment_key(cast(:mk as text))) as make_exists,
                   (select count(*) from vehicle_makes vm join vehicle_models vmo on vmo.make_id = vm.id
                     where fitment_key(vm.name) = fitment_key(cast(:mk as text)) and fitment_key(vmo.name) = fitment_key(cast(:mo as text))) as exact_n
            """
        ),
        {"mk": make, "mo": model},
    ).first()
    sugg = db.execute(
        text(
            """
            select * from (
              select vmo.id, vmo.name, vm.name as make,
                     greatest(
                       similarity(fitment_key(vmo.name), fitment_key(cast(:mo as text))),
                       case when length(fitment_key(vmo.name)) >= 3 and position(fitment_key(vmo.name) in fitment_key(cast(:mo as text))) > 0 then 0.8 else 0 end,
                       case when length(fitment_key(cast(:mo as text))) >= 3 and position(fitment_key(cast(:mo as text)) in fitment_key(vmo.name)) > 0 then 0.8 else 0 end,
                       case when exists (select 1 from unnest(cast(:subs as text[])) s where fitment_key(s) = fitment_key(vmo.name)) then 0.95 else 0 end
                     ) as score
              from vehicle_makes vm join vehicle_models vmo on vmo.make_id = vm.id
              where fitment_key(vm.name) = fitment_key(cast(:mk as text))) q
            where score >= 0.4 order by score desc, name limit 3
            """
        ),
        {"mk": make, "mo": model, "subs": subs[:50]},
    ).all()
    return {
        "make_exists": bool(r.make_exists),
        "ambiguous": int(r.exact_n or 0) > 1,
        "suggestions": [{"model_id": int(x.id), "name": x.name, "make": x.make, "score": round(float(x.score), 2)} for x in sugg],
    }


def _fitment_plan(db: Session, product_id: int, rows: list[dict]) -> dict:
    """What adding these eBay rows (attribute dicts with Make, Model, Year ...) to the product's fitment would do. Writes nothing.
    Spellings that normalise to the same machine are one line, and two eBay machines that map to the same model of ours are one line."""
    import datetime

    maxyear = datetime.date.today().year + 1
    spelled: dict[tuple[str, str], dict] = {}
    no_year = 0
    for a in rows:
        mk, mo = " ".join(str(a.get("Make") or "").split()), " ".join(str(a.get("Model") or "").split())
        y = str(a.get("Year") or "").strip()
        if not mk or not mo:
            continue
        if not _YEAR.fullmatch(y) or int(y) > maxyear:
            no_year += 1
            continue
        g = spelled.setdefault((mk, mo), {"years": set(), "subs": {}})
        g["years"].add(int(y))
        sub = " ".join(str(a.get("Submodel") or a.get("Trim") or "").split())
        if sub:
            g["subs"].setdefault(int(y), set()).add(sub)

    # one machine per normalised make + model (the normalising is done by the database so it is the same one the conversion uses)
    names = sorted(spelled)
    keys = []
    if names:
        keys = [
            (r.mk, r.ok)
            for r in db.execute(
                text("select fitment_key(a) as mk, fitment_key(b) as ok from unnest(cast(:a as text[]), cast(:b as text[])) with ordinality as t(a, b, n) order by n"),
                {"a": [n[0] for n in names], "b": [n[1] for n in names]},
            ).all()
        ]
    machines: dict[tuple[str, str], dict] = {}
    for (mk, mo), key in zip(names, keys):
        if not key[0] or not key[1]:
            continue
        m = machines.setdefault(key, {"make": mk, "model": mo, "years": set(), "subs": {}})
        m["years"] |= spelled[(mk, mo)]["years"]
        for y, v in spelled[(mk, mo)]["subs"].items():
            m["subs"].setdefault(y, set()).update(v)

    resolved: dict[int, dict] = {}
    unresolved, skipped = [], []
    for key, m in sorted(machines.items(), key=lambda kv: (kv[1]["make"].lower(), kv[1]["model"].lower())):
        model_id, skip = _resolve_model(db, m["make"], m["model"])
        if skip:
            skipped.append(f"{m['make']} {m['model']}")
            continue
        if model_id is None:
            subnames = sorted({s for v in m["subs"].values() for s in v})
            unresolved.append({"make": m["make"], "model": m["model"], "years": sorted(m["years"]), "subs": subnames[:12], **_unknown_machine(db, m["make"], m["model"], subnames)})
            continue
        r = resolved.setdefault(int(model_id), {"model_id": int(model_id), "years": set(), "subs": {}, "ebay": []})
        r["years"] |= m["years"]
        for y, v in m["subs"].items():
            r["subs"].setdefault(y, set()).update(v)
        r["ebay"].append(f"{m['make']} {m['model']}")

    models = []
    if resolved:
        ids = list(resolved)
        label = {r.id: (r.make, r.model) for r in db.execute(
            text("select vmo.id, vmo.name as model, vmk.name as make from vehicle_models vmo join vehicle_makes vmk on vmk.id = vmo.make_id where vmo.id = any(cast(:i as bigint[]))"),
            {"i": ids}).all()}
        have: dict[int, set[int]] = {}
        for r in db.execute(
            text("select v.model_id, v.year from product_fitment pf join vehicles v on v.id = pf.vehicle_id "
                 "where pf.product_id = :p and v.model_id = any(cast(:i as bigint[])) and v.year is not null"),
            {"p": product_id, "i": ids}).all():
            have.setdefault(int(r.model_id), set()).add(int(r.year))
        for mid, r in resolved.items():
            h = have.get(mid, set())
            new = sorted(y for y in r["years"] if y not in h)
            mk, mo = label.get(mid, ("", str(mid)))
            models.append({
                "model_id": mid, "make": mk, "model": mo, "ebay_names": sorted(set(r["ebay"])),
                "new_years": new, "already_years": sorted(y for y in r["years"] if y in h),
                "subs": {str(y): sorted(r["subs"].get(y, ())) for y in new if r["subs"].get(y)},
            })
        models.sort(key=lambda x: (x["make"].lower(), x["model"].lower()))
    unresolved.sort(key=lambda u: (-len(u["years"]), u["make"].lower(), u["model"].lower()))
    return {"models": models, "unresolved": unresolved, "skipped": skipped, "no_year": no_year}


def _fitment_apply(db: Session, product_id: int, chosen: list[dict]) -> dict:
    """Add the chosen years. Only ADDS: missing vehicle years for a model that already exists, and product_fitment links.
    Nothing is deleted or changed. chosen: [{model_id, years, subs}]."""
    vehicles_made = links = 0
    for m in chosen:
        for y in m["years"]:
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


def _fit_rows(db: Session, body: FitSource) -> tuple[list[dict], dict, str]:
    """The eBay rows for a fitment preview or commit, and what they came from. Reads eBay (and our saved tables); writes nothing."""
    pj = db.execute(text("select title, coalesce(is_universal, false) as u, coalesce(fitment_exempt, false) as x from products where id = :p"), {"p": body.product_id}).first()
    if not pj:
        raise HTTPException(404, "That product does not exist.")
    if pj.u or pj.x:
        raise HTTPException(409, "This product is marked universal or exempt from fitment, so no fitment is added to it.")
    if not body.item_id and not body.from_listings and not body.epid:
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
    if body.epid:
        cat = _catalog_rows(body.epid, None, db, (body.store or "").strip())
        rows += [c["values"] for c in cat["rows"]]
        info["from_catalog_rows"] = len(cat["rows"])
    if body.from_listings:
        saved, used, total = _saved_tables(db, body.product_id)
        rows += saved
        info.update({"own_listings_with_table": used, "own_listings": total})
    return rows, info, pj.title


@router.post("/fitment/preview")
def fitment_preview(body: FitSource, db: Session = Depends(get_db)):
    """Show what the eBay rows would add to the product's website fitment. Changes nothing. Rows can come from a listing read from eBay
    now, from the saved tables of the product's own listings, or both."""
    rows, info, title = _fit_rows(db, body)
    plan = _fitment_plan(db, body.product_id, rows)
    plan.update(info)
    plan["rows_read"] = len(rows)
    plan["product"] = title
    return plan


class FitDecision(BaseModel):
    ebay_make: str = Field(..., min_length=1, max_length=100)    # the machine as eBay spells it (what the decision is remembered under)
    ebay_model: str = Field(..., min_length=1, max_length=100)
    action: Literal["map", "create", "skip"]
    model_id: Optional[int] = None                                # map: our existing model
    make: str = Field("", max_length=100)                         # create: the make and model names to create, as edited in the review
    model: str = Field("", max_length=100)
    type_id: Optional[int] = None                                 # create: vehicle type of a NEW model


class SettleIn(BaseModel):
    decisions: list[FitDecision] = Field(..., min_length=1, max_length=200)


def _settle(db: Session, d: FitDecision) -> str:
    from app.admin_ebay_fitment import Decision, _create_model, _save_map

    eb_make, eb_model = " ".join(d.ebay_make.split()), " ".join(d.ebay_model.split())
    k = db.execute(text("select fitment_key(:a) as mk, fitment_key(:b) as ok"), {"a": eb_make, "b": eb_model}).first()
    if not k.mk or not k.ok:
        raise HTTPException(400, f"“{eb_make} {eb_model}” has no usable make and model.")
    # remembered under the eBay spelling, so this and every later import land on the same decision
    remembered = Decision(make_key=k.mk, model_key=k.ok, make=eb_make, model=eb_model, action="skip")
    if d.action == "skip":
        _save_map(db, remembered, None, True)
        return "skipped"
    if d.action == "map":
        if not d.model_id or not db.execute(text("select 1 from vehicle_models where id = :i"), {"i": d.model_id}).first():
            raise HTTPException(404, f"Choose one of our models for {eb_make} {eb_model}.")
        _save_map(db, remembered, d.model_id, False)
        return "mapped"
    make, model = " ".join(d.make.split()), " ".join(d.model.split())
    if not make or not model:
        raise HTTPException(400, f"A make and a model name are needed to create {eb_make} {eb_model}.")
    if d.type_id is not None and not db.execute(text("select 1 from vehicle_types where id = :i"), {"i": d.type_id}).first():
        raise HTTPException(404, "That vehicle type does not exist.")
    ck = db.execute(text("select fitment_key(:a) as mk, fitment_key(:b) as ok"), {"a": make, "b": model}).first()
    if not ck.mk or not ck.ok:
        raise HTTPException(400, f"“{make} {model}” has no usable make and model name.")
    mid = _create_model(db, Decision(make_key=ck.mk, model_key=ck.ok, make=make, model=model, action="create", type_id=d.type_id))
    _save_map(db, remembered, mid, False)
    return "created"


@router.post("/fitment/settle")
def fitment_settle(body: SettleIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Save the decisions made in the review for machines our vehicle list does not know. A made-up make or model is created only here,
    with the names the person edited, and the decision is remembered so the compat import agrees. No fitment is added by this call."""
    from app.admin_catalog import finish

    counts = {"mapped": 0, "created": 0, "skipped": 0}
    try:
        for d in body.decisions:
            counts[_settle(db, d)] += 1
        finish(db, refresh=counts["created"] > 0)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("could not save the fitment decisions")
        raise HTTPException(500, "Nothing was saved.")
    log.info("ebay lookup fitment decisions by %s: %s", user.email, counts)
    return counts


class PickIn(BaseModel):
    model_id: int
    years: list[int] = Field(..., min_length=1, max_length=200)


class FitCommitIn(FitSource):
    picks: list[PickIn] = Field(..., min_length=1, max_length=2000)


@router.post("/fitment/commit")
def fitment_commit(body: FitCommitIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Add the years that were ticked in the review to the product's website fitment. The rows are read again and matched again, and a
    ticked year is added only if it is still new for the product, so nothing already there is touched and nothing is removed."""
    from app.admin_catalog import finish

    rows, info, title = _fit_rows(db, body)
    plan = _fitment_plan(db, body.product_id, rows)
    wanted: dict[int, set[int]] = {}
    for p in body.picks:
        wanted.setdefault(p.model_id, set()).update(p.years)
    chosen = []
    for m in plan["models"]:
        ys = [y for y in m["new_years"] if y in wanted.get(m["model_id"], ())]
        if ys:
            chosen.append({"model_id": m["model_id"], "years": ys, "subs": m["subs"]})
    if not chosen:
        raise HTTPException(409, "None of the ticked years are new for this product any more, so nothing was added.")
    try:
        added = _fitment_apply(db, body.product_id, chosen)
        finish(db, refresh=True)
    except Exception:
        db.rollback()
        log.exception("could not add fitment to product %s", body.product_id)
        raise HTTPException(500, "The fitment could not be added, and nothing was changed.")
    log.info("ebay lookup fitment by %s: product %s %s", user.email, body.product_id, added)
    return {"product": title, "models": len(chosen), **added}
