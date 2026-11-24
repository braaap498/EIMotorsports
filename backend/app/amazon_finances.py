"""
Amazon fees and refunds, read from the Finances API (read-only toward Amazon). Mounted at /admin/amazon-finances/*.

  POST /poll-now     {account, days?}     read financial events now (default: since the saved cursor, first run 30 days)
  GET  /refunds      ?account=&days=      refunds Amazon reported, newest first

What it does with what Amazon reports:
  * FEES: each shipped item's fee list is added up and saved on the order line (amazon_order_lines.fees). The same figure is written to
    sales_transactions.platform_fees when that line has a sales row. The Amazon analytics rollup already subtracts line fees.
  * REFUNDS: each refund event is saved once in amazon_refunds (repeat-safe), and the totals are put on the line (refund_total) and the
    order (refund_total). A refund does NOT put stock back and does NOT change the order's state: staff decide about the part itself.

UNVERIFIED against Amazon (the field names are from the Finances v0 listFinancialEvents shape; compare with a real response):
  ShipmentEventList / RefundEventList, ShipmentItemList, ItemFeeList, ShipmentItemAdjustmentList, ItemChargeAdjustmentList,
  ChargeAmount.CurrencyAmount. The app needs the Finance and Accounting role granted for the Finances API to answer.
Run from the background loop about once an hour for accounts with sales polling ON; nothing here ever writes to Amazon.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.amazon_core import _row, sp_get
from app.auth import require_admin
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/amazon-finances", tags=["admin-amazon-finances"], dependencies=[Depends(require_admin)])

FIRST_LOOKBACK_DAYS = 30
OVERLAP = timedelta(days=1)       # events can post late, so each run re-reads the last day (writes are repeat-safe)
MAX_PAGES = 10                    # per run; the rest is picked up next run
EVERY_MINUTES = 60
BEFORE_LAG = timedelta(minutes=5)  # Amazon refuses an end time that is too close to now


def ensure_tables() -> None:
    db = _session_factory()()
    try:
        for stmt in (
            """create table if not exists amazon_refunds (
                 id bigserial primary key,
                 account text not null,
                 amazon_order_id text not null,
                 sku text not null default '',
                 posted_at timestamptz,
                 amount numeric not null default 0,
                 principal numeric not null default 0,
                 created_at timestamptz not null default now()
               )""",
            "create unique index if not exists amazon_refunds_once on amazon_refunds (account, amazon_order_id, sku, posted_at, amount)",
            "alter table amazon_order_lines add column if not exists refund_total numeric not null default 0",
            "alter table amazon_orders add column if not exists refund_total numeric not null default 0",
            "alter table amazon_accounts add column if not exists finances_cursor timestamptz",
            "alter table amazon_accounts add column if not exists finances_last_polled_at timestamptz",
            "alter table amazon_accounts add column if not exists finances_last_error text",
        ):
            db.execute(text(stmt))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not set up the Amazon finances tables")
    finally:
        db.close()


def _amt(v) -> Decimal:
    """CurrencyAmount from Amazon: a number or a numeric string. Anything else counts as 0."""
    try:
        return Decimal(str((v or {}).get("CurrencyAmount") if isinstance(v, dict) else v))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _when(v) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _events(payload: dict) -> dict:
    return ((payload or {}).get("payload") or payload or {}).get("FinancialEvents") or {}


def _save_fees(db: Session, account: str, ev: dict) -> int:
    """One shipment event: total the fees per item and save them on the matching line. Returns lines updated."""
    order_id = ev.get("AmazonOrderId")
    if not order_id:
        return 0
    n = 0
    for it in ev.get("ShipmentItemList") or []:
        fee = sum((-_amt(f.get("FeeAmount")) for f in (it.get("ItemFeeList") or [])), Decimal(0))   # Amazon reports fees as negatives
        if fee < 0:
            fee = Decimal(0)
        key, sku = it.get("OrderItemId"), it.get("SellerSKU")
        row = None
        if key:
            row = db.execute(text("update amazon_order_lines set fees = :f where account = :a and amazon_order_id = :o and line_key = :k returning sales_transaction_id"),
                             {"f": fee, "a": account, "o": order_id, "k": str(key)}).first()
        if row is None and sku:
            row = db.execute(text("update amazon_order_lines set fees = :f where account = :a and amazon_order_id = :o and sku = :s returning sales_transaction_id"),
                             {"f": fee, "a": account, "o": order_id, "s": sku}).first()
        if row is not None:
            n += 1
            if row.sales_transaction_id:
                db.execute(text("update sales_transactions set platform_fees = :f where id = :s"), {"f": fee, "s": row.sales_transaction_id})
    return n


def _save_refunds(db: Session, account: str, ev: dict) -> set:
    """One refund event: save each refunded item once. Returns the order ids touched."""
    order_id = ev.get("AmazonOrderId")
    if not order_id:
        return set()
    posted = _when(ev.get("PostedDate"))
    for it in ev.get("ShipmentItemAdjustmentList") or []:
        total = principal = Decimal(0)
        for c in it.get("ItemChargeAdjustmentList") or []:
            a = -_amt(c.get("ChargeAmount"))          # a refund is reported as a negative charge
            total += a
            if str(c.get("ChargeType", "")).lower() == "principal":
                principal += a
        if total == 0:
            continue
        db.execute(
            text("insert into amazon_refunds (account, amazon_order_id, sku, posted_at, amount, principal) values (:a, :o, :s, :p, :t, :pr) on conflict do nothing"),
            {"a": account, "o": order_id, "s": it.get("SellerSKU") or "", "p": posted, "t": total, "pr": principal},
        )
    return {order_id}


def _totals(db: Session, account: str, order_ids: set) -> None:
    for o in order_ids:
        db.execute(
            text("update amazon_orders set refund_total = coalesce((select sum(amount) from amazon_refunds r where r.account = :a and r.amazon_order_id = :o), 0) "
                 "where account = :a and amazon_order_id = :o"), {"a": account, "o": o})
        db.execute(
            text("update amazon_order_lines l set refund_total = coalesce((select sum(principal) from amazon_refunds r where r.account = l.account "
                 "and r.amazon_order_id = l.amazon_order_id and r.sku = coalesce(l.sku, '')), 0) where l.account = :a and l.amazon_order_id = :o"), {"a": account, "o": o})


def poll_account(db: Session, account: str, days: Optional[int] = None) -> dict:
    a = _row(db, account)
    if not a.refresh_token_enc:
        raise HTTPException(409, "Save this account's Amazon key first.")
    now = datetime.now(timezone.utc)
    if days:
        after = now - timedelta(days=min(int(days), 180))
    elif a.finances_cursor:
        after = a.finances_cursor - OVERLAP
    else:
        after = now - timedelta(days=FIRST_LOOKBACK_DAYS)
    before = now - BEFORE_LAG
    fees = refunds = pages = 0
    touched: set = set()
    token = None
    try:
        while pages < MAX_PAGES:
            params = {"NextToken": token} if token else {"PostedAfter": after.isoformat(), "PostedBefore": before.isoformat(), "MaxResultsPerPage": 100}
            data = sp_get(db, a.account, "/finances/v0/financialEvents", params)
            pages += 1
            fe = _events(data)
            for ev in fe.get("ShipmentEventList") or []:
                fees += _save_fees(db, a.account, ev)
            for ev in fe.get("RefundEventList") or []:
                touched |= _save_refunds(db, a.account, ev)
                refunds += 1
            db.commit()
            token = ((data.get("payload") or data) or {}).get("NextToken")
            if not token:
                break
            time.sleep(2)   # the Finances API allows about one call every 2 seconds
        _totals(db, a.account, touched)
        complete = token is None
        db.execute(
            text("update amazon_accounts set finances_last_polled_at = now(), finances_last_error = null, finances_cursor = case when :c then :b else finances_cursor end where account = :a"),
            {"a": a.account, "c": complete, "b": before})
        db.commit()
    except HTTPException as e:
        db.rollback()
        db.execute(text("update amazon_accounts set finances_last_polled_at = now(), finances_last_error = :e where account = :a"), {"e": str(e.detail)[:300], "a": a.account})
        db.commit()
        raise
    return {"ok": True, "pages": pages, "lines_with_fees": fees, "refund_events": refunds, "more_waiting": token is not None}


class PollIn(BaseModel):
    account: str
    days: Optional[int] = Field(None, ge=1, le=180)


@router.post("/poll-now")
def poll_now(body: PollIn, db: Session = Depends(get_db)):
    return poll_account(db, body.account, body.days)


@router.get("/refunds")
def refunds(account: str = "", days: int = Query(90, ge=1, le=730), db: Session = Depends(get_db)):
    rows = db.execute(
        text("select id, account, amazon_order_id, sku, posted_at, amount, principal from amazon_refunds "
             "where created_at >= now() - :d * interval '1 day' and (cast(:a as text) = '' or account = :a) order by coalesce(posted_at, created_at) desc limit 200"),
        {"d": days, "a": account.strip()}).all()
    return {"refunds": [{**dict(r._mapping), "amount": float(r.amount), "principal": float(r.principal)} for r in rows]}


def run_due() -> int:
    """From the background loop: accounts with sales polling ON, at most once an hour each."""
    db = _session_factory()()
    n = 0
    try:
        for a in db.execute(text(
            "select account from amazon_accounts where sales_polling_enabled and refresh_token_enc is not null and "
            "(finances_last_polled_at is null or finances_last_polled_at + make_interval(mins => :m) <= now())"), {"m": EVERY_MINUTES}).all():
            try:
                poll_account(db, a.account)
                n += 1
                try:
                    from app import amazon_analytics

                    amazon_analytics.rebuild(db, a.account, days=35)
                except Exception:
                    db.rollback()
                    log.exception("Amazon analytics rebuild after finances failed")
            except HTTPException:
                pass   # saved on the account as finances_last_error
            except Exception:
                db.rollback()
                log.exception("Amazon finances for %s", a.account)
    finally:
        db.close()
    return n
