"""
Live eBay listings: the back end of the admin "Live listings" tab.

Mounted at /admin/ebay-listings/* (owner/admin only).

  GET  /stores              the eBay stores the site knows about (never returns any token)
  POST /stores              add a store
  POST /repo/status         mark chosen listings active or inactive in OUR database (does not call eBay)
  GET  /repo                one page of a store's listings from OUR database (all, active or inactive), shaped like /list
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
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db, get_engine
from app.images import thumb_url
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
                   a.authorized_at, a.authorized_by, a.refresh_token_expires_at, a.live_pushes_enabled,
                   (select count(*) from channel_listings cl where cl.store = a.store) as linked_count
            from ebay_accounts a
            order by a.store
            """
        )
    ).all()
    return {"main_store": MAIN_STORE, "stores": [dict(r._mapping) for r in rows]}


class UserNameIn(BaseModel):
    seller_username: str


@router.put("/stores/{store}/seller-username")
def set_seller_username(store: str, body: UserNameIn, db: Session = Depends(get_db)):
    """Fix the eBay user name a store is expected to sign in as (used by the approval check)."""
    name = body.seller_username.strip()
    if not SELLER_RE.fullmatch(name):
        raise HTTPException(400, "That does not look like an eBay user name.")
    res = db.execute(
        text("update ebay_accounts set seller_username = :u, updated_at = now() where lower(store) = lower(:s)"),
        {"u": name, "s": store.strip()},
    )
    db.commit()
    if not res.rowcount:
        raise HTTPException(404, "That store is not in the list.")
    return {"ok": True}


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


# --------------------------------------------------------------------------
# the store's listings from our own database (not eBay): channel_listings + products
# --------------------------------------------------------------------------
# Unlike /list (eBay's public search: active, buyable listings only) this shows every listing we have on file for the
# store, so ended / inactive ones are here too. "Active" means channel_listings.status is 'active' (any case); everything
# else is "inactive". The raw status values and how many of each are returned so the screen can show them as they are.
_ACTIVE = "lower(coalesce(cl.status, '')) = 'active'"
# Several eBay listings can be linked to ONE product (merged duplicates), so the product's title/label would make them all
# look the same. Each listing's own title and custom label come first, from what we saved about that item number
# (uploaded report, then the eBay lookup, then the compat import); the product's own values are only the fallback.
_LISTING_JOINS = f"""
    left join products p on p.id = cl.product_id
    left join ebay_report_rows lr on lr.item_id = {_DIGITS}
    left join ebay_api_details la on la.item_id = {_DIGITS}
    left join ebay_compat_items lc on lc.item_id = {_DIGITS}
"""
_L_TITLE = "coalesce(nullif(btrim(lr.title), ''), nullif(btrim(la.title), ''), nullif(btrim(lc.title), ''), p.title)"
_L_LABEL = "coalesce(nullif(btrim(lr.custom_label_sku), ''), nullif(btrim(la.custom_label_sku), ''), p.custom_label_sku)"
# The product's price when it has one; otherwise the price saved for this exact item number (report, then eBay lookup).
_L_PRICE = "coalesce(p.list_price, lr.price, la.price)"
# Quantity: our stock on hand. When that is empty or 0 and eBay's available quantity was saved for this exact item number
# (report, then eBay lookup) and is above 0, that is shown instead and marked as coming from eBay. Stock is never changed.
_L_EBAY_QTY = "cast(coalesce(lr.quantity, la.quantity) as integer)"
_L_QTY_FROM_EBAY = f"(coalesce(p.quantity_on_hand, 0) = 0 and coalesce({_L_EBAY_QTY}, 0) > 0)"
# Photo order for a row: the listing's own saved set (ebay_listings.photos, filled by the photo import's "listing
# originals" run and by merges), then photos saved under its own item number, then the product's main photo.
_REPO_SORTS = {
    "newest": "cl.id desc",
    "title": f"lower({_L_TITLE}), cl.id",
    "price_low": f"{_L_PRICE} asc nulls last, cl.id",
    "price_high": f"{_L_PRICE} desc nulls last, cl.id",
}


@router.get("/repo")
def repo_listings(
    store: Optional[str] = Query(None, description="eBay store; empty means the main store"),
    all_stores: bool = Query(False, description="true = every store at once; the store parameter is then ignored"),
    status: str = Query("all", max_length=40, description="all, or one status exactly as on file (active, ended, draft, ...); 'inactive' = anything not active"),
    q: str = Query("", max_length=100),
    sort: str = Query("newest", pattern="^(newest|title|price_low|price_high)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    if all_stores:
        who = {"store": "", "seller": "", "known": True}
        base = [f"{_DIGITS} <> ''"]
        params: dict = {"n": limit, "o": offset}
    else:
        who = _resolve(db, store)
        base = ["lower(cl.store) = lower(:store)", f"{_DIGITS} <> ''"]
        params = {"store": who["store"], "n": limit, "o": offset}
    # counts and raw statuses for the whole store (the search box does not change them)
    counts = db.execute(
        text(
            f"select count(*) as total, count(*) filter (where {_ACTIVE}) as active "
            f"from channel_listings cl where {' and '.join(base)}"
        ),
        params,
    ).first()
    raw = db.execute(
        text(
            f"select lower(coalesce(nullif(btrim(cl.status), ''), '(none)')) as status, count(*) as n from channel_listings cl "
            f"where {' and '.join(base)} group by 1 order by n desc"
        ),
        params,
    ).all()

    # "Draft" = a listing whose stored status is draft (report import), OR a live listing with edits saved on the
    # website that have not been pushed to eBay yet (ebay_listing_drafts). The table may not exist yet: then none.
    has_edits_table = bool(db.execute(text("select to_regclass('public.ebay_listing_drafts') is not null")).scalar())
    edits_sql = (
        f"exists (select 1 from ebay_listing_drafts d where lower(d.store) = lower(cl.store) and d.item_id = {_DIGITS})"
        if has_edits_table
        else "false"
    )
    draft_sql = f"(lower(coalesce(cl.status, '')) = 'draft' or {edits_sql})"
    draft_n = db.execute(
        text(f"select count(*) from channel_listings cl where {' and '.join(base)} and {draft_sql}"), params
    ).scalar() or 0

    where = list(base)
    if status == "active":
        where.append(_ACTIVE)
    elif status == "inactive":
        where.append(f"not ({_ACTIVE})")
    elif status == "draft":
        where.append(draft_sql)
    elif status != "all":
        where.append("lower(coalesce(nullif(btrim(cl.status), ''), '(none)')) = :stv")
        params["stv"] = status.strip().lower()
    for i, w in enumerate([w for w in re.split(r"\s+", q.strip()) if w][:6]):
        params[f"w{i}"] = f"%{w}%"
        where.append(f"(p.title ilike :w{i} or {_L_TITLE} ilike :w{i} or {_DIGITS} ilike :w{i} or p.custom_label_sku ilike :w{i} or {_L_LABEL} ilike :w{i} or p.sku_display ilike :w{i})")
    clause = " and ".join(where)
    total = db.execute(
        text(f"select count(*) from channel_listings cl {_LISTING_JOINS} where {clause}"), params
    ).scalar()
    rows = db.execute(
        text(
            f"""
            select {_DIGITS} as item_id, cl.id as listing_id, cl.store, cl.status, cl.api_type,
                   p.id as product_id, p.sku_display as sku, {_L_TITLE} as title, {_L_LABEL} as custom_label_sku,
                   {_L_PRICE} as list_price, p.quantity_on_hand,
                   case when {_L_QTY_FROM_EBAY} then {_L_EBAY_QTY} else p.quantity_on_hand end as quantity_shown,
                   {_L_QTY_FROM_EBAY} as qty_from_ebay,
                   {edits_sql} as unsent_edits,
                   (select count(*) from channel_listings x where x.product_id = p.id) as product_listings,
                   case
                     when exists (select 1 from channel_listings x where x.product_id = p.id
                                    and regexp_replace(cast(x.external_id as text), '[^0-9]', '', 'g')
                                      = nullif(regexp_replace(coalesce(cast(p.ebay_item_id as text), ''), '[^0-9]', '', 'g'), ''))
                       then {_DIGITS} = nullif(regexp_replace(coalesce(cast(p.ebay_item_id as text), ''), '[^0-9]', '', 'g'), '')
                     else cl.id = (select min(x.id) from channel_listings x where x.product_id = p.id)
                   end as is_product_source,
                   coalesce(
                     (select e->>'image_key'
                        from ebay_listings el,
                             jsonb_array_elements(case when jsonb_typeof(el.photos) = 'array' then el.photos else '[]'::jsonb end)
                               with ordinality as t(e, n)
                       where el.ebay_item_id = {_DIGITS} and coalesce(e->>'image_key', '') <> ''
                       order by (coalesce(e->>'is_primary', '') = 'true') desc,
                                case when (e->>'sort_order') ~ '^[0-9]+$' then (e->>'sort_order')::int else 0 end, n
                       limit 1),
                     (select ph.image_key from product_photos ph
                       where split_part(ph.storage_path, '/', 1) = 'EBAY-' || {_DIGITS}
                       order by ph.is_primary desc, ph.sort_order, ph.id limit 1),
                     (select ph.image_key from product_photos ph where ph.product_id = p.id
                       order by ph.is_primary desc, ph.sort_order, ph.id limit 1)
                   ) as image_key
            from channel_listings cl {_LISTING_JOINS}
            where {clause} order by {_REPO_SORTS[sort]} limit :n offset :o
            """
        ),
        params,
    ).all()
    items = []
    for r in rows:
        d = dict(r._mapping)
        key = d.pop("image_key", None)
        items.append(
            {
                "store": d["store"],
                "sku": d["sku"],
                "item_id": d["item_id"],
                "listing_id": d["listing_id"],
                "title": d["title"] or "(no title)",
                "custom_label": d["custom_label_sku"],
                "price": float(d["list_price"]) if d["list_price"] is not None else None,
                "currency": "USD",
                "quantity": d["quantity_shown"],
                "quantity_from_ebay": bool(d["qty_from_ebay"]),
                "status": (d["status"] or "").strip() or None,
                "active": (d["status"] or "").strip().lower() == "active",
                "unsent_edits": bool(d["unsent_edits"]),
                "api_type": d["api_type"],
                "product_id": d["product_id"],
                "image_url": thumb_url(key) if key else None,
                # for a merged product (more than one listing), the one listing its gallery is taken from
                "is_product_source": bool(d["is_product_source"]) and (d["product_listings"] or 0) > 1,
                "url": f"https://www.ebay.com/itm/{d['item_id']}",
            }
        )
    return {
        "store": who["store"],
        "seller": who["seller"],
        "known_store": who["known"],
        "status": status,
        "total": total,
        "counts": {"all": counts.total, "active": counts.active, "inactive": counts.total - counts.active},
        # Draft is counted as described above (stored draft status plus listings with unsent edits), not by raw status
        "statuses": [{"status": r.status, "count": r.n} for r in raw if r.status != "draft"]
        + ([{"status": "draft", "count": draft_n}] if draft_n else []),
        "offset": offset,
        "limit": limit,
        "items": items,
    }


class RepoStatusIn(BaseModel):
    store: str
    listing_ids: list[int]
    status: str  # "active" or "ended" (the screen's "inactive")


@router.post("/repo/status")
def repo_set_status(body: RepoStatusIn, db: Session = Depends(get_db)):
    """The decision step: mark chosen listings active or inactive in our database, nothing more.
    It changes channel_listings.status only (the words already in use: active / ended). No call goes to eBay;
    the automatic check that will compare us with eBay comes later. Only listings of the named store are touched."""
    if body.status not in ("active", "ended"):
        raise HTTPException(400, "Status must be active or ended.")
    ids = sorted({i for i in body.listing_ids if isinstance(i, int)})
    if not ids:
        raise HTTPException(400, "Pick at least one listing.")
    if len(ids) > 200:
        raise HTTPException(400, "Pick 200 listings or fewer at a time.")
    who = _resolve(db, body.store)
    marks = ", ".join(f":i{n}" for n in range(len(ids)))
    params: dict = {f"i{n}": v for n, v in enumerate(ids)}
    params.update({"store": who["store"], "st": body.status})
    res = db.execute(
        text(
            f"update channel_listings set status = :st where lower(store) = lower(:store) and id in ({marks}) "
            f"and lower(coalesce(status, '')) <> :st"
        ),
        params,
    )
    db.commit()
    return {"store": who["store"], "status": body.status, "changed": res.rowcount, "asked": len(ids)}


# --------------------------------------------------------------------------
# keeping channel_listings.status true: a background check against eBay
# --------------------------------------------------------------------------
# The Listings tab shows the status saved in OUR database. This brings it in line with eBay without slowing the page:
# a background loop (start_status_sync_daily, started from main.py) runs one check of every connected store once a day,
# after 08:00 UTC, catching up when the server was asleep. Opening the tab only shows where the checks stand
# (POST /status-sync/start); POST /status-sync/run starts a check now.
#
# One check of one store (needs the store connected to eBay):
#   1. read the store's whole list of LIVE listings from eBay (GetMyeBaySelling, 200 per request) and mark those active;
#   2. listings our database does not call ended, that eBay did not list as live, are asked about one by one
#      (GetItem, at most STATUS_SYNC_MAX_CHECKS per run). Marked ended only when eBay itself says Completed/Ended,
#      marked active if eBay says Active. If eBay will not say, nothing changes and it is not asked again for 14 days.
# Nothing is ever marked ended from a missing list entry alone, and a failed or half-read list changes nothing it
# did not see. Progress and the last result are kept in ebay_status_sync (one row per store).
STATUS_SYNC_PAGE = 200


def _sync_max_checks() -> int:
    try:
        return max(0, int(os.environ.get("STATUS_SYNC_MAX_CHECKS") or 400))
    except ValueError:
        return 400


def _sync_tables_ready(db: Session) -> bool:
    ok = db.execute(
        text("select to_regclass('ebay_status_sync') is not null and to_regclass('ebay_status_checked') is not null")
    ).scalar()
    db.commit()
    return bool(ok)


def _sync_beat(store: str, **vals):
    """Saves progress (and shows the run is alive)."""
    sets = ", ".join(f"{k} = :{k}" for k in vals)
    with get_engine().begin() as c:
        c.execute(
            text(f"update ebay_status_sync set {sets}{', ' if sets else ''}updated_at = now() where store = :store"),
            {**vals, "store": store},
        )


def _live_item_ids(db: Session, store: str) -> set:
    """Every item number eBay lists as live for the store. Raises if eBay cannot be read completely."""
    from app.ebay_edit import NS, _failed, _first_message, _trading

    ids: set = set()
    page, pages = 1, 1
    while page <= pages:
        root = _trading(
            db,
            store,
            "GetMyeBaySelling",
            "<ActiveList><Include>true</Include><Pagination>"
            f"<EntriesPerPage>{STATUS_SYNC_PAGE}</EntriesPerPage><PageNumber>{page}</PageNumber>"
            "</Pagination></ActiveList>",
        )
        if _failed(root):
            raise RuntimeError(_first_message(root))
        lst = root.find(f"{NS}ActiveList")
        if lst is None:
            raise RuntimeError("eBay's answer had no list of live listings in it.")
        tp = (lst.findtext(f"{NS}PaginationResult/{NS}TotalNumberOfPages") or "1").strip()
        pages = int(tp) if tp.isdigit() else 1
        for it in lst.findall(f"{NS}ItemArray/{NS}Item"):
            v = (it.findtext(f"{NS}ItemID") or "").strip()
            if v.isdigit():
                ids.add(v)
        _sync_beat(store, message=f"Reading eBay's live listings… page {page} of {pages}", active_found=len(ids))
        page += 1
        time.sleep(0.2)
    return ids


def _item_status(db: Session, store: str, item: str) -> Optional[str]:
    """eBay's own word for one listing: 'active', 'ended', or None when eBay will not say."""
    from app.ebay_edit import NS, _failed, _trading

    root = _trading(
        db,
        store,
        "GetItem",
        f"<ItemID>{item}</ItemID><OutputSelector>ItemID</OutputSelector><OutputSelector>SellingStatus</OutputSelector>",
    )
    if _failed(root):
        return None
    st = (root.findtext(f"{NS}Item/{NS}SellingStatus/{NS}ListingStatus") or "").strip().lower()
    if st == "active":
        return "active"
    if st in ("completed", "ended"):
        return "ended"
    return None


def _set_status(store: str, items: list, status: str) -> int:
    n = 0
    with get_engine().begin() as c:
        for i in range(0, len(items), 1000):
            n += (
                c.execute(
                    text(
                        "update channel_listings set status = :st where lower(store) = lower(:s) "
                        "and regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = any(cast(:ids as text[])) "
                        "and lower(coalesce(status, '')) <> :st"
                    ),
                    {"st": status, "s": store, "ids": items[i : i + 1000]},
                ).rowcount
                or 0
            )
    return n


def _sync_store(store: str):
    db = Session(get_engine())
    try:
        live = _live_item_ids(db, store)
        marked_active = _set_status(store, sorted(live), "active")
        _sync_beat(store, marked_active=marked_active, message="Checking listings eBay did not list as live…")

        with get_engine().connect() as c:
            todo = [
                r[0]
                for r in c.execute(
                    text(
                        "select d.item from ("
                        "  select regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') as item, cl.id "
                        "  from channel_listings cl where lower(cl.store) = lower(:s) "
                        "  and lower(coalesce(cl.status, '')) <> 'ended'"
                        ") d left join ebay_status_checked k on k.item_number = d.item "
                        "where d.item ~ '^[0-9]{9,15}$' "
                        # Anything we still show as live but eBay did not list as live is looked at again on every daily
                        # run (only the ones already looked at in the last 20 hours wait), never-checked first, then
                        # the longest ago. The old 14-day wait let a listing that ended after its last look keep
                        # showing as active for up to two weeks.
                        "and (k.checked_at is null or k.checked_at < now() - interval '20 hours') "
                        "order by k.checked_at nulls first, d.id"
                    ),
                    {"s": store},
                )
            ]
        todo = [i for i in dict.fromkeys(todo) if i not in live]
        left = max(0, len(todo) - _sync_max_checks())
        marked_ended = unsure = 0
        for n, item in enumerate(todo[: _sync_max_checks()], 1):
            word = _item_status(db, store, item)
            with get_engine().begin() as c:
                c.execute(
                    text(
                        "insert into ebay_status_checked (item_number, result, checked_at) values (:i, :r, now()) "
                        "on conflict (item_number) do update set result = :r, checked_at = now()"
                    ),
                    {"i": item, "r": word or "unknown"},
                )
            if word == "ended":
                marked_ended += _set_status(store, [item], "ended")
            elif word == "active":
                marked_active += _set_status(store, [item], "active")
            else:
                unsure += 1
            if n % 20 == 0:
                _sync_beat(store, marked_active=marked_active, marked_ended=marked_ended,
                           message=f"Checking listings eBay did not list as live… {n} of {min(len(todo), _sync_max_checks())}")
        bits = [f"{len(live)} live on eBay", f"{marked_active} set to active", f"{marked_ended} set to ended"]
        if unsure:
            bits.append(f"{unsure} eBay would not say (left as they are)")
        if left:
            bits.append(f"{left} more will be checked on the next run")
        _sync_beat(
            store, status="done", marked_active=marked_active, marked_ended=marked_ended,
            active_found=len(live), message="; ".join(bits) + ".",
        )
        with get_engine().begin() as c:
            c.execute(text("update ebay_status_sync set finished_at = now() where store = :s"), {"s": store})
    except Exception as e:
        log.exception("status sync for %s failed", store)
        msg = getattr(e, "detail", None) or str(e) or "failed"
        _sync_beat(store, status="failed", message=f"Could not finish: {str(msg)[:300]}")
    finally:
        db.close()


def _sync_rows(db: Session) -> list:
    rows = db.execute(
        text(
            """
            select a.store, (a.refresh_token_enc is not null) as connected,
                   s.status, s.finished_at, s.active_found, s.marked_active, s.marked_ended, s.message,
                   coalesce(s.status = 'running' and s.updated_at > now() - interval '15 minutes', false) as running
            from ebay_accounts a left join ebay_status_sync s on s.store = a.store
            order by a.store
            """
        )
    ).all()
    return [dict(r._mapping) for r in rows]


@router.get("/status-sync")
def status_sync_info(db: Session = Depends(get_db)):
    if not _sync_tables_ready(db):
        return {"ready": False, "stores": []}
    return {"ready": True, "stores": _sync_rows(db)}


SYNC_HOUR_UTC = 8  # the daily check runs once after 08:00 UTC (3 AM US Eastern in winter); if the server was asleep it runs when it wakes


def _sync_cutoff(now: Optional[datetime] = None) -> datetime:
    """The most recent daily boundary (08:00 UTC). A store is due when its last finished check is older than this."""
    now = now or datetime.now(timezone.utc)
    cut = now.replace(hour=SYNC_HOUR_UTC, minute=0, second=0, microsecond=0)
    return cut if now >= cut else cut - timedelta(days=1)


def _start_due_syncs(db: Session, cutoff: datetime) -> list[str]:
    """For every connected store whose last finished check is older than `cutoff` (or never happened), and which is not
    already being checked, start the check in the background. Safe to call from several workers: the claim is one UPDATE."""
    started: list[str] = []
    for r in db.execute(text("select store from ebay_accounts where refresh_token_enc is not null")).all():
        db.execute(
            text("insert into ebay_status_sync (store, status) values (:s, 'idle') on conflict (store) do nothing"),
            {"s": r.store},
        )
        claimed = db.execute(
            text(
                "update ebay_status_sync set status = 'running', started_at = now(), updated_at = now(), "
                "message = 'Starting…', marked_active = 0, marked_ended = 0, active_found = 0 "
                "where store = :s "
                "and (status <> 'running' or updated_at < now() - interval '15 minutes') "
                "and (finished_at is null or finished_at < :cut) "
                "and (status <> 'failed' or updated_at < now() - interval '30 minutes') "
                "returning store"
            ),
            {"s": r.store, "cut": cutoff},
        ).first()
        db.commit()
        if claimed:
            started.append(r.store)
            threading.Thread(target=_sync_store, args=(r.store,), daemon=True, name=f"status-sync-{r.store}").start()
    return started


@router.post("/status-sync/start")
def status_sync_start(db: Session = Depends(get_db)):
    """Called when the Listings tab opens. It no longer starts a check (the daily background run does that); it only
    returns where each store's check stands, so the screen can show progress and the time of the last one."""
    if not _sync_tables_ready(db):
        return {"ready": False, "stores": []}
    return {"ready": True, "stores": _sync_rows(db)}


@router.post("/status-sync/run")
def status_sync_run(db: Session = Depends(get_db)):
    """Check every connected store against eBay now (the 'Check with eBay now' link). Returns at once."""
    if not _sync_tables_ready(db):
        return {"ready": False, "stores": []}
    _start_due_syncs(db, datetime.now(timezone.utc) + timedelta(minutes=1))
    return {"ready": True, "stores": _sync_rows(db)}


def start_status_sync_daily() -> None:
    """Background loop for main.py's start-up: looks every 15 minutes whether the daily check is due (once per day after
    08:00 UTC, catching up if the server was asleep) and starts it. Call once at start-up."""
    def loop() -> None:
        time.sleep(90)
        while True:
            try:
                with Session(get_engine()) as db:
                    if _sync_tables_ready(db):
                        started = _start_due_syncs(db, _sync_cutoff())
                        if started:
                            log.info("daily eBay status check started for %s", ", ".join(started))
            except Exception:
                log.exception("daily status check could not start")
            time.sleep(900)

    threading.Thread(target=loop, name="status-sync-daily", daemon=True).start()
