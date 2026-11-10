"""Order activity log (Inventory > History > Order activity).

One row each time somebody does something to an order:

  picked           parts scanned/taken off the shelf for the order
  label_opened     pressed "Open eBay label" (we cannot see whether a label was actually bought on eBay)
  released         an eBay line's parts were released (no longer set aside, shelf count unchanged)
  tracking_shipped tracking number entered and the order marked shipped (website and eBay)

Opening an order is NOT logged on purpose (releasing an eBay line is: it changes what is set aside).

Every row carries the sales channel ("Website", "eBay", later "Amazon"), that channel's own order number, and the EIO
number. An EIO number is made the first time an order is logged, from one shared sequence for every channel
(EIO-000001, EIO-000002, ...), and the same order always gets the same one. Adding Amazon later only means logging with
channel "Amazon".

Write rule: log_action() runs inside the caller's transaction (so the row is saved or rolled back together with the action)
but inside a savepoint, so a problem with the log can never stop a pick or a shipment.

Tables are created at startup (ensure_tables), never in a request, because creating a table commits the open transaction.
"""
import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user, needs, require_route_access
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)

# Used under /admin by main.py; every route here needs a @needs(...) marker or it is admin-only.
router = APIRouter(prefix="/inventory/activity", tags=["admin-order-activity"], dependencies=[Depends(require_route_access)])

ACTIONS = {
    "picked": "Picked",
    "label_opened": "Label button pressed",
    "released": "Released",
    "tracking_shipped": "Tracking entered / shipped",
}
CHANNELS = ("Website", "eBay", "Amazon")
EIO_DIGITS = 6
_ready = False


def ensure_tables() -> None:
    """Create the two tables if they are missing. Called once at startup, on its own connection."""
    global _ready
    if _ready:
        return
    db = _session_factory()()
    try:
        db.execute(
            text(
                """
                create table if not exists order_eio (
                    seq bigint generated always as identity primary key,
                    channel text not null,
                    order_ref text not null,
                    created_at timestamptz not null default now(),
                    unique (channel, order_ref)
                )
                """
            )
        )
        db.execute(
            text(
                """
                create table if not exists order_activity (
                    id bigint generated always as identity primary key,
                    at timestamptz not null default now(),
                    user_email text,
                    action text not null,
                    channel text not null,
                    order_ref text not null,
                    eio_number text not null,
                    detail text
                )
                """
            )
        )
        db.execute(text("create index if not exists order_activity_at_idx on order_activity (at desc)"))
        db.execute(text("create index if not exists order_activity_ref_idx on order_activity (lower(order_ref))"))
        db.execute(text("create index if not exists order_activity_eio_idx on order_activity (eio_number)"))
        db.commit()
        _ready = True
    except Exception:
        db.rollback()
        log.exception("Could not create the order activity tables; the activity log will be skipped until the next restart")
    finally:
        db.close()


def eio_for(db: Session, channel: str, order_ref: str) -> str:
    """The EIO number of this order, made on first use."""
    db.execute(
        text("insert into order_eio (channel, order_ref) values (:c, :r) on conflict (channel, order_ref) do nothing"),
        {"c": channel, "r": order_ref},
    )
    seq = db.execute(
        text("select seq from order_eio where channel = :c and order_ref = :r"), {"c": channel, "r": order_ref}
    ).scalar()
    return f"EIO-{int(seq):0{EIO_DIGITS}d}"


def log_action(
    db: Session,
    user: Optional[AuthUser],
    action: str,
    channel: str,
    order_ref: str,
    detail: Optional[str] = None,
) -> None:
    """Add one row. Never raises: a failed log must not undo or block the real action."""
    ref = (order_ref or "").strip()
    if not ref or action not in ACTIONS or not _ready:
        return
    try:
        with db.begin_nested():
            if action == "picked":
                detail = ((detail or "") + _taken_from_other_warehouses(db)).strip()
            eio = eio_for(db, channel, ref)
            db.execute(
                text(
                    "insert into order_activity (user_email, action, channel, order_ref, eio_number, detail) "
                    "values (:u, :a, :c, :r, :e, :d)"
                ),
                {"u": (user.email if user else None), "a": action, "c": channel, "r": ref, "e": eio, "d": (detail or None) and detail[:500]},
            )
    except Exception:
        log.exception("Could not write the order activity log (%s %s %s)", action, channel, ref)


def _taken_from_other_warehouses(db: Session) -> str:
    """' Taken from Reno: ei-0001 x3' when this same transaction had to draw on another warehouse (the main one ran out)."""
    try:
        rows = db.execute(
            text(
                "select w.name, coalesce(p.sku_display, '#' || p.id::text) as sku, sum(-l.delta) as n "
                "from warehouse_stock_log l join warehouses w on w.id = l.warehouse_id join products p on p.id = l.product_id "
                "where l.txid = txid_current() and l.delta < 0 group by w.name, p.id, p.sku_display order by w.name, p.id"
            )
        ).all()
    except Exception:
        return ""
    if not rows:
        return ""
    by: dict[str, list[str]] = {}
    for r in rows:
        by.setdefault(r.name, []).append(f"{r.sku} x{int(r.n)}")
    return " | " + "; ".join(f"Taken from {name}: {', '.join(items)}" for name, items in by.items())


def parts_summary(db: Session, qty_by_product: dict) -> str:
    """'ei-0001 x2, ei-0042 x1' for a pick (qty_by_product is {product_id: units})."""
    if not qty_by_product:
        return ""
    try:
        rows = db.execute(
            text("select id, coalesce(sku_display, '#' || id::text) as sku from products where id = any(cast(:ids as bigint[]))"),
            {"ids": sorted(int(i) for i in qty_by_product)},
        ).all()
        sku = {r.id: r.sku for r in rows}
        return ", ".join(f"{sku.get(int(p), '#' + str(p))} x{int(q)}" for p, q in qty_by_product.items())[:480]
    except Exception:
        return ""


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
class LabelIn(BaseModel):
    channel: str = Field("eBay", max_length=20)
    order_ref: str = Field(..., min_length=1, max_length=100)


@router.post("/label-opened")
@needs("inventory.ship")
def label_opened(body: LabelIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    """Someone pressed the button that opens the shipping label page."""
    channel = body.channel if body.channel in CHANNELS else "eBay"
    log_action(db, user, "label_opened", channel, body.order_ref, "Opened the label page")
    db.commit()
    return {"ok": True}


def _names() -> dict[str, str]:
    try:
        from app.admin_purchase_orders import _staff_names

        return _staff_names()
    except Exception:
        return {}


# what each column heading sorts by (a fixed list, so nothing the browser sends ever reaches the SQL)
_ACTIVITY_SORTS = {
    "date": "at",
    "employee": "lower(user_email)",
    "action": "action",
    "eio": "eio_number",
    "channel": "channel",
    "order": "order_ref",
    "detail": "detail",
}


@router.get("")
@needs("inventory.receive", "inventory.ship", "inventory.barcodes", "inventory.adjust")
def activity(
    q: Optional[str] = Query(None, max_length=100),
    action: Optional[str] = Query(None, max_length=30),
    channel: Optional[str] = Query(None, max_length=20),
    date_from: str = Query("", max_length=10),
    date_to: str = Query("", max_length=10),
    sort: str = Query("date", max_length=12),
    dir: str = Query("desc", max_length=4),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Newest first unless sort / dir say otherwise. q matches the EIO number, the channel's own order number, or the employee's email.
    date_from / date_to (YYYY-MM-DD, both days included) keep only what happened between them."""
    if action and action not in ACTIONS:
        raise HTTPException(400, "Unknown action")
    where, params = [], {"limit": limit, "offset": offset}
    term = (q or "").strip()
    if term:
        params["q"] = f"%{term.replace('%', '').replace('_', '')}%"
        where.append("(eio_number ilike :q or order_ref ilike :q or user_email ilike :q or detail ilike :q)")
    if action:
        where.append("action = :action")
        params["action"] = action
    if channel:
        where.append("channel = :channel")
        params["channel"] = channel
    for key, raw, op in (("df", date_from, ">="), ("dt", date_to, "<")):
        if raw.strip():
            try:
                d = date.fromisoformat(raw.strip())
            except ValueError:
                raise HTTPException(422, "Dates must look like 2026-11-07.")
            params[key] = d if key == "df" else d + timedelta(days=1)  # "to" includes the whole last day
            where.append(f"at {op} :{key}")
    clause = ("where " + " and ".join(where)) if where else ""
    col = _ACTIVITY_SORTS.get(sort, _ACTIVITY_SORTS["date"])
    direction = "asc" if dir == "asc" else "desc"
    total = db.execute(text(f"select count(*) from order_activity {clause}"), params).scalar() or 0
    rows = db.execute(
        text(
            f"select id, at, user_email, action, channel, order_ref, eio_number, detail from order_activity {clause} "
            f"order by {col} {direction} nulls last, at desc, id desc limit :limit offset :offset"
        ),
        params,
    ).all()
    names = _names()
    items = []
    for r in rows:
        email = r.user_email
        items.append(
            {
                "id": r.id,
                "at": r.at,
                "employee": names.get((email or "").strip().lower()) or ((email or "").split("@")[0] or None),
                "email": email,
                "action": r.action,
                "action_label": ACTIONS.get(r.action, r.action),
                "channel": r.channel,
                "order_ref": r.order_ref,
                "eio_number": r.eio_number,
                "detail": r.detail,
            }
        )
    return {"items": items, "total": int(total)}
