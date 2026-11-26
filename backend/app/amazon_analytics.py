"""
Amazon analytics feed (Analytics > Amazon tab), parallel to the eBay analytics tables. Mounted at /admin/amazon-analytics/*.

Reads amazon_orders / amazon_order_lines (written by amazon_sales.py) and keeps a daily rollup in amazon_analytics_daily.
Read-only toward Amazon: nothing here calls Amazon.

  POST /rebuild              {account?, days?}  recompute the daily rollup for the last N days (default 90)
  GET  /summary              ?account=&days=    totals: orders, units, revenue, fees, estimated margin
  GET  /daily                ?account=&days=    one row per day
  GET  /top-products         ?account=&days=&limit=   best sellers by revenue (linked products only show a product id)
  GET  /overview             ?account=&start=&end=    everything the Amazon tab of the Analytics page needs (dates, not "days"); refreshes the
                                                      rollup first when it is empty or more than 30 minutes old

rebuild(db) can also be called from the background timer after a poll. Cancelled orders are excluded.
Margin is an estimate: revenue - refunds (item price part, from amazon_finances) - fees (when Amazon gave them) - product cost x units x pieces per sale. Lines whose product has no
cost are counted in revenue but flagged in `lines_without_cost`.
"""
import logging
from datetime import date
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


# ---- Analytics page (Amazon tab): one call with everything the tab shows, for a date range like the eBay and QuickBooks tabs.

def _refresh_if_stale(db: Session) -> None:
    """Keep the rollup fresh without a timer: empty -> build two years, older than 30 minutes -> redo the last 90 days."""
    try:
        r = db.execute(text(
            "select count(*) n, coalesce(extract(epoch from (now() - max(refreshed_at))), 1e9) age from amazon_analytics_daily"
        )).first()
        if r.n == 0:
            rebuild(db, None, 730)
        elif float(r.age) > 1800:
            rebuild(db, None, 90)
    except Exception:
        db.rollback()
        log.exception("could not refresh the Amazon analytics rollup")


def _range(account: str, start: Optional[date], end: Optional[date]):
    where, p = ["true"], {}
    if account.strip():
        where.append("account = :a")
        p["a"] = account.strip()
    if start:
        where.append("day >= :s")
        p["s"] = start
    if end:
        where.append("day <= :e")
        p["e"] = end
    return " and ".join(where), p


def _num(row: dict, keys: tuple) -> dict:
    return {k: (float(v) if k in keys and v is not None else v) for k, v in row.items()}


@router.get("/overview")
def overview(account: str = "", start: Optional[date] = None, end: Optional[date] = None, db: Session = Depends(get_db)):
    _refresh_if_stale(db)
    where, p = _range(account, start, end)
    money = ("revenue", "fees", "cost", "refunds")
    k = db.execute(text(
        f"select coalesce(sum(orders),0) orders, coalesce(sum(units),0) units, coalesce(sum(revenue),0) revenue, coalesce(sum(refunds),0) refunds, "
        f"coalesce(sum(fees),0) fees, coalesce(sum(cost),0) cost, coalesce(sum(lines_without_cost),0) lines_without_cost, "
        f"coalesce(sum(dropship_lines),0) dropship_lines, min(day) first_date, max(day) last_date, max(refreshed_at) refreshed_at "
        f"from amazon_analytics_daily where {where}"), p).first()
    kpi = _num(dict(k._mapping), money)
    kpi["orders"], kpi["units"] = int(kpi["orders"]), int(kpi["units"])
    kpi["lines_without_cost"], kpi["dropship_lines"] = int(kpi["lines_without_cost"]), int(kpi["dropship_lines"])
    kpi["estimated_margin"] = round(kpi["revenue"] - kpi["refunds"] - kpi["fees"] - kpi["cost"], 2)
    kpi["first_date"] = kpi["first_date"].isoformat() if kpi["first_date"] else None
    kpi["last_date"] = kpi["last_date"].isoformat() if kpi["last_date"] else None
    refreshed_at = kpi.pop("refreshed_at")

    by_account = [
        {**_num(dict(r._mapping), money), "orders": int(r.orders), "units": int(r.units)}
        for r in db.execute(text(
            f"select account, sum(orders) orders, sum(units) units, sum(revenue) revenue, sum(refunds) refunds, sum(fees) fees, sum(cost) cost "
            f"from amazon_analytics_daily where {where} group by account order by revenue desc"), p).all()
    ]
    monthly = [
        {**_num(dict(r._mapping), money), "orders": int(r.orders), "units": int(r.units)}
        for r in db.execute(text(
            f"select to_char(date_trunc('month', day), 'YYYY-MM') as month, account, sum(orders) orders, sum(units) units, sum(revenue) revenue, "
            f"sum(refunds) refunds, sum(fees) fees, sum(cost) cost from amazon_analytics_daily where {where} group by 1, 2 order by 1, 2"), p).all()
    ]

    # Best sellers come from the order lines (same filters, by sale date).
    lw, lp = ["not o.cancelled"], {}
    if account.strip():
        lw.append("o.account = :a")
        lp["a"] = account.strip()
    if start:
        lw.append("(coalesce(l.sale_date, o.created_at_amazon))::date >= :s")
        lp["s"] = start
    if end:
        lw.append("(coalesce(l.sale_date, o.created_at_amazon))::date <= :e")
        lp["e"] = end

    def top(order: str):
        rows = db.execute(text(
            f"""select l.product_id, coalesce(max(pr.title), max(l.title)) as title, max(l.sku) as sku, sum(l.quantity) units,
                       sum(l.quantity * coalesce(l.unit_price, 0)) revenue, count(distinct o.id) orders
                  from amazon_order_lines l join amazon_orders o on o.id = l.order_pk left join products pr on pr.id = l.product_id
                 where {" and ".join(lw)}
                 group by l.product_id, coalesce(l.product_id::text, l.sku)
                 order by {order} desc limit 10"""), lp).all()
        return [{**dict(r._mapping), "revenue": float(r.revenue), "units": int(r.units), "orders": int(r.orders)} for r in rows]

    return {
        "kpi": kpi,
        "by_account": by_account,
        "monthly": monthly,
        "top_revenue": top("revenue"),
        "top_units": top("units"),
        "refreshed_at": refreshed_at.isoformat() if refreshed_at else None,
    }
