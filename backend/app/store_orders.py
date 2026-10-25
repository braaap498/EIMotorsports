"""
Customer order tracking. Mounted at /store/*.

  POST /store/orders/track    public   order number + the email used at checkout -> status, items,
                                       tracking number, and when backordered parts are expected

The order number alone is not enough: the email must match too, and a wrong guess gets the same
"not found" answer as an order that doesn't exist, so nobody can look orders up by guessing numbers.
Nothing internal is returned (no red flags, no stock counts, no payment details).
"""
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.stock import order_shortfalls

router = APIRouter(prefix="/store", tags=["order-tracking"])


class TrackIn(BaseModel):
    order_number: str = Field(..., min_length=1, max_length=40)
    email: str = Field(..., min_length=3, max_length=200)


def _tracking_link(carrier: Optional[str], number: Optional[str]) -> Optional[str]:
    """A link to the carrier's tracking page, when we recognise the carrier (otherwise just the number)."""
    if not number:
        return None
    c = (carrier or "").lower()
    n = urllib.parse.quote(number.strip())
    if "usps" in c or "postal" in c:
        return f"https://tools.usps.com/go/TrackConfirmAction?tLabels={n}"
    if "ups" in c:
        return f"https://www.ups.com/track?tracknum={n}"
    if "fedex" in c:
        return f"https://www.fedex.com/fedextrack/?trknbr={n}"
    return None


def _label(o) -> str:
    if o.status == "cancelled":
        return "Cancelled"
    if o.status == "shipped" or o.shipped_at:
        return "Shipped"
    if o.status == "paid":
        return "Packed and ready to ship" if o.picked_at else "Paid. We're getting your order ready"
    if o.status == "pending_payment":
        return "Waiting for payment"
    return str(o.status).replace("_", " ").capitalize()


@router.post("/orders/track")
def track_order(body: TrackIn, db: Session = Depends(get_db)):
    o = db.execute(
        text(
            "select id, order_number, status, customer_email, paid_at, picked_at, shipped_at, tracking_number, "
            "       carrier, total_cents, refunded_cents, cancelled_at, shipping_address "
            "from orders where lower(order_number) = lower(:n)"
        ),
        {"n": body.order_number.strip()},
    ).first()
    if not o or (o.customer_email or "").strip().lower() != body.email.strip().lower():
        raise HTTPException(404, "We couldn't find an order with that number and email.")

    items = db.execute(
        text(
            "select oi.product_id, oi.title, oi.quantity, oi.unit_price_cents from order_items oi "
            "where oi.order_id = :o order by oi.id"
        ),
        {"o": o.id},
    ).all()

    # Parts still waiting on stock, and only the ones that are on a purchase order (so there is a real
    # arrival to talk about). Everything else is simply "being prepared".
    waiting = {}
    if o.status == "paid" and not o.picked_at:
        for r in order_shortfalls(db, [o.id]):
            if r.on_order:
                waiting[r.product_id] = r

    out_items = []
    for i in items:
        w = waiting.get(i.product_id)
        out_items.append(
            {
                "title": i.title,
                "quantity": i.quantity,
                "unit_price_cents": i.unit_price_cents,
                "backordered": bool(w),
                "backordered_units": int(w.short) if w else 0,
                "expected_date": w.expected_date.isoformat() if w and w.expected_date else None,
                "days_until": int(w.days_until) if w and w.days_until is not None else None,
            }
        )
    addr = o.shipping_address or {}
    return {
        "order_number": o.order_number,
        "status": o.status,
        "status_label": _label(o),
        "paid_at": o.paid_at,
        "shipped_at": o.shipped_at,
        "tracking_number": o.tracking_number,
        "carrier": o.carrier,
        "tracking_url": _tracking_link(o.carrier, o.tracking_number),
        "total_cents": o.total_cents,
        "refunded_cents": o.refunded_cents or 0,
        "ship_to": {"name": addr.get("full_name"), "city": addr.get("city"), "region": addr.get("region")},
        "has_backorder": bool(waiting),
        "items": out_items,
    }
