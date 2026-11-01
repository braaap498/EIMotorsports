"""
Analytics: sales numbers crunched from the order history stored in Postgres. Owner / admin only.
Mounted at /admin/analytics/*.

  GET  /analytics/ebay/stores     every store that has data: orders, first and last sale date
  POST /analytics/ebay/import     add orders from an "All orders" CSV (the page reads the file and sends it in
                                  chunks). Adding the same order again UPDATES it, so overlapping files are fine.
  GET  /analytics/ebay/summary    everything the charts need, for one store or all stores, for a date range
                                  (?store=eandimotorsports&start=2025-01-01&end=2025-12-31; no store = all)

More sources (QuickBooks, ...) get their own tables and their own /analytics/<source>/ routes later.
"""
import logging
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db
from app.ebay_orders_parse import build_orders, clean_store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/analytics", tags=["admin-analytics"], dependencies=[Depends(require_admin)])

MAX_ROWS_PER_CALL = 3000

_ORDER_COLS = [
    "store", "order_number", "sales_record_number", "sale_date", "paid_on_date", "shipped_on_date",
    "buyer_username", "buyer_state", "buyer_country", "line_count", "units", "items_subtotal",
    "shipping_charged", "tax_collected", "total_price", "gross_sales", "net_sales", "refund_est",
    "shipping_service", "promoted", "global_shipping", "feedback_received", "days_to_ship", "source_file",
]
_UPSERT_ORDER = text(
    f"insert into analytics_ebay_orders ({', '.join(_ORDER_COLS)}, imported_at) "
    f"values ({', '.join(':' + c for c in _ORDER_COLS)}, now()) "
    "on conflict (store, order_number) do update set "
    + ", ".join(f"{c} = excluded.{c}" for c in _ORDER_COLS if c not in ("store", "order_number"))
    + ", imported_at = now()"
)
_INSERT_ITEM = text(
    "insert into analytics_ebay_order_items "
    "(store, order_number, line_no, item_number, item_title, custom_label, transaction_id, quantity, unit_price) "
    "values (:store, :order_number, :line_no, :item_number, :item_title, :custom_label, :transaction_id, :quantity, :unit_price)"
)


class ImportBody(BaseModel):
    store: str
    source_file: Optional[str] = Field(default=None, max_length=200)
    rows: list[dict[str, Any]] = Field(default_factory=list)


def _tables_ready(db: Session) -> bool:
    return bool(db.execute(text("select to_regclass('public.analytics_ebay_orders') is not null")).scalar())


def _need_tables(db: Session) -> None:
    if not _tables_ready(db):
        raise HTTPException(
            status_code=409,
            detail="The analytics tables don't exist yet. Run analytics_ebay_tables.sql in the Supabase SQL editor first.",
        )


@router.get("/ebay/stores")
def stores(db: Session = Depends(get_db)):
    if not _tables_ready(db):
        return {"tables_ready": False, "stores": []}
    rows = db.execute(
        text(
            "select store, count(*) as orders, min(sale_date) as first_date, max(sale_date) as last_date, "
            "max(imported_at) as last_import from analytics_ebay_orders group by store order by store"
        )
    ).mappings().all()
    return {"tables_ready": True, "stores": [dict(r) for r in rows]}


@router.post("/ebay/import")
def import_orders(body: ImportBody, db: Session = Depends(get_db)):
    _need_tables(db)
    try:
        store = clean_store(body.store)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if len(body.rows) > MAX_ROWS_PER_CALL:
        raise HTTPException(status_code=413, detail=f"Send at most {MAX_ROWS_PER_CALL} rows at a time.")

    recs, skipped = build_orders(store, body.rows, body.source_file)
    if not recs:
        return {"store": store, "received": 0, "added": 0, "updated": 0, "skipped": skipped}

    # The same order can appear twice in one chunk only if the file is odd; keep the last one.
    by_number = {r["order"]["order_number"]: r for r in recs}
    recs = list(by_number.values())
    numbers = list(by_number)
    try:
        existing = db.execute(
            text("select count(*) from analytics_ebay_orders where store = :s and order_number in :n").bindparams(
                bindparam("n", expanding=True)
            ),
            {"s": store, "n": numbers},
        ).scalar() or 0
        db.execute(_UPSERT_ORDER, [r["order"] for r in recs])
        db.execute(
            text("delete from analytics_ebay_order_items where store = :s and order_number in :n").bindparams(
                bindparam("n", expanding=True)
            ),
            {"s": store, "n": numbers},
        )
        items = [{"store": store, "order_number": r["order"]["order_number"], **it} for r in recs for it in r["items"]]
        if items:
            db.execute(_INSERT_ITEM, items)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("analytics import failed")
        raise HTTPException(status_code=500, detail="The import failed and nothing from this batch was saved.")
    return {
        "store": store,
        "received": len(recs),
        "added": len(recs) - int(existing),
        "updated": int(existing),
        "skipped": skipped,
    }


def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


def _rows(db: Session, sql: str, params: dict) -> list[dict]:
    return [{k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in r.items()} for r in db.execute(text(sql), params).mappings().all()]


@router.get("/ebay/summary")
def summary(
    store: Optional[str] = Query(default=None),
    start: Optional[date] = None,
    end: Optional[date] = None,
    db: Session = Depends(get_db),
):
    _need_tables(db)
    store = (store or "").strip().lower() or None
    if store in ("all", "__all__"):
        store = None
    p = {"store": store, "start": start, "end": end}
    # `o` = analytics_ebay_orders. Same filter for every query below.
    flt = (
        "(cast(:store as text) is null or o.store = cast(:store as text)) "
        "and (cast(:start as date) is null or o.sale_date >= cast(:start as date)) "
        "and (cast(:end as date) is null or o.sale_date <= cast(:end as date))"
    )

    kpi = _rows(
        db,
        f"""select count(*) as orders, coalesce(sum(units),0) as units, coalesce(sum(gross_sales),0) as gross_sales,
                   coalesce(sum(net_sales),0) as net_sales, coalesce(sum(refund_est),0) as refunds,
                   coalesce(sum(items_subtotal),0) as items, coalesce(sum(shipping_charged),0) as shipping,
                   coalesce(sum(tax_collected),0) as tax, coalesce(sum(total_price),0) as buyer_paid,
                   count(distinct buyer_username) as buyers,
                   count(*) filter (where refund_est > 0) as refunded_orders,
                   count(*) filter (where total_price = 0) as cancelled_orders,
                   count(*) filter (where buyer_country is not null and buyer_country <> 'United States') as intl_orders,
                   avg(days_to_ship) as avg_days_to_ship,
                   count(*) filter (where days_to_ship <= 1) as shipped_fast, count(days_to_ship) as shipped_known,
                   min(sale_date) as first_date, max(sale_date) as last_date
            from analytics_ebay_orders o where {flt}""",
        p,
    )[0]

    by_store = _rows(
        db,
        f"""select store, count(*) as orders, coalesce(sum(units),0) as units, coalesce(sum(gross_sales),0) as gross_sales,
                   coalesce(sum(net_sales),0) as net_sales, coalesce(sum(refund_est),0) as refunds,
                   coalesce(sum(shipping_charged),0) as shipping, count(distinct buyer_username) as buyers
            from analytics_ebay_orders o where {flt} group by store order by net_sales desc""",
        p,
    )

    monthly = _rows(
        db,
        f"""select to_char(date_trunc('month', sale_date), 'YYYY-MM') as month, store, count(*) as orders,
                   coalesce(sum(units),0) as units, coalesce(sum(gross_sales),0) as gross_sales,
                   coalesce(sum(net_sales),0) as net_sales, coalesce(sum(refund_est),0) as refunds,
                   coalesce(sum(shipping_charged),0) as shipping
            from analytics_ebay_orders o where {flt} group by 1, 2 order by 1, 2""",
        p,
    )

    weekday = _rows(
        db,
        f"""select extract(dow from sale_date)::int as dow, count(*) as orders, coalesce(sum(net_sales),0) as net_sales
            from analytics_ebay_orders o where {flt} group by 1 order by 1""",
        p,
    )

    states = _rows(
        db,
        f"""select upper(buyer_state) as state, count(*) as orders, coalesce(sum(net_sales),0) as net_sales
            from analytics_ebay_orders o
            where {flt} and buyer_country = 'United States' and buyer_state is not null
            group by 1 order by net_sales desc""",
        p,
    )

    countries = _rows(
        db,
        f"""select coalesce(buyer_country, 'Unknown') as country, count(*) as orders, coalesce(sum(net_sales),0) as net_sales
            from analytics_ebay_orders o where {flt} group by 1 order by orders desc limit 10""",
        p,
    )

    shipping = _rows(
        db,
        f"""select coalesce(shipping_service, 'Unknown') as service, count(*) as orders,
                   coalesce(sum(shipping_charged),0) as shipping_charged, coalesce(sum(net_sales),0) as net_sales
            from analytics_ebay_orders o where {flt} group by 1 order by orders desc limit 8""",
        p,
    )

    promoted = _rows(
        db,
        f"""select case when promoted then 'Promoted Listings' when promoted is false then 'Organic' else 'Unknown' end as source,
                   count(*) as orders, coalesce(sum(net_sales),0) as net_sales
            from analytics_ebay_orders o where {flt} group by 1 order by orders desc""",
        p,
    )

    order_sizes = _rows(
        db,
        f"""select bucket, min(sort) as sort, count(*) as orders from (
              select case when gross_sales < 25 then 'Under $25' when gross_sales < 50 then '$25-50'
                          when gross_sales < 100 then '$50-100' when gross_sales < 200 then '$100-200'
                          when gross_sales < 500 then '$200-500' else '$500+' end as bucket,
                     case when gross_sales < 25 then 1 when gross_sales < 50 then 2 when gross_sales < 100 then 3
                          when gross_sales < 200 then 4 when gross_sales < 500 then 5 else 6 end as sort
              from analytics_ebay_orders o where {flt}) t group by bucket order by sort""",
        p,
    )

    repeat = _rows(
        db,
        f"""select case when n = 1 then '1 order' when n = 2 then '2 orders' when n <= 4 then '3-4 orders' else '5+ orders' end as band,
                   min(case when n = 1 then 1 when n = 2 then 2 when n <= 4 then 3 else 4 end) as sort,
                   count(*) as buyers, sum(net) as net_sales
            from (select buyer_username, count(*) as n, sum(net_sales) as net
                  from analytics_ebay_orders o where {flt} and buyer_username is not null group by 1) t
            group by 1 order by sort""",
        p,
    )

    feedback = _rows(
        db,
        f"""select coalesce(feedback_received, 'none') as feedback, count(*) as orders
            from analytics_ebay_orders o where {flt} group by 1 order by orders desc""",
        p,
    )

    item_sql = f"""select i.item_number, max(i.item_title) as title, max(i.custom_label) as custom_label,
                          sum(i.quantity) as units, sum(i.quantity * i.unit_price) as revenue,
                          count(distinct i.store || i.order_number) as orders, string_agg(distinct i.store, ', ') as stores
                   from analytics_ebay_order_items i
                   join analytics_ebay_orders o on o.store = i.store and o.order_number = i.order_number
                   where {flt} and i.item_number is not null
                   group by i.item_number order by %s desc limit 12"""
    top_revenue = _rows(db, item_sql % "revenue", p)
    top_units = _rows(db, item_sql % "units", p)

    return {
        "store": store,
        "start": start,
        "end": end,
        "kpi": kpi,
        "by_store": by_store,
        "monthly": monthly,
        "weekday": weekday,
        "states": states,
        "countries": countries,
        "shipping": shipping,
        "promoted": promoted,
        "order_sizes": order_sizes,
        "repeat": repeat,
        "feedback": feedback,
        "top_revenue": top_revenue,
        "top_units": top_units,
    }
