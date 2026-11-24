"""
Amazon analytics feed (Analytics > Amazon tab), parallel to the eBay analytics tables. Mounted at /admin/amazon-analytics/*.

Reads amazon_orders / amazon_order_lines (written by amazon_sales.py) and keeps a daily rollup in amazon_analytics_daily.
Read-only toward Amazon: nothing here calls Amazon.

  POST /rebuild              {account?, days?}  recompute the daily rollup for the last N days (default 90)
  GET  /summary              ?account=&days=    totals: orders, units, revenue, fees, estimated margin
  GET  /daily                ?account=&days=    one row per day
  GET  /top-products         ?account=&days=&limit=   best sellers by revenue (linked products only show a product id)

rebuild(db) can also be called from the background timer after a poll. Cancelled orders are excluded.
Margin is an estimate: revenue - refunds (item price part, from amazon_finances) - fees (when Amazon gave them) - product cost x units x pieces per sale. Lines whose product has no
cost are counted in revenue but flagged in `lines_without_cost`.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/amazon-analytics", tags=["admin-amazon-analytics"], dependencies=[Depends(require_admin)])

_COST = "nullif(to_jsonb(p)->>'cost', '')::numeric"


def ensure_tables() -> None:
    db = _session_factory()()
    try:
        db.execute(text(
            """create table if not exists amazon_analytics_daily (
                 account text not null,
                 marketplace text not null default '',
                 day date not null,
                 orders integer not null default 0,
                 units integer not null default 0,
                 revenue numeric not null default 0,
                 fees numeric not null default 0,
                 cost numeric not null default 0,
                 lines_without_cost integer not null default 0,
                 dropship_lines integer not null default 0,
                 refunds numeric not null default 0,
                 refreshed_at timestamptz not null default now(),
                 primary key (account, marketplace, day)
               )"""
        ))
        db.execute(text("alter table amazon_analytics_daily add column if not exists refunds numeric not null default 0"))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not set up the Amazon analytics table")
    finally:
        db.close()


def rebuild(db: Session, account: Optional[str] = None, days: int = 90) -> int:
    """Recompute the last `days` days. Returns the number of day rows written."""
    days = max(1, min(int(days), 730))
    p = {"d": days, "a": (account or "").strip() or None}
    db.execute(text("delete from amazon_analytics_daily where day >= (current_date - :d * interval '1 day')::date and (cast(:a as text) is null or account = :a)"), p)
    res = db.execute(text(
        f"""insert into amazon_analytics_daily (account, marketplace, day, orders, units, revenue, fees, cost, lines_without_cost, dropship_lines, refunds)
            select o.account, coalesce(o.marketplace, ''), (coalesce(l.sale_date, o.created_at_amazon))::date as day,
                   count(distinct o.id),
                   coalesce(sum(l.quantity), 0),
                   coalesce(sum(l.quantity * coalesce(l.unit_price, 0)), 0),
                   coalesce(sum(coalesce(l.fees, 0)), 0),
                   coalesce(sum(case when {_COST} is not null then {_COST} * l.quantity * coalesce(l.pieces_per_sale, 1) else 0 end), 0),
                   count(*) filter (where l.product_id is null or {_COST} is null),
                   count(*) filter (where l.state = 'dropship'),
                   coalesce(sum(coalesce(l.refund_total, 0)), 0)
              from amazon_orders o
              join amazon_order_lines l on l.order_pk = o.id
              left join products p on p.id = l.product_id
             where not o.cancelled
               and (coalesce(l.sale_date, o.created_at_amazon))::date >= (current_date - :d * interval '1 day')::date
               and (cast(:a as text) is null or o.account = :a)
             group by o.account, coalesce(o.marketplace, ''), (coalesce(l.sale_date, o.created_at_amazon))::date"""
    ), p)
    db.commit()
    return res.rowcount or 0


class RebuildIn(BaseModel):
    account: Optional[str] = Field(None, max_length=60)
    days: int = Field(90, ge=1, le=730)


@router.post("/rebuild")
def rebuild_route(body: RebuildIn, db: Session = Depends(get_db)):
    return {"ok": True, "day_rows": rebuild(db, body.account, body.days)}


def _where(account: str):
    return ("day >= (current_date - :d * interval '1 day')::date" + (" and account = :a" if account.strip() else ""))


@router.get("/summary")
def summary(account: str = "", days: int = Query(30, ge=1, le=730), db: Session = Depends(get_db)):
    r = db.execute(text(
        f"select coalesce(sum(orders),0) orders, coalesce(sum(units),0) units, coalesce(sum(revenue),0) revenue, coalesce(sum(fees),0) fees, "
        f"coalesce(sum(cost),0) cost, coalesce(sum(lines_without_cost),0) lines_without_cost, coalesce(sum(dropship_lines),0) dropship_lines, coalesce(sum(refunds),0) refunds "
        f"from amazon_analytics_daily where {_where(account)}"), {"d": days, "a": account.strip()}).first()
    out = {k: float(v) if k in ("revenue", "fees", "cost", "refunds") else int(v) for k, v in r._mapping.items()}
    out["estimated_margin"] = round(out["revenue"] - out["refunds"] - out["fees"] - out["cost"], 2)
    out["days"] = days
    return out


@router.get("/daily")
def daily(account: str = "", days: int = Query(30, ge=1, le=730), db: Session = Depends(get_db)):
    rows = db.execute(text(
        f"select day, sum(orders) orders, sum(units) units, sum(revenue) revenue, sum(fees) fees, sum(cost) cost, sum(refunds) refunds from amazon_analytics_daily "
        f"where {_where(account)} group by day order by day"), {"d": days, "a": account.strip()}).all()
    return {"days": [{**dict(r._mapping), "revenue": float(r.revenue), "fees": float(r.fees), "cost": float(r.cost), "refunds": float(r.refunds)} for r in rows]}


@router.get("/top-products")
def top_products(account: str = "", days: int = Query(30, ge=1, le=730), limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    p = {"d": days, "lim": limit, "a": account.strip()}
    rows = db.execute(text(
        f"""select l.product_id, coalesce(max(pr.title), max(l.title)) as title, max(l.sku) as sku, sum(l.quantity) units,
                   sum(l.quantity * coalesce(l.unit_price, 0)) revenue
              from amazon_order_lines l join amazon_orders o on o.id = l.order_pk left join products pr on pr.id = l.product_id
             where not o.cancelled and (coalesce(l.sale_date, o.created_at_amazon))::date >= (current_date - :d * interval '1 day')::date
               {"and o.account = :a" if account.strip() else ""}
             group by l.product_id, coalesce(l.product_id::text, l.sku)
             order by revenue desc limit :lim"""), p).all()
    return {"products": [{**dict(r._mapping), "revenue": float(r.revenue), "units": int(r.units)} for r in rows]}
