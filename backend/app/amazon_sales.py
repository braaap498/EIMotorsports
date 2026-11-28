"""
Amazon orders (Sales channels > Amazon > Orders), modeled on ebay_sales.py. Mounted at /admin/amazon-sales/* (owner/admin only).

  POST /poll-now        {account, days?}   read orders changed since the last check (Orders API v2026-01-01, searchOrders)
  GET  /lines           the Orders page: every order line (filters: account, state, q, dates)
  POST /lines/{id}/release   stop setting the parts aside (shelf count unchanged)

HOW A SALE FLOWS (same as eBay)
  1. A check asks Amazon for orders changed since the last good check (15 minutes of overlap).
  2. Each order is saved (amazon_orders) with one line per item (amazon_order_lines), linked to a product through the listing's SKU.
  3. A line that is paid (not Pending/Canceled) and linked sets the parts aside (stock_reservations). The shelf count changes when the
     part is picked, as with eBay. A queued quantity update goes to every other channel listing of that product.
  4. A canceled order that has not been picked releases its parts. A line with no product link is kept as 'unmatched' and retried.
  5. A line on a listing marked drop ship is NOT set aside: it goes to state 'dropship' (the drop ship chain takes it from there).
  6. A Fulfilled-by-Amazon (FBA) order, or a line on an FBA listing, is NEVER set aside or picked: Amazon holds and ships that stock, so the
     line goes straight to history and "Mark shipped" refuses it. It still counts in Orders and Analytics.

Built since: Mark picked, shipment confirmation, background loop, the employee-facing ship_router, scan-out support
(take_stock_for_reference), drop ship hooks (mark_lines_dropship / undo_lines_dropship), sales_transactions rows, analytics rebuild
after a poll. NOT BUILT YET: refunds.
The field names read from the v2026-01-01 response are read defensively and are UNVERIFIED against a live response: run Poll now
once, then compare amazon_orders.raw_sample with what the parser picked up.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.amazon_core import MARKETPLACES, _row, sp_get
from app.auth import AuthUser, needs, require_admin, require_route_access
from app.database import _session_factory, get_db
from app.stock import lock_products, take_kit_components

log = logging.getLogger(__name__)
router = APIRouter(prefix="/amazon-sales", tags=["admin-amazon-sales"], dependencies=[Depends(require_admin)])
# The same pick / release / ship actions for employees who hold inventory.ship (Inventory > Pick & ship), like eBay's ship_router.
ship_router = APIRouter(prefix="/inventory/amazon-orders", tags=["inventory-amazon-orders"], dependencies=[Depends(require_route_access)])

OVERLAP = timedelta(minutes=15)
FIRST_LOOKBACK_DAYS = 3
MAX_DAYS = 89


def ensure_tables() -> None:
    db = _session_factory()()
    try:
        for stmt in (
            """create table if not exists amazon_orders (
                 id bigserial primary key,
                 account text not null,
                 marketplace text,
                 amazon_order_id text not null,
                 status text,
                 created_at_amazon timestamptz,
                 paid_at timestamptz,
                 modified_at_amazon timestamptz,
                 shipped_at timestamptz,
                 cancelled boolean not null default false,
                 buyer_state text,
                 buyer_zip text,
                 buyer_country text,
                 total numeric,
                 shipping_charged numeric,
                 tax_total numeric,
                 fulfillment text,
                 ship_service text,
                 tracking_number text,
                 tracking_carrier text,
                 raw_sample text,
                 updated_at timestamptz not null default now(),
                 unique (account, amazon_order_id)
               )""",
            """create table if not exists amazon_order_lines (
                 id bigserial primary key,
                 order_pk bigint not null references amazon_orders(id) on delete cascade,
                 account text not null,
                 amazon_order_id text not null,
                 line_key text not null,
                 sku text,
                 asin text,
                 title text,
                 quantity integer not null default 1,
                 unit_price numeric,
                 fees numeric,
                 sale_date timestamptz,
                 state text not null default 'pending',
                 product_id bigint,
                 pieces_per_sale integer,
                 created_at timestamptz not null default now(),
                 unique (order_pk, line_key)
               )""",
            "alter table stock_reservations add column if not exists amazon_order_line_id bigint",
            "create unique index if not exists stock_res_amazon_line on stock_reservations (amazon_order_line_id) where amazon_order_line_id is not null",
            "alter table amazon_order_lines add column if not exists sales_transaction_id bigint",
            "alter table amazon_accounts add column if not exists channel_id bigint",
            "alter table amazon_accounts add column if not exists sales_cursor timestamptz",
            "alter table amazon_accounts add column if not exists sales_last_polled_at timestamptz",
            "alter table amazon_accounts add column if not exists sales_last_summary text",
            "alter table amazon_accounts add column if not exists sales_last_error text",
        ):
            db.execute(text(stmt))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not set up the Amazon order tables")
    finally:
        db.close()


# ---- parsing (defensive) ---------------------------------------------------------------------------
def _dt(v) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _money(v) -> Optional[Decimal]:
    if isinstance(v, dict):
        v = v.get("amount") or v.get("value")
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except InvalidOperation:
        return None


def _first(d: dict, *names):
    for n in names:
        if isinstance(d, dict) and d.get(n) not in (None, ""):
            return d[n]
    return None


def _fulfillment_text(o: dict) -> str:
    """How Amazon says the order is fulfilled, as text. Reads the flat field names and a nested `fulfillment` object (UNVERIFIED names: compare raw_sample)."""
    f = _first(o, "fulfillmentType", "fulfillmentChannel")
    if f is None:
        nested = o.get("fulfillment") if isinstance(o, dict) else None
        if isinstance(nested, str):
            f = nested
        elif isinstance(nested, dict):
            f = _first(nested, "fulfilledBy", "fulfillmentChannel", "fulfillmentType", "channel")
    return str(f or "")


def is_fba_text(v) -> bool:
    """True for Amazon's 'fulfilled by Amazon' values: AFN (Orders v0), AMAZON / AMAZON_NA (newer APIs), or our own FBA label."""
    t = str(v or "").strip().upper().replace(" ", "_")
    return t in ("AFN", "FBA") or t.startswith("AMAZON")


def _listing_is_fba(db: Session, account: str, sku: Optional[str]) -> bool:
    """Second opinion from the listing: the listing sync marks a listing FBA when Amazon holds its stock."""
    if not sku:
        return False
    r = db.execute(text("select 1 from amazon_listings where account = :a and sku = :s and fulfillment = 'FBA' limit 1"), {"a": account, "s": sku}).first()
    return r is not None


def _shipping_paid(o: dict, items: list) -> Optional[Decimal]:
    """What the buyer paid for shipping, or None when this order does not say (None keeps drop ship at the review step).
    UNVERIFIED field names: compare amazon_orders.raw_sample, then trim the names to the real ones."""
    names = ("shippingPrice", "shippingCharge", "shippingCharges", "shippingFee", "shipping")
    seen, total = False, Decimal(0)
    for it in items:
        if not isinstance(it, dict):
            continue
        for src in (it, it.get("proceeds") or {}, it.get("itemPrice") if isinstance(it.get("itemPrice"), dict) else {}):
            v = _first(src, *names) if isinstance(src, dict) else None
            m = _money(v)
            if m is not None:
                seen, total = True, total + m
                break
    if seen:
        return total
    m = _money(_first(o, *names))
    return m


def _parse_order(o: dict) -> dict:
    items = _first(o, "orderItems", "items", "lineItems") or []
    addr = (_first(o, "recipient", "shippingAddress", "deliveryAddress") or {})
    addr = _first(addr, "deliveryAddress") or addr if isinstance(addr, dict) else {}
    status = str(_first(o, "orderStatus", "status", "fulfillmentStatus") or "")
    lines = []
    for n, it in enumerate(items):
        prod = it.get("product") or {}
        qty = _first(it, "quantityOrdered", "quantity") or 1
        lines.append(
            {
                "key": str(_first(it, "orderItemId", "lineItemId", "id") or n),
                "sku": _first(prod, "sellerSku", "sku") or _first(it, "sellerSku", "sku"),
                "asin": _first(prod, "asin") or _first(it, "asin"),
                "title": (_first(prod, "title") or _first(it, "title") or "")[:300] or None,
                "qty": int(qty),
                "price": _money(_first(it, "itemPrice", "unitPrice", "price")),
            }
        )
    ship_paid = _shipping_paid(o, items)
    return {
        "ship_paid": ship_paid,
        "id": str(_first(o, "orderId", "amazonOrderId")),
        "status": status,
        "created": _dt(_first(o, "createdTime", "purchaseDate", "createdAt")),
        "modified": _dt(_first(o, "lastUpdatedTime", "lastUpdateDate", "updatedAt")),
        "mp": _first(o, "marketplaceId"),
        "state": _first(addr, "stateOrRegion", "state"),
        "zip": _first(addr, "postalCode", "zip"),
        "country": _first(addr, "countryCode", "country"),
        "total": _money(_first(o, "orderTotal", "total")),
        "fulfillment": _fulfillment_text(o),
        "lines": lines,
    }


def _is_cancelled(status: str) -> bool:
    return status.lower().replace("_", "") in ("canceled", "cancelled")


def _is_unpaid(status: str) -> bool:
    return status.lower().replace("_", "") in ("pending", "pendingavailability", "unfulfillable")


# ---- saving + stock ---------------------------------------------------------------------------------
def _match_product(db: Session, account: str, sku: Optional[str], asin: Optional[str]) -> Optional[int]:
    r = db.execute(
        text("select product_id from amazon_listings where account = :a and product_id is not null and (sku = :s or (:n is not null and asin = :n)) limit 1"),
        {"a": account, "s": sku, "n": asin},
    ).first()
    return r.product_id if r else None


def _listing_flags(db: Session, account: str, sku: Optional[str]):
    r = db.execute(
        text("select dropship_ok, pieces_per_sale from amazon_listings where account = :a and sku = :s limit 1"), {"a": account, "s": sku}
    ).first()
    return (bool(r.dropship_ok), int(r.pieces_per_sale or 1)) if r else (False, 1)


def _release(db: Session, line_id: int) -> None:
    db.execute(text("delete from stock_reservations where amazon_order_line_id = :l"), {"l": line_id})


def _record_sale(db: Session, ln, od: dict, pid: int, pieces: int) -> None:
    """Write the Amazon sale to sales_transactions the way eBay does, so Analytics sees it.
    Only when the account has a channel_id (amazon_accounts.channel_id, set by hand from your channels table): without it nothing is written.
    A failure here is logged and skipped; it never stops the order from being saved."""
    if getattr(ln, "sales_transaction_id", None):
        return
    try:
        ch = db.execute(text("select channel_id from amazon_accounts where account = :a"), {"a": ln.account}).scalar()
        if ch is None:
            return
        with db.begin_nested():
            sid = db.execute(
                text(
                    """insert into sales_transactions
                         (product_id, channel_id, quantity_sold, unit_sale_price, unit_cost_at_sale, shipping_charged,
                          platform_fees, sale_date, order_reference, ship_to_state, ship_to_zip)
                       values (:p, :ch, :q, :price,
                               (select nullif(to_jsonb(p)->>'cost', '')::numeric * :pc from products p where p.id = :p),
                               case when :l = (select min(x.id) from amazon_order_lines x where x.order_pk = (select order_pk from amazon_order_lines where id = :l))
                                    then coalesce((select o.shipping_charged from amazon_orders o join amazon_order_lines y on y.order_pk = o.id where y.id = :l), 0)
                                    else 0 end,
                               null, :d, :ref, :st, :zip)
                       returning id"""
                ),
                {"p": pid, "ch": ch, "q": ln.quantity, "price": getattr(ln, "unit_price", None), "pc": pieces,
                 "l": ln.id, "d": getattr(ln, "sale_date", None), "ref": ln.amazon_order_id, "st": od.get("state"), "zip": od.get("zip")},
            ).scalar()
            db.execute(text("update amazon_order_lines set sales_transaction_id = :s where id = :l"), {"s": sid, "l": ln.id})
    except Exception:
        log.exception("could not record the Amazon sale for line %s", ln.id)


def _apply_line(db: Session, ln, od: dict) -> Optional[int]:
    """Move one line to the state it should be in. Returns the product id whose stock changed, or None."""
    if od["cancelled"]:
        if ln.state in ("pending", "reserved", "unmatched", "dropship"):
            _release(db, ln.id)
            sid = db.execute(text("select sales_transaction_id from amazon_order_lines where id = :l"), {"l": ln.id}).scalar()
            if sid:
                db.execute(text("delete from sales_transactions where id = :s"), {"s": sid})
                db.execute(text("update amazon_order_lines set sales_transaction_id = null where id = :l"), {"l": ln.id})
            db.execute(text("update amazon_order_lines set state = 'cancelled' where id = :l"), {"l": ln.id})
            return ln.product_id
        return None
    if ln.state in ("picked", "history", "cancelled", "reserved", "dropship") or od["unpaid"]:
        return None
    pid = ln.product_id or _match_product(db, ln.account, ln.sku, ln.asin)
    if pid is None:
        db.execute(text("update amazon_order_lines set state = 'unmatched' where id = :l"), {"l": ln.id})
        return None
    dropship, pieces = _listing_flags(db, ln.account, ln.sku)
    _record_sale(db, ln, od, pid, pieces)  # no-op until amazon_accounts.channel_id is set
    if is_fba_text(od.get("fulfillment")) or _listing_is_fba(db, ln.account, ln.sku):
        # Fulfilled by Amazon: Amazon holds and ships this stock, so nothing is set aside here, there is nothing to pick or confirm,
        # and the shelf count is never touched. The line goes straight to history (it is still counted in Orders and Analytics).
        db.execute(text("update amazon_order_lines set state = 'history', product_id = :p, pieces_per_sale = :pc where id = :l"), {"p": pid, "pc": pieces, "l": ln.id})
        return None
    if od["shipped"]:
        db.execute(text("update amazon_order_lines set state = 'history', product_id = :p, pieces_per_sale = :pc where id = :l"), {"p": pid, "pc": pieces, "l": ln.id})
        return None
    if dropship:
        db.execute(text("update amazon_order_lines set state = 'dropship', product_id = :p, pieces_per_sale = :pc where id = :l"), {"p": pid, "pc": pieces, "l": ln.id})
        return pid
    lock_products(db, [pid])
    db.execute(
        text(
            "insert into stock_reservations (order_id, product_id, quantity, status, expires_at, amazon_order_line_id) "
            "values (null, :p, :q, 'reserved', null, :l) on conflict (amazon_order_line_id) where amazon_order_line_id is not null do nothing"
        ),
        {"p": pid, "q": int(ln.quantity) * pieces, "l": ln.id},
    )
    db.execute(text("update amazon_order_lines set state = 'reserved', product_id = :p, pieces_per_sale = :pc where id = :l"), {"p": pid, "pc": pieces, "l": ln.id})
    return pid


def _save_order(db: Session, account: str, o: dict, raw: dict, first_run: bool) -> set:
    cancelled = _is_cancelled(o["status"])
    shipped = o["status"].lower() in ("shipped", "delivered", "partiallyshipped")
    row = db.execute(
        text(
            """insert into amazon_orders (account, marketplace, amazon_order_id, status, created_at_amazon, paid_at, modified_at_amazon, shipped_at,
                                          cancelled, buyer_state, buyer_zip, buyer_country, total, fulfillment, raw_sample, shipping_charged)
               values (:a, :mp, :o, :st, :cr, :paid, :mod, :sh, :can, :bs, :bz, :bc, :tot, :ful, :raw, :shp)
               on conflict (account, amazon_order_id) do update set status = excluded.status, modified_at_amazon = excluded.modified_at_amazon,
                 shipped_at = coalesce(excluded.shipped_at, amazon_orders.shipped_at), cancelled = excluded.cancelled,
                 total = coalesce(excluded.total, amazon_orders.total),
                 shipping_charged = coalesce(excluded.shipping_charged, amazon_orders.shipping_charged), updated_at = now()
               returning id"""
        ),
        {
            "a": account, "mp": o["mp"], "o": o["id"], "st": o["status"], "cr": o["created"], "paid": None if _is_unpaid(o["status"]) else o["created"],
            "mod": o["modified"], "sh": o["modified"] if shipped else None, "can": cancelled, "bs": o["state"], "bz": o["zip"],
            "bc": o["country"], "tot": o["total"], "ful": o["fulfillment"], "raw": json.dumps(raw, default=str)[:2000], "shp": o.get("ship_paid"),
        },
    ).first()
    od = {"cancelled": cancelled, "unpaid": _is_unpaid(o["status"]), "shipped": shipped, "state": o["state"], "zip": o["zip"]}
    changed: set = set()
    for l in o["lines"]:
        db.execute(
            text(
                "insert into amazon_order_lines (order_pk, account, amazon_order_id, line_key, sku, asin, title, quantity, unit_price, sale_date) "
                "values (:op, :a, :o, :k, :sku, :asin, :t, :q, :pr, :d) on conflict (order_pk, line_key) do nothing"
            ),
            {"op": row.id, "a": account, "o": o["id"], "k": l["key"], "sku": l["sku"], "asin": l["asin"], "t": l["title"], "q": l["qty"], "pr": l["price"], "d": o["created"]},
        )
        ln = db.execute(
            text("select id, state, product_id, quantity, sku, asin, account, amazon_order_id, unit_price, sale_date, sales_transaction_id from amazon_order_lines where order_pk = :op and line_key = :k"),
            {"op": row.id, "k": l["key"]},
        ).first()
        pid = _apply_line(db, ln, od)
        if pid:
            changed.add(pid)
    return changed


def poll_account(db: Session, account: str, days: Optional[int] = None) -> dict:
    a = _row(db, account)
    if not a.refresh_token_enc:
        raise HTTPException(409, "Save this Amazon account's key first.")
    now = datetime.now(timezone.utc)
    first_run = bool(days) or not a.sales_cursor
    since = now - timedelta(days=min(days, MAX_DAYS)) if days else (a.sales_cursor - OVERLAP if a.sales_cursor else now - timedelta(days=FIRST_LOOKBACK_DAYS))
    seen, errors, changed = 0, 0, set()
    try:
        for mp in [m for m in (a.marketplaces or "US").split(",") if m in MARKETPLACES]:
            token = None
            while True:
                params = {"marketplaceIds": MARKETPLACES[mp], "lastUpdatedAfter": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "paginationToken": token}
                data = sp_get(db, a.account, "/orders/2026-01-01/orders", {k: v for k, v in params.items() if v})
                for raw in data.get("orders") or []:
                    o = _parse_order(raw)
                    if not o["id"] or o["id"] == "None":
                        continue
                    try:
                        changed |= _save_order(db, a.account, o, raw, first_run)
                        db.commit()
                        seen += 1
                    except Exception:
                        db.rollback()
                        errors += 1
                        log.exception("Amazon order %s could not be saved", o["id"])
                token = (data.get("pagination") or {}).get("nextToken")
                if not token:
                    break
        from app.amazon_listings import enqueue_product

        for pid in changed:
            enqueue_product(db, pid)
        try:  # keep the other channels in step too
            from app.ebay_sales import enqueue_product as ebay_enqueue

            for p in changed:
                ebay_enqueue(db, p)
        except Exception:
            log.exception("could not queue eBay quantities after Amazon orders")
        msg = f"{seen} orders checked, {len(changed)} products changed" + (f", {errors} could not be saved (see server log)" if errors else "")
        db.execute(text("update amazon_accounts set sales_cursor = :c, sales_last_summary = :m, sales_last_error = null, sales_last_polled_at = now() where account = :a"), {"c": now, "m": msg, "a": a.account})
        db.commit()
        return {"ok": True, "summary": msg}
    except HTTPException as e:
        db.rollback()
        db.execute(text("update amazon_accounts set sales_last_error = :e, sales_last_polled_at = now() where account = :a"), {"e": str(e.detail)[:400], "a": a.account})
        db.commit()
        raise


class BackfillIn(BaseModel):
    account: str


@router.post("/backfill-sales")
def backfill_sales(body: BackfillIn, db: Session = Depends(get_db)):
    """Write sales_transactions rows for Amazon lines saved before amazon_accounts.channel_id was set. Safe to repeat: lines that have a row are skipped."""
    _row(db, body.account)
    if db.execute(text("select channel_id from amazon_accounts where account = :a"), {"a": body.account}).scalar() is None:
        raise HTTPException(409, "Set this account's channel_id first (the Amazon row of your channels table).")
    rows = db.execute(
        text(
            "select l.id, l.account, l.amazon_order_id, l.quantity, l.unit_price, l.sale_date, l.product_id, l.pieces_per_sale, l.sales_transaction_id, "
            "o.buyer_state, o.buyer_zip from amazon_order_lines l join amazon_orders o on o.id = l.order_pk "
            "where l.account = :a and l.sales_transaction_id is null and l.product_id is not null and not o.cancelled "
            "and l.state in ('reserved', 'dropship', 'picked', 'history') order by l.id"
        ),
        {"a": body.account},
    ).all()
    done = 0
    for r in rows:
        _record_sale(db, r, {"state": r.buyer_state, "zip": r.buyer_zip}, r.product_id, int(r.pieces_per_sale or 1))
        done += 1
    db.commit()
    left = db.execute(text("select count(*) from amazon_order_lines where account = :a and sales_transaction_id is null and product_id is not null and state in ('reserved','dropship','picked','history')"), {"a": body.account}).scalar()
    return {"ok": True, "tried": done, "still_without_row": int(left or 0)}


class PollIn(BaseModel):
    account: str
    days: Optional[int] = Field(None, ge=1, le=MAX_DAYS)


@router.post("/poll-now")
def poll_now(body: PollIn, db: Session = Depends(get_db)):
    return poll_account(db, body.account, body.days)


@router.get("/lines")
def lines(
    account: str = "", state: str = "", q: str = "", date_from: str = "", date_to: str = "",
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
):
    where, p = ["true"], {"lim": limit, "off": offset}
    if account.strip():
        where.append("l.account = :acc")
        p["acc"] = account.strip()
    if state.strip():
        where.append("l.state = :st")
        p["st"] = state.strip()
    for key, raw, op in (("df", date_from, ">="), ("dt", date_to, "<")):
        if raw.strip():
            try:
                d = datetime.fromisoformat(raw.strip())
            except ValueError:
                raise HTTPException(422, "Dates must look like 2026-11-07.")
            p[key] = d if key == "df" else d + timedelta(days=1)
            where.append(f"l.sale_date {op} :{key}")
    if q.strip():
        where.append("(l.amazon_order_id ilike :q or l.sku ilike :q or l.asin ilike :q or l.title ilike :q or o.tracking_number ilike :q)")
        p["q"] = f"%{q.strip()}%"
    w = " and ".join(where)
    total = db.execute(text(f"select count(*) from amazon_order_lines l join amazon_orders o on o.id = l.order_pk where {w}"), p).scalar()
    rows = db.execute(
        text(
            f"select l.id, l.order_pk, l.account, l.amazon_order_id, l.sku, l.asin, coalesce(l.title, pr.title) as title, l.quantity, l.unit_price, l.state, "
            f"l.product_id, pr.sku_display as product_sku, coalesce(l.pieces_per_sale, 1) as pieces_per_sale, l.sale_date, o.cancelled, "
            f"o.shipped_at, o.tracking_number, o.tracking_carrier, o.ship_service, o.fulfillment "
            f"from amazon_order_lines l join amazon_orders o on o.id = l.order_pk left join products pr on pr.id = l.product_id "
            f"where {w} order by l.sale_date desc nulls last, l.id desc limit :lim offset :off"
        ),
        p,
    ).all()
    return {"total": total, "items": [{**dict(r._mapping), "is_fba": is_fba_text(r.fulfillment)} for r in rows]}


@ship_router.post("/lines/{line_id}/release")
@router.post("/lines/{line_id}/release")
@needs("inventory.ship")
def release_line(line_id: int, db: Session = Depends(get_db)):
    ln = db.execute(text("select id, product_id, state from amazon_order_lines where id = :l for update"), {"l": line_id}).first()
    if not ln or ln.state != "reserved":
        raise HTTPException(409, "Only a line whose parts are set aside can be released.")
    _release(db, line_id)
    db.execute(text("update amazon_order_lines set state = 'history' where id = :l"), {"l": line_id})
    if ln.product_id:
        from app.amazon_listings import enqueue_product

        enqueue_product(db, ln.product_id)
    db.commit()
    return {"ok": True}


# ---- picking, shipment confirmation, background loop (step 4) ---------------------------------------
import json as _json
import re
import threading
import time
import urllib.error
import urllib.request

from app import order_activity
from app.amazon_core import NA_ENDPOINT, access_token
from app.auth import current_user

CARRIERS = {"usps": "USPS", "ups": "UPS", "fedex": "FedEx", "dhl": "DHL"}
_TRACK_OK = re.compile(r"^[A-Za-z0-9\-]{6,60}$")


@ship_router.post("/lines/{line_id}/pick")
@router.post("/lines/{line_id}/pick")
@needs("inventory.ship")
def pick_line(line_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    """Take this line's parts off the shelf now (the part has left the building). Same stock effect as eBay's Mark picked."""
    ln = db.execute(
        text("select l.id, l.product_id, l.state, l.amazon_order_id, l.quantity * coalesce(l.pieces_per_sale, 1) as units from amazon_order_lines l where l.id = :l for update"),
        {"l": line_id},
    ).first()
    if not ln or ln.state != "reserved" or not ln.product_id:
        raise HTTPException(409, "Only a line whose parts are set aside can be marked picked.")
    lock_products(db, [ln.product_id])
    res = db.execute(text("select id, quantity from stock_reservations where amazon_order_line_id = :l and status = 'reserved' for update"), {"l": line_id}).first()
    if not res:
        raise HTTPException(409, "No parts are set aside for that line.")
    take = min(int(ln.units), int(res.quantity))
    before = db.execute(text("select coalesce(quantity_on_hand, 0) from products where id = :p"), {"p": ln.product_id}).scalar()
    after = db.execute(
        text("update products set quantity_on_hand = greatest(0, coalesce(quantity_on_hand, 0) - :t), updated_at = now() where id = :p returning quantity_on_hand"),
        {"t": take, "p": ln.product_id},
    ).scalar()
    if after != before:
        db.execute(
            text("insert into inventory_transactions (product_id, transaction_type, quantity_change, reference, legacy_created_by) values (:p, 'Adjustment', :d, :r, :by)"),
            {"p": ln.product_id, "d": after - before, "r": f"Picked for Amazon order {ln.amazon_order_id}", "by": user.email},
        )
    take_kit_components(db, ln.product_id, take, ln.amazon_order_id, user.email)  # a kit's parts come off the single-part products
    db.execute(text("delete from stock_reservations where id = :i"), {"i": res.id})
    db.execute(text("update amazon_order_lines set state = 'picked' where id = :l"), {"l": line_id})
    order_activity.log_action(db, user, "picked", "Amazon", ln.amazon_order_id, f"{take} unit(s) (marked picked without scanning)")
    db.commit()
    return {"ok": True}


class ShipIn(BaseModel):
    carrier: str = Field(min_length=2, max_length=20)
    tracking_number: str = Field(min_length=1, max_length=80)


@ship_router.post("/orders/{order_pk}/ship")
@router.post("/orders/{order_pk}/ship")
@needs("inventory.ship")
def mark_shipped(order_pk: int, body: ShipIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    """Tell Amazon this order shipped, with tracking (Orders API v0 confirmShipment). Needs Live pushes ON. UNVERIFIED against a live call."""
    od = db.execute(text("select id, account, marketplace, amazon_order_id, cancelled, fulfillment from amazon_orders where id = :i"), {"i": order_pk}).first()
    if not od:
        raise HTTPException(404, "That order is not in the list.")
    if od.cancelled:
        raise HTTPException(409, "That order is cancelled.")
    if is_fba_text(od.fulfillment) or db.execute(
        text("select 1 from amazon_order_lines l join amazon_listings g on g.account = l.account and g.sku = l.sku where l.order_pk = :o and g.fulfillment = 'FBA' limit 1"),
        {"o": order_pk},
    ).first():
        raise HTTPException(409, "Amazon ships this order itself (Fulfilled by Amazon), so there is no shipment to confirm.")
    acct = _row(db, od.account)
    if not acct.live_pushes_enabled:
        raise HTTPException(409, "Live pushes are OFF for this Amazon account.")
    carrier = CARRIERS.get(body.carrier.strip().lower()) or body.carrier.strip()[:20]
    tracking = re.sub(r"\s+", "", body.tracking_number)
    if not _TRACK_OK.match(tracking):
        raise HTTPException(422, "The tracking number should be 6 to 60 letters, numbers or dashes, with no spaces.")
    items = db.execute(text("select line_key, quantity from amazon_order_lines where order_pk = :o and state <> 'cancelled'"), {"o": order_pk}).all()
    payload = {
        "marketplaceId": MARKETPLACES.get(od.marketplace or "US", MARKETPLACES["US"]) if len(str(od.marketplace or "")) < 5 else od.marketplace,
        "packageDetail": {
            "packageReferenceId": 1, "carrierCode": carrier, "trackingNumber": tracking,
            "shipDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "orderItems": [{"orderItemId": i.line_key, "quantity": int(i.quantity)} for i in items],
        },
    }
    req = urllib.request.Request(
        f"{NA_ENDPOINT}/orders/v0/orders/{od.amazon_order_id}/shipmentConfirmation", data=_json.dumps(payload).encode(), method="POST",
        headers={"x-amz-access-token": access_token(db, od.account), "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=25).read()
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"Amazon did not accept it ({e.code}): {e.read().decode('utf-8', 'replace')[:200]}")
    except (urllib.error.URLError, TimeoutError):
        raise HTTPException(502, "Could not reach Amazon.")
    db.execute(
        text("update amazon_orders set tracking_number = :t, tracking_carrier = :c, shipped_at = coalesce(shipped_at, now()), updated_at = now() where id = :i"),
        {"t": tracking, "c": carrier, "i": order_pk},
    )
    order_activity.log_action(db, user, "tracking_shipped", "Amazon", od.amazon_order_id, f"{carrier} {tracking}")
    db.commit()
    return {"ok": True, "tracking_number": tracking, "tracking_carrier": carrier}


def mark_lines_dropship(db: Session, amazon_order_id: str, product_ids) -> int:
    """A drop ship PO was made for these products of this Amazon order: the distributor fills them, not our shelf. Lines go to
    'dropship', any shelf hold is dropped (the shelf count is NOT changed) and the listing quantities are recalculated."""
    pids = sorted({int(p) for p in (product_ids or []) if p is not None})
    if not pids or not amazon_order_id:
        return 0
    rows = db.execute(
        text("select id, product_id from amazon_order_lines where amazon_order_id = :o and state in ('reserved', 'dropship') and product_id = any(cast(:p as bigint[])) for update"),
        {"o": amazon_order_id, "p": pids},
    ).all()
    from app.amazon_listings import enqueue_product

    for r in rows:
        db.execute(text("delete from stock_reservations where amazon_order_line_id = :l"), {"l": r.id})
        db.execute(text("update amazon_order_lines set state = 'dropship' where id = :l"), {"l": r.id})
    for pid in {r.product_id for r in rows if r.product_id}:
        enqueue_product(db, pid)
    db.commit()
    return len(rows)


def undo_lines_dropship(db: Session, amazon_order_id: str, product_ids=None) -> int:
    """These lines are NOT drop shipped after all (PO cancelled, the distributor cannot fill them, a person chose our own stock): put
    them back to Set aside so the order is picked normally. product_ids=None means every drop ship line of the order. An order that is
    cancelled or already shipped is left alone."""
    pids = sorted({int(p) for p in (product_ids or []) if p is not None})
    rows = db.execute(
        text(
            "select l.id, l.product_id, l.quantity * coalesce(l.pieces_per_sale, 1) as quantity from amazon_order_lines l "
            "join amazon_orders o on o.id = l.order_pk where l.amazon_order_id = :o and l.state = 'dropship' and l.product_id is not null "
            "and (cardinality(cast(:p as bigint[])) = 0 or l.product_id = any(cast(:p as bigint[]))) "
            "and not o.cancelled and o.shipped_at is null for update of l"
        ),
        {"o": amazon_order_id, "p": pids},
    ).all()
    from app.amazon_listings import enqueue_product

    for r in rows:
        lock_products(db, [r.product_id])
        db.execute(
            text(
                "insert into stock_reservations (order_id, product_id, quantity, status, expires_at, amazon_order_line_id) "
                "values (null, :p, :q, 'reserved', null, :l) on conflict (amazon_order_line_id) where amazon_order_line_id is not null do nothing"
            ),
            {"p": r.product_id, "q": r.quantity, "l": r.id},
        )
        db.execute(text("update amazon_order_lines set state = 'reserved' where id = :l"), {"l": r.id})
    for pid in {r.product_id for r in rows if r.product_id}:
        enqueue_product(db, pid)
    db.commit()
    return len(rows)


@ship_router.get("/lines")
@needs("inventory.ship")
def ship_lines(q: str = "", offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    """Inventory > Pick & ship: only Amazon lines whose parts are still set aside (not picked, released or cancelled)."""
    return lines(account="", state="reserved", q=q, date_from="", date_to="", offset=offset, limit=limit, db=db)


@ship_router.get("/to-pick-count")
@needs("inventory.ship")
def to_pick_count(db: Session = Depends(get_db)):
    n = db.execute(text("select count(distinct amazon_order_id) from amazon_order_lines where state = 'reserved'")).scalar()
    return {"orders": int(n or 0)}


def is_amazon_order(db: Session, reference: str) -> bool:
    ref = (reference or "").strip()
    return bool(ref) and db.execute(text("select 1 from amazon_orders where lower(amazon_order_id) = lower(:r) limit 1"), {"r": ref}).first() is not None


def take_stock_for_reference(db: Session, reference: str, merged: dict, email: str) -> list[int]:
    """The parts in this scan-out leave the shelf for the Amazon order with this number (same rules as the eBay version). Parts not on
    a set-aside line of that order are ignored. Returns the line ids touched. Does not commit."""
    from app.ebay_sales import lower_hold_marks

    touched: list[int] = []
    for pid, m in merged.items():
        left = int(m["qty"])
        for ln in db.execute(
            text("select id, amazon_order_id from amazon_order_lines where lower(amazon_order_id) = lower(:r) and product_id = :p and state = 'reserved' order by id"),
            {"r": reference.strip(), "p": pid},
        ).all():
            if left <= 0:
                break
            lock_products(db, [pid])
            res = db.execute(text("select id, quantity from stock_reservations where amazon_order_line_id = :l and status = 'reserved' for update"), {"l": ln.id}).first()
            if not res:
                continue
            take = min(left, int(res.quantity))
            before = db.execute(text("select coalesce(quantity_on_hand, 0) from products where id = :p"), {"p": pid}).scalar()
            after = db.execute(
                text("update products set quantity_on_hand = greatest(0, coalesce(quantity_on_hand, 0) - :t), updated_at = now() where id = :p returning quantity_on_hand"),
                {"t": take, "p": pid},
            ).scalar()
            if after != before:
                lower_hold_marks(db, pid)
                db.execute(
                    text("insert into inventory_transactions (product_id, transaction_type, quantity_change, reference, legacy_created_by) values (:p, 'Adjustment', :d, :r, :by)"),
                    {"p": pid, "d": after - before, "r": f"Picked for Amazon order {ln.amazon_order_id}", "by": email},
                )
            take_kit_components(db, pid, take, ln.amazon_order_id, email)  # a kit's parts come off the single-part products
            if int(res.quantity) - take > 0:
                db.execute(text("update stock_reservations set quantity = quantity - :t where id = :i"), {"t": take, "i": res.id})
            else:
                db.execute(text("delete from stock_reservations where id = :i"), {"i": res.id})
                db.execute(text("update amazon_order_lines set state = 'picked' where id = :l"), {"l": ln.id})
            left -= take
            touched.append(ln.id)
    return touched


_last_scan = 0.0


def _drop_ship_scan() -> None:
    """At most every 2 minutes (the loop wakes every 30 seconds): make drop ship POs for new Amazon orders and move waiting ones on."""
    global _last_scan
    if time.time() - _last_scan < 120:
        return
    _last_scan = time.time()
    from app.dropship_auto import scan_amazon_if_enabled

    scan_amazon_if_enabled()


def _drain_queues() -> None:
    from app.amazon_listings import run_queue

    db = _session_factory()()
    try:
        for r in db.execute(text("select distinct l.account from amazon_qty_queue q join amazon_listings l on l.id = q.listing_id where q.status = 'pending'")).all():
            try:
                run_queue(db, r.account)
            except Exception:
                db.rollback()
                log.exception("sending queued Amazon quantities for %s", r.account)
        from app.amazon_buybox import run_price_queue

        try:
            run_price_queue(db)  # queued Buy Box prices
        except Exception:
            db.rollback()
            log.exception("sending queued Amazon prices")
    finally:
        db.close()


def run_due() -> int:
    db = _session_factory()()
    n = 0
    try:
        accts = db.execute(
            text(
                "select account from amazon_accounts where sales_polling_enabled and refresh_token_enc is not null and "
                "(sales_last_polled_at is null or sales_last_polled_at + make_interval(mins => sales_poll_minutes) <= now())"
            )
        ).all()
        for a in accts:
            try:
                poll_account(db, a.account)
                n += 1
                try:   # keep the Amazon analytics rollup fresh (last 7 days; the rebuild is idempotent)
                    from app import amazon_analytics

                    amazon_analytics.rebuild(db, a.account, days=7)
                except Exception:
                    db.rollback()
                    log.exception("Amazon analytics rebuild after the poll failed")
            except HTTPException:
                pass   # the reason is saved on the account (sales_last_error)
    finally:
        db.close()
    return n


def _finances_due() -> None:
    from app import amazon_finances

    amazon_finances.run_due()   # fees and refunds, about once an hour per account


# ---- buyer data cleanup ----------------------------------------------------------------------------
# Amazon's Data Protection Policy: keep buyer PII (name, address, phone) only as long as needed to fulfill the order.
# The full ship-to exists in two places: the drop ship PO (po_headers.ship_to) and the job's buyer_name (dropship_jobs).
# Both are cleared AMAZON_PII_RETAIN_DAYS after the order shipped or was cancelled (default 14, never more than 28), and for any
# order older than 30 days even if it never shipped. amazon_orders.raw_sample (a debugging copy of the API response) goes too.
# amazon_orders keeps only state / ZIP / country, which the sales analytics need.
PII_REMOVED = "[Amazon buyer address removed after the retention period]"
_last_purge = 0.0


def _pii_retain_days() -> int:
    import os

    try:
        return max(1, min(28, int(os.environ.get("AMAZON_PII_RETAIN_DAYS", "14"))))
    except ValueError:
        return 14


def purge_buyer_data(db: Session) -> dict:
    """Clear expired Amazon buyer data. Safe to repeat. Returns how many rows each step changed."""
    expired = (
        "(o.shipped_at < now() - make_interval(days => :d) "
        "or (o.cancelled and o.updated_at < now() - make_interval(days => :d)) "
        "or coalesce(o.created_at_amazon, o.updated_at) < now() - interval '30 days')"
    )
    p = {"d": _pii_retain_days(), "ph": PII_REMOVED}
    out = {}
    steps = (
        ("po_ship_to", f"update po_headers h set ship_to = :ph from amazon_orders o where h.source = 'amazon:' || o.amazon_order_id and h.ship_to is not null and h.ship_to <> :ph and {expired}"),
        ("job_buyer_name", f"update dropship_jobs j set buyer_name = null from amazon_orders o where j.channel = 'amazon' and j.ebay_order_id = 'amazon:' || o.amazon_order_id and j.buyer_name is not null and {expired}"),
        ("raw_sample", f"update amazon_orders o set raw_sample = null where o.raw_sample is not null and {expired}"),
    )
    for name, sql in steps:
        try:
            out[name] = db.execute(text(sql), p).rowcount or 0
            db.commit()
        except Exception:   # a table or column that does not exist yet must not stop the other steps
            db.rollback()
            out[name] = -1
            log.exception("Amazon buyer data cleanup step %s failed", name)
    if any(v > 0 for v in out.values()):
        log.info("Amazon buyer data cleanup: %s", out)
    return out


def _purge_due() -> None:
    global _last_purge
    if time.time() - _last_purge < 3600:
        return
    _last_purge = time.time()
    db = _session_factory()()
    try:
        purge_buyer_data(db)
    finally:
        db.close()


@router.post("/purge-buyer-data")
def purge_now(db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Run the buyer data cleanup now (it also runs about once an hour by itself)."""
    return {"ok": True, "retain_days": _pii_retain_days(), "cleared": purge_buyer_data(db)}


def _loop() -> None:
    time.sleep(40)
    while True:
        for fn in (run_due, _drop_ship_scan, _drain_queues, _finances_due, _purge_due):
            try:
                fn()
            except Exception:
                log.exception("Amazon background step %s", fn.__name__)
        time.sleep(30)


_started = False


def start_background() -> None:
    """Called once at startup. Set AMAZON_SALES_THREAD=0 to turn it off."""
    import os

    global _started
    if _started or os.environ.get("AMAZON_SALES_THREAD", "1") == "0":
        return
    _started = True
    threading.Thread(target=_loop, name="amazon-sales", daemon=True).start()
