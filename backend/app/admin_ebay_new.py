"""
New eBay listings: find listings that exist on eBay but are not linked to any product
yet, and let an admin link each one to an existing product.

Mounted at /admin/ebay-new/* (owner/admin only).

  1. GET  /admin/ebay-new/check            asks eBay for the store's newest listings and returns
                                           the ones whose item number is NOT in channel_listings.
                                           NOTHING is imported or written.
  2. POST /admin/ebay-new/link             adds one eBay item number to an existing product
                                           (one new row in channel_listings). That is all it does.

Why this exists: after parts are merged, one product carries many eBay item numbers (the
channel_listings rows). When a new eBay listing is made for a part we already sell, its item
number is not on the product yet, so a sale of it could not update that product's stock. An admin
links it here, and it simply joins the product's other eBay item numbers.

How eBay is read: the same public Browse API access the About page uses (ebay_feedback.py), asked
for the seller's listings NEWEST FIRST. Reading stops at the first page that holds nothing new, so
a normal check is one or two calls. "deep" keeps going through everything eBay will return
(eBay stops at 10,000 results), which is slower.

SKU form: products carry SKUs like EBAY-800705556598, so the screen shows new listings the same way
(EBAY- plus the item number). What gets stored in channel_listings copies the form the existing
links already use (with or without a prefix), and "already linked" ignores any prefix when comparing,
so the two can never disagree about whether a listing is linked.

Settings (environment variables on the API; the first three are shared with the About page):
    EBAY_CLIENT_ID, EBAY_CLIENT_SECRET   production keys from developer.ebay.com
    EBAY_SELLER_USERNAME                 the store's eBay user name
    EBAY_CATEGORY_IDS                    optional, comma-separated top-level eBay categories to read
                                         (default 6000, Vehicle Parts & Accessories)
"""
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin
from app.database import get_db
from app.ebay_feedback import SEARCH_URL, FeedbackError, _open, _token, configured, seller_username

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-new", tags=["admin-ebay-new"], dependencies=[Depends(require_admin)])

PAGE = 200  # most listings eBay returns per call
QUICK_MAX_PAGES = 10  # a normal check never reads more than the 2,000 newest listings
DEEP_MAX_PAGES = 50  # eBay will not return past result 10,000 (50 x 200)
MAX_NEW_SHOWN = 500


def _category_ids() -> str:
    return (os.environ.get("EBAY_CATEGORY_IDS") or "6000").strip()


def _item_number(summary: dict) -> Optional[str]:
    """The plain item number (digits) that eBay shows on the listing page.
    Browse returns ids like 'v1|123456789012|0'; the middle part is the item number."""
    legacy = str(summary.get("legacyItemId") or "").strip()
    if legacy.isdigit():
        return legacy
    parts = str(summary.get("itemId") or "").split("|")
    if len(parts) >= 2 and parts[1].isdigit():
        return parts[1]
    return None


SELLER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")  # characters eBay allows in a user name


def _resolve_seller(seller: Optional[str]) -> str:
    """The store to read: the one typed on the screen, or the API's own store when left empty."""
    name = (seller or "").strip()
    if not name:
        return seller_username()
    if not SELLER_RE.fullmatch(name):
        raise HTTPException(400, "That does not look like an eBay store name.")
    return name


def _fetch_page(token: str, offset: int, seller: str) -> dict:
    q = urllib.parse.urlencode(
        {
            "filter": f"sellers:{{{seller}}}",
            "category_ids": _category_ids(),
            "sort": "newlyListed",
            "limit": PAGE,
            "offset": offset,
        }
    )
    req = urllib.request.Request(
        f"{SEARCH_URL}?{q}",
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
    )
    return _open(req)


_DIGITS = "regexp_replace(cast(external_id as text), '[^0-9]', '', 'g')"  # item number without any prefix
SKU_PREFIX = "EBAY-"  # how product SKUs are written


def _stored_prefix(db: Session) -> str:
    """What comes before the item number in the existing channel_listings.external_id values
    (for example 'EBAY-', or '' when only the bare number is stored): whatever most rows use."""
    row = db.execute(
        text(
            "select regexp_replace(cast(external_id as text), '[0-9]+$', '') as pfx, count(*) "
            "from channel_listings where external_id is not null "
            "group by 1 order by 2 desc limit 1"
        )
    ).first()
    return row[0] if row and row[0] is not None else ""


def _already_linked(db: Session, item_numbers: list[str]) -> set[str]:
    if not item_numbers:
        return set()
    rows = db.execute(
        text(f"select {_DIGITS} from channel_listings where {_DIGITS} = any(cast(:ids as text[]))"),
        {"ids": item_numbers},
    ).all()
    return {r[0] for r in rows}


# --------------------------------------------------------------------------
# 1. check (read-only)
# --------------------------------------------------------------------------
@router.get("/check")
def check(
    deep: bool = Query(False),
    seller: Optional[str] = Query(None, description="eBay store name; empty means this site's own store"),
    db: Session = Depends(get_db),
):
    if not configured():
        raise HTTPException(503, "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API.")

    seller = _resolve_seller(seller)
    max_pages = DEEP_MAX_PAGES if deep else QUICK_MAX_PAGES
    try:
        token = _token()
        new: dict[str, dict] = {}
        read = 0  # eBay listings looked at
        pages = 0
        ebay_total: Optional[int] = None
        caught_up = False

        for page in range(max_pages):
            data = _fetch_page(token, page * PAGE, seller)
            pages += 1
            if ebay_total is None and isinstance(data.get("total"), int):
                ebay_total = data["total"]
            summaries = data.get("itemSummaries") or []
            if not summaries:
                caught_up = True  # eBay has nothing more to give
                break

            numbers = [n for n in (_item_number(s) for s in summaries) if n]
            read += len(numbers)
            linked = _already_linked(db, numbers)

            found_here = 0
            for s in summaries:
                n = _item_number(s)
                if not n or n in linked or n in new:
                    continue
                found_here += 1
                price = s.get("price") or {}
                image = s.get("image") or {}
                new[n] = {
                    "item_id": n,
                    "sku": f"{SKU_PREFIX}{n}",
                    "title": s.get("title") or "(no title)",
                    "price": price.get("value"),
                    "currency": price.get("currency"),
                    "image_url": image.get("imageUrl"),
                    "url": s.get("itemWebUrl") or f"https://www.ebay.com/itm/{n}",
                    "listed_at": s.get("itemCreationDate"),
                }
            # Newest first: a page with nothing new means everything older is already linked.
            if not deep and found_here == 0:
                caught_up = True
                break
            if len(summaries) < PAGE or len(new) >= MAX_NEW_SHOWN:
                caught_up = True
                break
    except FeedbackError as e:
        raise HTTPException(502, str(e))
    except HTTPException:
        raise
    except Exception:
        log.exception("unexpected error checking eBay for new listings")
        raise HTTPException(502, "Something unexpected went wrong reading eBay.")

    items = list(new.values())[:MAX_NEW_SHOWN]
    return {
        "seller": seller,
        "deep": deep,
        "pages_read": pages,
        "listings_read": read,
        "ebay_total": ebay_total,
        "reached_end": caught_up,
        "new_count": len(items),
        "items": items,
    }


# --------------------------------------------------------------------------
# 2. link one eBay item number to a product
# --------------------------------------------------------------------------
class LinkIn(BaseModel):
    item_id: str
    product_id: int


@router.post("/link")
def link(body: LinkIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    item_id = body.item_id.strip()
    if not re.fullmatch(r"\d{9,15}", item_id):
        raise HTTPException(400, "That does not look like an eBay item number.")

    product = db.execute(
        text("select id, title from products where id = :id"), {"id": body.product_id}
    ).first()
    if not product:
        raise HTTPException(404, "That product no longer exists.")

    existing = db.execute(
        text(f"select product_id from channel_listings where {_DIGITS} = :e limit 1"),
        {"e": item_id},
    ).first()
    if existing:
        raise HTTPException(409, f"eBay item {item_id} is already linked to product {existing[0]}.")

    # Which channel the new row belongs to: the one every existing eBay link already uses.
    channel = db.execute(
        text(
            "select channel_id from channel_listings group by channel_id "
            "order by count(*) desc limit 1"
        )
    ).first()
    if not channel:
        raise HTTPException(409, "No existing eBay links to copy the channel from, so nothing was added.")

    # channel_listings may have other columns I was not shown. If any of them must be filled in
    # and has no default, refuse (clearly) instead of guessing.
    cols = db.execute(
        text(
            """
            select column_name, data_type, is_nullable, column_default, is_identity, is_generated
            from information_schema.columns
            where table_schema = 'public' and table_name = 'channel_listings'
            """
        )
    ).all()
    by_name = {c.column_name: c for c in cols}
    if "external_id" not in by_name:
        raise HTTPException(500, "channel_listings has no external_id column.")
    must_fill = [
        c.column_name
        for c in cols
        if c.is_nullable == "NO"
        and c.column_default is None
        and c.is_identity != "YES"
        and c.is_generated == "NEVER"
        and c.column_name not in {"product_id", "channel_id", "external_id"}
    ]
    if must_fill:
        raise HTTPException(
            409,
            "channel_listings has required columns this screen does not fill in: "
            + ", ".join(must_fill)
            + ". Nothing was added.",
        )

    numeric = by_name["external_id"].data_type in {"bigint", "integer", "smallint", "numeric"}
    value_sql = "cast(:e as bigint)" if numeric else "cast(:e as text)"
    # Store it the way the existing links are stored (same prefix, if they have one).
    stored = item_id if numeric else _stored_prefix(db) + item_id
    try:
        db.execute(
            text(
                f"insert into channel_listings (product_id, channel_id, external_id) "
                f"values (:p, :c, {value_sql})"
            ),
            {"p": product.id, "c": channel[0], "e": stored},
        )
        count = db.execute(
            text("select count(*) from channel_listings where product_id = :p"), {"p": product.id}
        ).scalar()
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"eBay item {item_id} was just linked by someone else.")

    log.info("eBay item %s linked to product %s by %s", item_id, product.id, user.email or user.user_id)
    return {"ok": True, "item_id": item_id, "sku": f"{SKU_PREFIX}{item_id}", "stored_as": stored, "product_id": product.id, "product_title": product.title, "listing_count": count}
