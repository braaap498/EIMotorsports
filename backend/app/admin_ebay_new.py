"""
New eBay listings: find listings that exist on eBay but are not linked to any product
yet, and let an admin link each one to an existing product.

Mounted at /admin/ebay-new/* (owner/admin only).

  1. GET  /admin/ebay-new/check            asks eBay for the store's newest listings and returns
                                           the ones whose item number is NOT in channel_listings.
                                           NOTHING is imported or written.
  2. POST /admin/ebay-new/link             adds one eBay item number to an existing product
                                           (one new row in channel_listings, with the store it
                                           came from). That is all it does.

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
from app.admin_catalog import _unique_slug, finish, slugify
from app.admin_cleanup import _PN_SUBQUERY
from app.admin_ebay_listings import _resolve
from app.database import get_db
from app.ebay_feedback import SEARCH_URL, FeedbackError, _open, _token, configured, seller_username
from app.skus import clean_ebay_item_id

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


def _custom_labels(db: Session, item_numbers: list[str]) -> dict[str, str]:
    """Custom label SKUs we already have for these item numbers (from the label import table,
    filled by the photo import and the report upload). Listings with no label on file are left out."""
    if not item_numbers:
        return {}
    rows = db.execute(
        text(
            "select cast(item_number as text), nullif(trim(cast(custom_label_sku as text)), '') "
            "from ebay_label_import where cast(item_number as text) = any(cast(:ids as text[]))"
        ),
        {"ids": item_numbers},
    ).all()
    return {r[0]: r[1] for r in rows if r[1]}


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

    who = _resolve(db, seller)  # same store rules as the Live listings tab
    store_key = who["store"]
    seller = who["seller"]
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
    labels = _custom_labels(db, [i["item_id"] for i in items])
    for i in items:
        label = labels.get(i["item_id"])
        i["custom_label"] = label  # None when we do not have one yet; "sku" stays EBAY- plus the item number
    return {
        "store": store_key,
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
# 1b. likely products for a listing (read-only)
# --------------------------------------------------------------------------
_STOP = {"the", "and", "for", "with", "new", "free", "fits", "fit", "kit", "set", "oem", "genuine", "from"}


def _esc(w: str) -> str:
    return w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/suggest")
def suggest(
    label: str = Query("", max_length=100),
    title: str = Query("", max_length=300),
    q: str = Query("", max_length=100),
    db: Session = Depends(get_db),
):
    """Products that are probably the one this eBay listing belongs to.

    With nothing typed (q empty): products with exactly this custom label SKU first, then products whose
    manufacturer part number appears in the listing title, then products sharing several words with the
    title. With q typed: every product matching ALL the typed words (title, custom label SKU, eBay item #,
    part number), the likely ones still first. At most 30 are returned."""
    lab = label.strip()
    words: list[str] = []
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]{2,}", title.lower()):
        if w not in _STOP and w not in words:
            words.append(w)
    words = words[:8]
    qwords = [w for w in re.split(r"\s+", q.strip()) if w][:8]

    params: dict = {"lab": lab, "title": title.strip()}
    for n, w in enumerate(words):
        params[f"tw{n}"] = f"%{_esc(w)}%"
    score = " + ".join(f"(case when p.title ilike cast(:tw{n} as text) then 1 else 0 end)" for n in range(len(words))) or "0"

    if qwords:
        for n, w in enumerate(qwords):
            params[f"qw{n}"] = f"%{_esc(w)}%"
        cond = " and ".join(
            f"(p.title ilike cast(:qw{n} as text) or p.ebay_item_id ilike cast(:qw{n} as text) "
            f"or p.custom_label_sku ilike cast(:qw{n} as text) or p.manufacturer_part_number ilike cast(:qw{n} as text))"
            for n in range(len(qwords))
        )
        outer = "true"
    else:
        cond = "true"
        params["minscore"] = max(2, min(4, -(-len(words) * 2 // 5))) if words else 99
        outer = "t.label_match = 1 or t.mpn_match = 1 or t.score >= cast(:minscore as int)"

    rows = db.execute(
        text(
            f"""
            select * from (
              select p.id, p.ebay_item_id, p.custom_label_sku, p.title, p.list_price,
                     p.quantity_on_hand, p.is_published,
                     (select count(*) from channel_listings cl where cl.product_id = p.id) as listing_count,
                     {_PN_SUBQUERY} as part_number,
                     case when cast(:lab as text) <> '' and lower(trim(cast(p.custom_label_sku as text))) = lower(cast(:lab as text)) then 1 else 0 end as label_match,
                     case when cast(:title as text) <> '' and p.manufacturer_part_number is not null
                               and length(trim(p.manufacturer_part_number)) >= 4
                               and position(lower(trim(p.manufacturer_part_number)) in lower(cast(:title as text))) > 0
                          then 1 else 0 end as mpn_match,
                     {score} as score
              from products p
              where {cond}
            ) t
            where {outer}
            order by t.label_match desc, t.mpn_match desc, t.score desc, t.title, t.id
            limit 30
            """
        ),
        params,
    ).all()
    return {"searching": bool(qwords), "items": [dict(r._mapping) for r in rows]}


# --------------------------------------------------------------------------
# 2. link one eBay item number to a product (and 3. create a product and link it)
# --------------------------------------------------------------------------
def _check_item(item_id: str) -> str:
    item_id = item_id.strip()
    if not re.fullmatch(r"\d{9,15}", item_id):
        raise HTTPException(400, "That does not look like an eBay item number.")
    return item_id


def _not_linked_yet(db: Session, item_id: str) -> None:
    existing = db.execute(
        text(f"select product_id from channel_listings where {_DIGITS} = :e limit 1"), {"e": item_id}
    ).first()
    if existing:
        raise HTTPException(409, f"eBay item {item_id} is already linked to product {existing[0]}.")


def _insert_link(db: Session, item_id: str, product_id: int, store_key: str) -> int:
    """Adds the channel_listings row (no commit). Returns how many eBay item numbers the product now has."""
    # Which channel the new row belongs to: the one every existing eBay link already uses.
    channel = db.execute(
        text("select channel_id from channel_listings group by channel_id order by count(*) desc limit 1")
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
        and c.column_name not in {"product_id", "channel_id", "external_id", "store"}
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
    db.execute(
        text(
            f"insert into channel_listings (product_id, channel_id, external_id, store) "
            f"values (:p, :c, {value_sql}, :st)"
        ),
        {"p": product_id, "c": channel[0], "e": stored, "st": store_key},
    )
    return db.execute(
        text("select count(*) from channel_listings where product_id = :p"), {"p": product_id}
    ).scalar()


class LinkIn(BaseModel):
    item_id: str
    product_id: int
    store: Optional[str] = None  # the store the listing was found in; empty means the main store


@router.post("/link")
def link(body: LinkIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    item_id = _check_item(body.item_id)
    product = db.execute(
        text("select id, title from products where id = :id"), {"id": body.product_id}
    ).first()
    if not product:
        raise HTTPException(404, "That product no longer exists.")
    store_key = _resolve(db, body.store)["store"]
    _not_linked_yet(db, item_id)
    try:
        count = _insert_link(db, item_id, product.id, store_key)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"eBay item {item_id} was just linked by someone else.")
    except HTTPException:
        db.rollback()
        raise

    log.info("eBay item %s (store %s) linked to product %s by %s", item_id, store_key, product.id, user.email or user.user_id)
    return {
        "ok": True, "created": False, "item_id": item_id, "sku": f"{SKU_PREFIX}{item_id}", "store": store_key,
        "product_id": product.id, "product_title": product.title, "listing_count": count,
    }


class CreateIn(BaseModel):
    item_id: str
    store: Optional[str] = None
    title: str
    custom_label_sku: Optional[str] = None  # may be empty
    list_price: Optional[float] = None
    quantity_on_hand: int = 0


@router.post("/create-product")
def create_and_link(body: CreateIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Makes a new (unpublished) product from an eBay listing and links the listing to it, in one step."""
    item_id = _check_item(body.item_id)
    title = " ".join(body.title.split())
    if not title:
        raise HTTPException(400, "A title is required.")
    if len(title) > 255:
        raise HTTPException(400, "That title is too long.")
    label = (body.custom_label_sku or "").strip() or None
    if label and re.sub(r"[^0-9]", "", label) == item_id and re.fullmatch(r"(?i)(ebay-)?\d+", label):
        raise HTTPException(400, "The custom label SKU cannot be the same as the eBay item number. Leave it empty if there is none.")
    if body.quantity_on_hand < 0:
        raise HTTPException(400, "Quantity cannot be negative.")
    if body.list_price is not None and body.list_price < 0:
        raise HTTPException(400, "Price cannot be negative.")
    store_key = _resolve(db, body.store)["store"]
    _not_linked_yet(db, item_id)

    fields = {
        "title": title,
        "slug": _unique_slug(db, slugify(title)),
        "ebay_item_id": clean_ebay_item_id(item_id),
        "custom_label_sku": label,
        "list_price": body.list_price,
        "quantity_on_hand": body.quantity_on_hand,
        "is_published": False,
        "is_active": True,
    }
    try:
        cols = ", ".join(fields)  # keys are fixed above, not from the client
        vals = ", ".join(f":{k}" for k in fields)
        new_id = db.execute(text(f"insert into products ({cols}) values ({vals}) returning id"), fields).scalar()
        count = _insert_link(db, item_id, new_id, store_key)
        finish(db, refresh=True)  # commits both rows together
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A product with that eBay item number already exists, or the listing was just linked. Nothing was created.")
    except HTTPException:
        db.rollback()
        raise

    # The new product starts with the listing's description from eBay (read only; if eBay can't be reached the
    # product is still made, and Parts clean-up > "Fill from eBay" can fill it later).
    try:
        from app.ebay_edit import _read_item
        from app.sanitize import clean_description

        desc = clean_description(_read_item(db, store_key, item_id).get("description"))
        if desc:
            db.execute(text("update products set description = :d where id = :p and description is null"), {"d": desc, "p": new_id})
            db.commit()
    except Exception:
        db.rollback()
        log.warning("Could not read the description of eBay item %s for the new product", item_id)

    log.info("eBay item %s (store %s): new product %s created and linked by %s", item_id, store_key, new_id, user.email or user.user_id)
    return {
        "ok": True, "created": True, "item_id": item_id, "sku": f"{SKU_PREFIX}{item_id}", "store": store_key,
        "product_id": new_id, "product_title": title, "listing_count": count,
    }
