"""
Amazon listings (Sales channels > Amazon > Listings), modeled on admin_ebay_listings.py + the quantity queue in ebay_sales.py.
Mounted at /admin/amazon-listings/* (owner/admin only).

  POST /sync                 {account}   read the seller's listings from Amazon (Listings Items API search) into amazon_listings
  GET  /repo                 one page of saved listings (filters: account, status, q, linked)
  POST /{id}/link            {product_id}  point a listing at a product (changes our database only)
  POST /{id}/dropship        {on}   drop ship (counts the distributor's stock) for just this listing
  POST /{id}/pieces          {pieces}   units of the product one sale uses (a 3-pack = 3)
  POST /queue/run            {account}  send waiting quantity updates (needs Live pushes AND quantity pushes ON)

enqueue_product(db, product_id) is the hook to call from every place that already calls ebay_sales.enqueue_product /
queue_stock_push, so a sale or stock change on any channel updates Amazon too.
Quantity is the product's availability (app/availability.py) divided by pieces per sale, rounded down. FBA listings are never pushed.
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.amazon_core import MARKETPLACES, NA_ENDPOINT, _row, access_token, sp_get
from app.auth import require_admin
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/amazon-listings", tags=["admin-amazon-listings"], dependencies=[Depends(require_admin)])

QTY_PER_RUN = 20
MAX_ATTEMPTS = 5


def ensure_tables() -> None:
    db = _session_factory()()
    try:
        for stmt in (
            """create table if not exists amazon_listings (
                 id bigserial primary key,
                 account text not null,
                 marketplace text not null default 'US',
                 sku text not null,
                 asin text,
                 title text,
                 product_type text,
                 status text,
                 price numeric,
                 quantity integer,
                 fulfillment text,
                 product_id bigint,
                 dropship_ok boolean not null default false,
                 pieces_per_sale integer not null default 1,
                 issues text,
                 synced_at timestamptz,
                 unique (account, marketplace, sku)
               )""",
            "create index if not exists amazon_listings_product_idx on amazon_listings (product_id)",
            """create table if not exists amazon_qty_queue (
                 id bigserial primary key,
                 listing_id bigint not null,
                 product_id bigint,
                 target_qty integer,
                 status text not null default 'pending',
                 attempts integer not null default 0,
                 last_error text,
                 created_at timestamptz not null default now(),
                 done_at timestamptz
               )""",
            "create unique index if not exists amazon_qty_queue_pending on amazon_qty_queue (listing_id) where status = 'pending'",
        ):
            db.execute(text(stmt))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not set up the Amazon listing tables")
    finally:
        db.close()


class AccountIn(BaseModel):
    account: str


# ---- reading listings ----------------------------------------------------------------------------
def _parse(item: dict, mp: str) -> dict:
    s = ((item.get("summaries") or [{}])[0]) or {}
    offers = item.get("offers") or []
    fa = item.get("fulfillmentAvailability") or []
    code = (fa[0].get("fulfillmentChannelCode") if fa else "") or ""
    price = None
    for o in offers:
        try:
            price = float((o.get("price") or {}).get("amount"))
            break
        except (TypeError, ValueError):
            continue
    status = ",".join(s.get("status") or []) or None
    return {
        "mp": mp, "sku": item.get("sku"), "asin": s.get("asin"), "title": (s.get("itemName") or "")[:300] or None,
        "pt": s.get("productType"), "status": status, "price": price,
        "qty": fa[0].get("quantity") if fa and code == "DEFAULT" else None,
        "ful": "FBA" if code and code != "DEFAULT" else "MFN",
        "iss": json.dumps([i.get("message") for i in (item.get("issues") or [])][:10]),
    }


@router.post("/sync")
def sync(body: AccountIn, db: Session = Depends(get_db)):
    a = _row(db, body.account)
    if not a.seller_id:
        raise HTTPException(409, "Save the seller ID on the account first (Store API > Amazon).")
    seen = errors = 0
    for mp in [m for m in (a.marketplaces or "US").split(",") if m in MARKETPLACES]:
        token = None
        while True:
            params = {
                "marketplaceIds": MARKETPLACES[mp], "includedData": "summaries,offers,fulfillmentAvailability,issues",
                "pageSize": 20,
            }
            if token:
                params["pageToken"] = token
            data = sp_get(db, a.account, f"/listings/2021-08-01/items/{a.seller_id}", params)
            for it in data.get("items") or []:
                r = _parse(it, mp)
                if not r["sku"]:
                    continue
                try:
                    db.execute(
                        text(
                            """insert into amazon_listings (account, marketplace, sku, asin, title, product_type, status, price, quantity,
                                                             fulfillment, issues, synced_at)
                               values (:a, :mp, :sku, :asin, :title, :pt, :status, :price, :qty, :ful, :iss, now())
                               on conflict (account, marketplace, sku) do update set asin = excluded.asin, title = excluded.title,
                                 product_type = excluded.product_type, status = excluded.status, price = excluded.price,
                                 quantity = excluded.quantity, fulfillment = excluded.fulfillment, issues = excluded.issues,
                                 synced_at = now()"""
                        ),
                        {**r, "a": a.account},
                    )
                    seen += 1
                except Exception:
                    db.rollback()
                    errors += 1
                    log.exception("could not save Amazon listing %s", r["sku"])
            db.commit()
            token = (data.get("pagination") or {}).get("nextToken")
            if not token:
                break
    return {"ok": True, "listings": seen, "errors": errors}


@router.get("/repo")
def repo(
    account: str = "", status: str = "", q: str = "", linked: str = Query("", pattern="^(|yes|no)$"),
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
):
    where, p = ["true"], {"lim": limit, "off": offset}
    if account.strip():
        where.append("l.account = :acc")
        p["acc"] = account.strip()
    if status.strip():
        where.append("l.status ilike :st")
        p["st"] = f"%{status.strip()}%"
    if linked:
        where.append("l.product_id is not null" if linked == "yes" else "l.product_id is null")
    for i, w in enumerate(q.split()[:5]):
        where.append(f"(l.sku ilike :w{i} or l.asin ilike :w{i} or l.title ilike :w{i} or p.sku_display ilike :w{i} or p.title ilike :w{i})")
        p[f"w{i}"] = f"%{w}%"
    w = " and ".join(where)
    total = db.execute(text(f"select count(*) from amazon_listings l left join products p on p.id = l.product_id where {w}"), p).scalar()
    rows = db.execute(
        text(
            f"select l.id, l.account, l.marketplace, l.sku, l.asin, coalesce(l.title, p.title) as title, l.status, l.price, l.quantity, "
            f"l.fulfillment, l.product_id, p.sku_display as product_sku, l.dropship_ok, l.pieces_per_sale, l.issues, l.synced_at "
            f"from amazon_listings l left join products p on p.id = l.product_id where {w} order by l.id desc limit :lim offset :off"
        ),
        p,
    ).all()
    return {"total": total, "items": [dict(r._mapping) for r in rows]}


class LinkIn(BaseModel):
    product_id: int


@router.post("/{listing_id}/link")
def link(listing_id: int, body: LinkIn, db: Session = Depends(get_db)):
    if not db.execute(text("select 1 from products where id = :p"), {"p": body.product_id}).first():
        raise HTTPException(404, "That product does not exist.")
    r = db.execute(text("update amazon_listings set product_id = :p where id = :l returning id"), {"p": body.product_id, "l": listing_id}).first()
    if not r:
        raise HTTPException(404, "Listing not found.")
    queued = enqueue_product(db, body.product_id)
    db.commit()
    return {"ok": True, "queued": queued}


class OnIn(BaseModel):
    on: bool


@router.post("/{listing_id}/dropship")
def set_dropship(listing_id: int, body: OnIn, db: Session = Depends(get_db)):
    from app.availability import has_distributor

    r = db.execute(text("select product_id, account, fulfillment from amazon_listings where id = :l"), {"l": listing_id}).first()
    if not r:
        raise HTTPException(404, "Listing not found.")
    if body.on:
        if r.fulfillment == "FBA":
            raise HTTPException(409, "An FBA listing is fulfilled by Amazon and cannot be drop shipped.")
        if not r.product_id or not has_distributor(db, int(r.product_id)):
            raise HTTPException(409, "Link the listing to a product whose parts are all matched to the distributor catalog first.")
        if not db.execute(text("select dropship_policy_ack from amazon_accounts where account = :a"), {"a": r.account}).scalar():
            raise HTTPException(409, "Acknowledge Amazon's drop shipping policy (you must be the seller of record) on the account first.")
    db.execute(text("update amazon_listings set dropship_ok = :v where id = :l"), {"v": body.on, "l": listing_id})
    queued = enqueue_product(db, r.product_id) if r.product_id else 0
    db.commit()
    return {"dropship_ok": body.on, "queued": queued}


class PiecesIn(BaseModel):
    pieces: int = Field(..., ge=1, le=999)


@router.post("/{listing_id}/pieces")
def set_pieces(listing_id: int, body: PiecesIn, db: Session = Depends(get_db)):
    r = db.execute(text("update amazon_listings set pieces_per_sale = :n where id = :l returning product_id"), {"n": body.pieces, "l": listing_id}).first()
    if not r:
        raise HTTPException(404, "Listing not found.")
    queued = enqueue_product(db, r.product_id) if r.product_id else 0
    db.commit()
    return {"pieces": body.pieces, "queued": queued}


# ---- quantity queue ------------------------------------------------------------------------------
def _target(db: Session, product_id: int, dropship: bool, pieces: int) -> int:
    from app.availability import effective, packs

    return packs(effective(db, product_id, dropship), pieces)


def enqueue_product(db: Session, product_id: int) -> int:
    """Queue a quantity update for each MFN Amazon listing of this product, on accounts where both pushes are ON. Never raises."""
    try:
        with db.begin_nested():
            rows = db.execute(
                text(
                    "select l.id, l.dropship_ok, l.pieces_per_sale from amazon_listings l join amazon_accounts a on a.account = l.account "
                    "where l.product_id = :p and l.fulfillment = 'MFN' and a.live_pushes_enabled and a.sales_qty_push_enabled "
                    "and a.refresh_token_enc is not null"
                ),
                {"p": product_id},
            ).all()
            for r in rows:
                db.execute(
                    text(
                        "insert into amazon_qty_queue (listing_id, product_id, target_qty) values (:l, :p, :q) "
                        "on conflict (listing_id) where status = 'pending' do update set target_qty = excluded.target_qty"
                    ),
                    {"l": r.id, "p": product_id, "q": _target(db, product_id, bool(r.dropship_ok), int(r.pieces_per_sale or 1))},
                )
            return len(rows)
    except Exception:
        log.exception("could not queue Amazon quantities for product %s", product_id)
        return 0


def _patch_qty(db: Session, l, qty: int) -> None:
    a = _row(db, l.account)
    if not a.live_pushes_enabled:
        raise HTTPException(409, "Live pushes are OFF for this Amazon account.")
    if not l.product_type:
        raise HTTPException(409, "Amazon did not give this listing's product type; sync the listings again.")
    body = {
        "productType": l.product_type,
        "patches": [{"op": "replace", "path": "/attributes/fulfillment_availability",
                     "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": int(qty)}]}],
    }
    url = (f"{NA_ENDPOINT}/listings/2021-08-01/items/{a.seller_id}/{urllib.parse.quote(l.sku, safe='')}"
           f"?marketplaceIds={MARKETPLACES[l.marketplace]}")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="PATCH",
        headers={"x-amz-access-token": access_token(db, l.account), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            res = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"Amazon refused it ({e.code}): {e.read().decode('utf-8', 'replace')[:200]}")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise HTTPException(502, "Could not reach Amazon.")
    if str(res.get("status", "")).upper() == "INVALID":
        raise HTTPException(502, "Amazon marked the update invalid: " + json.dumps(res.get("issues"))[:200])


def run_queue(db: Session, account: str, limit: int = QTY_PER_RUN) -> dict:
    out = {"done": 0, "skipped": 0, "failed": 0}
    rows = db.execute(
        text(
            "select q.id, q.attempts, l.id as lid, l.account, l.marketplace, l.sku, l.product_type, l.product_id, l.dropship_ok, "
            "l.pieces_per_sale, l.quantity, l.fulfillment from amazon_qty_queue q join amazon_listings l on l.id = q.listing_id "
            "where q.status = 'pending' and l.account = :a order by q.id limit :n"
        ),
        {"a": account, "n": limit},
    ).all()
    for r in rows:
        status, note = "done", None
        target = _target(db, r.product_id, bool(r.dropship_ok), int(r.pieces_per_sale or 1)) if r.product_id else 0
        try:
            if r.fulfillment != "MFN":
                status, note = "skipped", "FBA listing: Amazon holds the stock"
            elif r.quantity != target:
                _patch_qty(db, r, target)
                db.execute(text("update amazon_listings set quantity = :q where id = :l"), {"q": target, "l": r.lid})
        except HTTPException as e:
            status, note = ("failed" if r.attempts + 1 >= MAX_ATTEMPTS else "pending"), str(e.detail)[:300]
        except Exception as e:
            status, note = ("failed" if r.attempts + 1 >= MAX_ATTEMPTS else "pending"), type(e).__name__
        db.execute(
            text(
                "update amazon_qty_queue set status = :s, attempts = attempts + 1, last_error = :n, target_qty = :t, "
                "done_at = case when :s in ('done','skipped','failed') then now() end where id = :i"
            ),
            {"s": status, "n": note, "t": target, "i": r.id},
        )
        db.commit()
        if status in out:
            out[status] += 1
    return out


@router.post("/queue/run")
def queue_run(body: AccountIn, db: Session = Depends(get_db)):
    return run_queue(db, _row(db, body.account).account)
