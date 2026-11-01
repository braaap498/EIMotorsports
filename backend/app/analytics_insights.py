"""
eBay insights: seasonality, product mix, buyer behaviour and a diagnosis of odd months. Owner / admin only.
Mounted at /admin/analytics/ebay/insights.

  GET /analytics/ebay/insights?store=eandimotorsports&start=2025-01-01&end=2025-12-31   (no store = all stores)

Reads analytics_ebay_orders and analytics_ebay_order_items (nothing is written). The number crunching is in
insights_calc.py. Cancelled / fully refunded orders (Total Price of $0) are left out of everything here, since
they are not sales. Buyer usernames are only used to count and to list the heaviest buyers (admin only).
"""
import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db
from app.insights_calc import build_insights

log = logging.getLogger(__name__)
router = APIRouter(prefix="/analytics/ebay", tags=["admin-analytics-insights"], dependencies=[Depends(require_admin)])

MAX_LINES = 400_000  # a safety cap; two years of both stores is well under this


@router.get("/insights")
def insights(
    store: Optional[str] = Query(default=None),
    start: Optional[date] = None,
    end: Optional[date] = None,
    db: Session = Depends(get_db),
):
    if not db.execute(text("select to_regclass('public.analytics_ebay_orders') is not null")).scalar():
        raise HTTPException(status_code=409, detail="The analytics tables don't exist yet. Run analytics_ebay_tables.sql first.")
    store = (store or "").strip().lower() or None
    if store in ("all", "__all__"):
        store = None
    p = {"store": store, "start": start, "end": end, "cap": MAX_LINES + 1}
    flt = (
        "(cast(:store as text) is null or o.store = cast(:store as text)) "
        "and (cast(:start as date) is null or o.sale_date >= cast(:start as date)) "
        "and (cast(:end as date) is null or o.sale_date <= cast(:end as date)) "
        "and o.sale_date is not null"
    )

    days = [
        dict(r)
        for r in db.execute(
            text(
                f"""select o.sale_date, count(*) as orders, coalesce(sum(o.net_sales), 0) as net_sales
                    from analytics_ebay_orders o where {flt} and o.total_price > 0
                    group by o.sale_date order by o.sale_date"""
            ),
            p,
        ).mappings().all()
    ]
    if not days:
        return {"ok": False, "reason": "No orders in this store and date range."}

    rows = db.execute(
        text(
            f"""select o.store, o.order_number, o.sale_date, o.buyer_username, upper(o.buyer_state) as state,
                       i.item_number, i.item_title, i.quantity, i.unit_price
                from analytics_ebay_order_items i
                join analytics_ebay_orders o on o.store = i.store and o.order_number = i.order_number
                where {flt} and o.total_price > 0
                order by o.sale_date, o.order_number
                limit :cap"""
        ),
        p,
    ).mappings().all()
    if len(rows) > MAX_LINES:
        raise HTTPException(status_code=413, detail="That is too many order lines for one pass. Pick a shorter date range.")

    lines = [
        {**dict(r), "unit_price": float(r["unit_price"] or 0), "quantity": int(r["quantity"] or 1)}
        for r in rows
    ]
    for d in days:
        d["net_sales"] = float(d["net_sales"] or 0)
    try:
        return build_insights(lines, days, today=date.today())
    except Exception:
        log.exception("insights failed")
        raise HTTPException(status_code=500, detail="The insights could not be worked out. The server log has the details.")
