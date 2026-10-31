"""
Live eBay listings: the back end of the admin "Live listings" tab.

Mounted at /admin/ebay-listings/* (owner/admin only).

  GET  /stores              the eBay stores the site knows about (never returns any token)
  POST /stores              add a store
  GET  /list                one page of a store's live listings, newest first, with what the
                            website knows about each one

THIS STEP IS READ ONLY. Nothing here changes anything on eBay. Editing, previews and pushes come
in the next steps.

How the list is read: eBay's public Browse API with the app's own keys (the same access the
"Link new eBay listings" tab uses). Until a store owner authorizes the app, that is all that can
be seen, so these limits apply for now:
  * only ACTIVE listings that are buyable show up (a listing at quantity 0 or ended is not listed)
  * eBay returns at most 10,000 results per search, so a search box narrows a big store down
  * quantity and custom label are not in this data; they arrive after authorization

The store box works like the other eBay tabs: blank means the main store, and the main store's
eBay user name is the API's EBAY_SELLER_USERNAME setting. A store added to ebay_accounts can have
its own eBay user name; if it does not, the store name itself is used as the user name.
"""
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db
from app.ebay_feedback import SEARCH_URL, FeedbackError, _open, _token, configured, seller_username

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-listings", tags=["admin-ebay-listings"], dependencies=[Depends(require_admin)])

MAIN_STORE = "eandimotorsports"  # the store key the existing 10,217 links were labelled with
SELLER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
SORTS = {"newest": "newlyListed", "price_low": "price", "price_high": "-price"}
MAX_RESULTS = 10_000  # eBay's search never goes past this
_DIGITS = "regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g')"


def _category_ids() -> str:
    return (os.environ.get("EBAY_CATEGORY_IDS") or "6000").strip()


# One app token is good for about two hours; reuse it instead of asking eBay for one per page.
_tok: dict = {"v": None, "t": 0.0}


def _app_token(force: bool = False) -> str:
    if force or not _tok["v"] or time.time() - _tok["t"] > 5400:
        _tok["v"] = _token()
        _tok["t"] = time.time()
    return _tok["v"]


def _item_number(summary: dict) -> Optional[str]:
    legacy = str(summary.get("legacyItemId") or "").strip()
    if legacy.isdigit():
        return legacy
    parts = str(summary.get("itemId") or "").split("|")
    if len(parts) >= 2 and parts[1].isdigit():
        return parts[1]
    return None


def _resolve(db: Session, store: Optional[str]) -> dict:
    """What the store box means: the store key we file things under, and the eBay user name to read."""
    name = (store or "").strip()
    if not name:
        name = MAIN_STORE
    elif not SELLER_RE.fullmatch(name):
        raise HTTPException(400, "That does not look like an eBay store name.")
    row = db.execute(
        text("select store, seller_username from ebay_accounts where lower(store) = lower(:s)"), {"s": name}
    ).first()
    if row:
        key = row.store
        seller = row.seller_username or (seller_username() if key == MAIN_STORE else key)
        return {"store": key, "seller": seller, "known": True}
    seller = seller_username() if name == MAIN_STORE else name
    return {"store": name, "seller": seller, "known": False}


# --------------------------------------------------------------------------
# stores
# --------------------------------------------------------------------------
@router.get("/stores")
def stores(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            """
            select a.store, a.display_name, a.seller_username, a.is_active,
                   (a.refresh_token_enc is not null) as authorized,
                   a.authorized_at, a.live_pushes_enabled,
                   (select count(*) from channel_listings cl where cl.store = a.store) as linked_count
            from ebay_accounts a
            order by a.store
            """
        )
    ).all()
    return {"main_store": MAIN_STORE, "stores": [dict(r._mapping) for r in rows]}


class StoreIn(BaseModel):
    store: str
    display_name: Optional[str] = None
    seller_username: Optional[str] = None  # only if the eBay user name differs from the store name


@router.post("/stores")
def add_store(body: StoreIn, db: Session = Depends(get_db)):
    store = body.store.strip()
    if not SELLER_RE.fullmatch(store):
        raise HTTPException(400, "Store names can only use letters, numbers, dots, dashes and underscores.")
    seller = (body.seller_username or "").strip() or None
    if seller and not SELLER_RE.fullmatch(seller):
        raise HTTPException(400, "That does not look like an eBay user name.")
    if db.execute(text("select 1 from ebay_accounts where lower(store) = lower(:s)"), {"s": store}).first():
        raise HTTPException(409, f"{store} is already in the list.")
    db.execute(
        text("insert into ebay_accounts (store, display_name, seller_username) values (:s, :d, :u)"),
        {"s": store, "d": (body.display_name or "").strip() or None, "u": seller},
    )
    db.commit()
    return {"ok": True, "store": store}


# --------------------------------------------------------------------------
# the list
# --------------------------------------------------------------------------
def _browse(seller: str, q: str, sort: str, offset: int, limit: int) -> dict:
    params = {
        "filter": f"sellers:{{{seller}}}",
        "category_ids": _category_ids(),
        "limit": limit,
        "offset": offset,
        "sort": SORTS.get(sort, "newlyListed"),
    }
    if q:
        params["q"] = q
    url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"

    def attempt(token: str) -> dict:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        )
        return _open(req)

    try:
        return attempt(_app_token())
    except FeedbackError as e:
        if "(401)" not in str(e):
            raise
        return attempt(_app_token(force=True))  # the saved token expired: get a new one and try once more


@router.get("/list")
def list_listings(
    store: Optional[str] = Query(None, description="eBay store; empty means the main store"),
    q: str = Query("", max_length=100),
    sort: str = Query("newest"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    if not configured():
        raise HTTPException(503, "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API.")
    if offset >= MAX_RESULTS:
        raise HTTPException(400, "eBay's public search returns at most 10,000 results. Use the search box to narrow it down.")
    limit = min(limit, MAX_RESULTS - offset)

    who = _resolve(db, store)
    try:
        data = _browse(who["seller"], q.strip(), sort, offset, limit)
    except FeedbackError as e:
        raise HTTPException(502, str(e))
    except Exception:
        log.exception("unexpected error reading live listings")
        raise HTTPException(502, "Something unexpected went wrong reading eBay.")

    summaries = data.get("itemSummaries") or []
    ids = [n for n in (_item_number(s) for s in summaries) if n]

    known: dict[str, dict] = {}
    if ids:
        rows = db.execute(
            text(
                f"""
                select {_DIGITS} as digits, cl.product_id, cl.api_type, cl.store, cl.status,
                       p.title as product_title, p.list_price, p.quantity_on_hand
                from channel_listings cl
                join products p on p.id = cl.product_id
                where {_DIGITS} = any(cast(:ids as text[]))
                """
            ),
            {"ids": ids},
        ).all()
        known = {r.digits: dict(r._mapping) for r in rows}

    items = []
    for s in summaries:
        n = _item_number(s)
        if not n:
            continue
        price = s.get("price") or {}
        k = known.get(n)
        items.append(
            {
                "item_id": n,
                "title": s.get("title") or "(no title)",
                "price": price.get("value"),
                "currency": price.get("currency"),
                "image_url": (s.get("thumbnailImages") or [{}])[0].get("imageUrl") or (s.get("image") or {}).get("imageUrl"),
                "url": s.get("itemWebUrl") or f"https://www.ebay.com/itm/{n}",
                "linked": (
                    {
                        "product_id": k["product_id"],
                        "product_title": k["product_title"],
                        "api_type": k["api_type"],
                        "store": k["store"],
                        "site_price": float(k["list_price"]) if k["list_price"] is not None else None,
                        "site_stock": k["quantity_on_hand"],
                    }
                    if k
                    else None
                ),
            }
        )

    total = data.get("total") if isinstance(data.get("total"), int) else len(items)
    return {
        "store": who["store"],
        "seller": who["seller"],
        "known_store": who["known"],
        "total": min(total, MAX_RESULTS),
        "ebay_total": total,
        "offset": offset,
        "limit": limit,
        "items": items,
    }
