"""
eBay sales capture: every sale on every connected store becomes a record here, linked to the
listing (item id) and the product, and sets the parts aside so "available" drops at once.

Mounted at /admin/ebay-sales/* (owner/admin only), plus ONE public route for an optional outside
"pinger" (see run_due_public) that wakes a sleeping free-tier server.

HOW A SALE FLOWS
  1. A check (poll) asks eBay's Trading API GetOrders for orders CHANGED since the last check
     (with 15 minutes of overlap, so a late change is never missed). Nothing is lost if the server
     was asleep: the next check simply starts from where the last good one ended.
  2. Each order is saved (ebay_orders) with one line per item (ebay_order_lines). The line keeps the
     eBay item id, so sales can be reported per listing as well as per product.
  3. A PAID line that is linked to a product SETS PARTS ASIDE (stock_reservations, the same table
     website orders use), writes a sales_transactions row, and queues a quantity update for every
     other eBay listing of that product, so none of them can be bought past what is left.
     quantity_on_hand does NOT change here. It changes when the part is scanned out on the Ship
     screen with the eBay order number as the reference (see take_stock_for_reference).
  4. A cancelled order that has not been picked releases its parts and removes its sales row.
  4b. Every paid order is also copied into the analytics tables (feed_analytics), so the eBay Analytics tab
     needs no report uploads for new sales. A report you upload later replaces those rows for its dates.
  5. A line whose item is not linked to a product yet is kept as 'unmatched' and re-checked on
     every poll, so it starts counting as soon as the listing is linked.

REFUNDS: eBay's refund amounts (order MonetaryDetails) are saved on the order. A FULLY refunded order is not a
sale: it is treated like a cancelled one (parts released if not yet picked, sales row removed). A partial refund
keeps the sale and records the refunded amount on its sales row (sales_transactions.refund_amount) so reports can
subtract it. Parts already picked are NOT put back on the shelf automatically; whether the part comes back is a
person's decision (adjust the count on the Inventory screen if it does).

THE FIRST CHECK (and "import history") never sets parts aside for orders eBay already shows as
shipped; those are recorded as 'history' so old, finished sales don't lock up stock.

Quantity updates on eBay only run when BOTH "Live pushes" and "Update quantities after a sale" are
ON for the store, and only touch active fixed-price listings without variations.
"""
import hmac
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin
from app.database import _session_factory, get_db
from app.ebay_edit import NS, _failed, _first_message, _read_item, _trading
from app.stock import available_sql, lock_products

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-sales", tags=["admin-ebay-sales"], dependencies=[Depends(require_admin)])
public_router = APIRouter(tags=["ebay-sales-public"])

OVERLAP = timedelta(minutes=15)
FIRST_LOOKBACK_DAYS = 3
MAX_WINDOW_DAYS = 89           # eBay will not look further back than 90 days
MAX_CALL_DAYS = 29             # eBay's GetOrders refuses a ModTime window over 30 days; longer look-backs are split
QTY_PER_RUN = 20               # listings updated on eBay per check (each one is two eBay calls)
QTY_MAX_ATTEMPTS = 5
DIGITS = "regexp_replace(cast(external_id as text), '[^0-9]', '', 'g')"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _num(s: Optional[str]) -> Optional[Decimal]:
    try:
        return Decimal((s or "").strip()) if (s or "").strip() else None
    except InvalidOperation:
        return None


def _t(el: ET.Element, path: str) -> str:
    return (el.findtext("/".join(NS + p for p in path.split("/"))) or "").strip()


# --------------------------------------------------------------------------
# reading orders from eBay
# --------------------------------------------------------------------------
def _parse_orders(root: ET.Element) -> list[dict]:
    out = []
    for o in root.findall(f"{NS}OrderArray/{NS}Order"):
        order_id = _t(o, "OrderID")
        if not order_id:
            continue
        lines = []
        tax = Decimal(0)
        for tr in o.findall(f"{NS}TransactionArray/{NS}Transaction"):
            tax += _num(_t(tr, "Taxes/TotalTaxAmount")) or Decimal(0)
            item_id = _t(tr, "Item/ItemID")
            if not item_id:
                continue
            txn = _t(tr, "TransactionID")
            lines.append(
                {
                    "line_key": _t(tr, "OrderLineItemID") or f"{item_id}-{txn or '0'}",
                    "item_id": item_id,
                    "txn": txn or None,
                    "title": _t(tr, "Item/Title") or None,
                    "label": _t(tr, "Item/SKU") or None,
                    "qty": int(_t(tr, "QuantityPurchased") or 1),
                    "price": _num(_t(tr, "TransactionPrice")),
                    "fee": _num(_t(tr, "FinalValueFee")),
                }
            )
        refund, refunded_at = Decimal(0), None
        for rf in o.findall(f"{NS}MonetaryDetails/{NS}Refunds/{NS}Refund"):
            if _t(rf, "RefundStatus").lower() == "failed":
                continue
            refund += _num(_t(rf, "RefundAmount")) or Decimal(0)
            when = _dt(_t(rf, "RefundTime"))
            if when and (refunded_at is None or when > refunded_at):
                refunded_at = when
        out.append(
            {
                "refund": refund,
                "refunded_at": refunded_at,
                "tax": tax,
                "buyer": _t(o, "BuyerUserID")[:80] or None,
                "country": _t(o, "ShippingAddress/Country")[:60] or None,
                "ship_service": _t(o, "ShippingServiceSelected/ShippingService")[:80] or None,
                "id": order_id,
                "status": _t(o, "OrderStatus"),
                "created": _dt(_t(o, "CreatedTime")),
                "paid": _dt(_t(o, "PaidTime")),
                "shipped": _dt(_t(o, "ShippedTime")),
                "modified": _dt(_t(o, "CheckoutStatus/LastModifiedTime")),
                "state": _t(o, "ShippingAddress/StateOrProvince") or None,
                "zip": _t(o, "ShippingAddress/PostalCode") or None,
                "ship_charged": _num(_t(o, "ShippingServiceSelected/ShippingServiceCost")),
                "total": _num(_t(o, "Total")),
                "lines": lines,
            }
        )
    return out


def _fetch_orders(db: Session, store: str, since: datetime, until: datetime):
    """All orders changed between `since` and `until`. eBay's GetOrders refuses a time window longer than 30 days
    (error 406), so a longer look-back (the 90-day import, or a check after the server slept a long time) is asked
    for in pieces of at most MAX_CALL_DAYS days."""
    start = since
    while start < until:
        end = min(start + timedelta(days=MAX_CALL_DAYS), until)
        yield from _fetch_orders_window(db, store, start, end)
        start = end


def _fetch_orders_window(db: Session, store: str, since: datetime, until: datetime):
    page = 1
    while True:
        inner = (
            f"<ModTimeFrom>{_iso(since)}</ModTimeFrom><ModTimeTo>{_iso(until)}</ModTimeTo>"
            "<OrderRole>Seller</OrderRole><OrderStatus>All</OrderStatus>"
            f"<Pagination><EntriesPerPage>100</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>"
        )
        root = _trading(db, store, "GetOrders", inner)
        if _failed(root):
            raise HTTPException(502, _first_message(root))
        yield from _parse_orders(root)
        if (root.findtext(f"{NS}HasMoreOrders") or "").lower() != "true" or page >= 60:
            return
        page += 1


# --------------------------------------------------------------------------
# matching a listing to a product, and the channel id eBay sales are filed under
# --------------------------------------------------------------------------
def _match_product(db: Session, item_id: str) -> Optional[int]:
    r = db.execute(
        text("select product_id from ebay_listings where ebay_item_id = :i and product_id is not null limit 1"),
        {"i": item_id},
    ).first()
    if r:
        return r.product_id
    r = db.execute(
        text(f"select product_id from channel_listings where {DIGITS} = :i and product_id is not null limit 1"),
        {"i": item_id},
    ).first()
    if r:
        return r.product_id
    r = db.execute(
        text(
            "select id from products where regexp_replace(coalesce(ebay_item_id, ''), '[^0-9]', '', 'g') = :i limit 1"
        ),
        {"i": item_id},
    ).first()
    return r.id if r else None


def _channel_id(db: Session) -> Optional[int]:
    return db.execute(
        text("select channel_id from channel_listings group by channel_id order by count(*) desc limit 1")
    ).scalar()


# --------------------------------------------------------------------------
# what one order line does to stock and the sales record
# --------------------------------------------------------------------------
def _release_line(db: Session, line_id: int, sales_id: Optional[int]) -> None:
    db.execute(text("delete from stock_reservations where ebay_order_line_id = :l"), {"l": line_id})
    if sales_id:
        db.execute(text("delete from sales_transactions where id = :s"), {"s": sales_id})


def _apply_line(db: Session, ln, od: dict, channel_id: Optional[int], skip_shipped: bool, first_in_order: bool) -> tuple[Optional[int], dict]:
    """Moves one order line to the state it should be in. Returns (product id whose stock changed or None, counts)."""
    c = {"reserved": 0, "cancelled": 0, "unmatched": 0}
    if ln.state in ("picked", "history", "cancelled", "reserved") and not od["cancelled"]:
        return None, c        # nothing to do: already finished, or already set aside (do not count it again)
    if od["cancelled"]:
        if ln.state in ("pending", "reserved", "unmatched"):
            _release_line(db, ln.id, ln.sales_transaction_id)
            db.execute(
                text(
                    "update ebay_order_lines set state = 'cancelled', cancelled_at = now(), "
                    "sales_transaction_id = null where id = :l"
                ),
                {"l": ln.id},
            )
            c["cancelled"] = 1
            return ln.product_id, c
        return None, c
    if not od["paid"]:
        return None, c

    pid = ln.product_id or _match_product(db, ln.ebay_item_id)
    if pid is None:
        if ln.state != "unmatched":
            db.execute(text("update ebay_order_lines set state = 'unmatched' where id = :l"), {"l": ln.id})
        c["unmatched"] = 1
        return None, c

    shipped_already = bool(od["shipped"]) and skip_shipped
    sales_id = ln.sales_transaction_id
    if sales_id is None and channel_id is not None:
        sales_id = db.execute(
            text(
                """
                insert into sales_transactions
                  (product_id, channel_id, quantity_sold, unit_sale_price, unit_cost_at_sale, shipping_charged,
                   platform_fees, sale_date, order_reference, ship_to_state, ship_to_zip, ebay_item_id)
                values (:p, :ch, :q, :price,
                        (select nullif(to_jsonb(p)->>'cost', '')::numeric from products p where p.id = :p),
                        :ship, :fee, :d, :ref, :st, :zip, :item)
                returning id
                """
            ),
            {
                "p": pid, "ch": channel_id, "q": ln.quantity, "price": ln.unit_price,
                "ship": (od["ship_charged"] or 0) if first_in_order else 0, "fee": ln.fees,
                "d": ln.sale_date or od["paid"], "ref": ln.ebay_order_id, "st": od["state"], "zip": od["zip"],
                "item": ln.ebay_item_id,
            },
        ).scalar()
    if shipped_already:
        db.execute(
            text("update ebay_order_lines set state = 'history', product_id = :p, sales_transaction_id = :s where id = :l"),
            {"p": pid, "s": sales_id, "l": ln.id},
        )
        return None, c

    lock_products(db, [pid])
    db.execute(
        text(
            "insert into stock_reservations (order_id, product_id, quantity, status, expires_at, ebay_order_line_id) "
            "values (null, :p, :q, 'reserved', null, :l) on conflict (ebay_order_line_id) "
            "where ebay_order_line_id is not null do nothing"
        ),
        {"p": pid, "q": ln.quantity, "l": ln.id},
    )
    db.execute(
        text(
            "update ebay_order_lines set state = 'reserved', product_id = :p, sales_transaction_id = :s, "
            "reserved_at = coalesce(reserved_at, now()) where id = :l"
        ),
        {"p": pid, "s": sales_id, "l": ln.id},
    )
    c["reserved"] = 1
    return pid, c


_LINE_SQL = (
    "select id, state, product_id, quantity, unit_price, fees, ebay_item_id, ebay_order_id, "
    "sales_transaction_id, sale_date from ebay_order_lines "
)


def _save_order(db: Session, store: str, o: dict, channel_id: Optional[int], skip_shipped: bool) -> tuple[set, dict]:
    counts = {"orders": 1, "new_lines": 0, "reserved": 0, "cancelled": 0, "unmatched": 0, "refunded": 0, "pk": 0}
    cancelled = o["status"] in ("Cancelled", "Inactive")
    paid = bool(o["paid"]) or o["status"] in ("Completed", "Shipped", "InProcess")
    row = db.execute(
        text(
            """
            insert into ebay_orders (store, ebay_order_id, ebay_status, paid_at, created_at_ebay, modified_at_ebay,
                                     shipped_at_ebay, buyer_state, buyer_zip, shipping_charged, total, cancelled,
                                     refund_total, refunded_at, buyer_username, buyer_country, shipping_service, tax_total)
            values (:s, :o, :st, :paid, :cr, :mod, :sh, :bs, :bz, :sc, :tot, :can, :rf, :rfat, :bu, :bc, :sv, :tx)
            on conflict (store, ebay_order_id) do update set
              ebay_status = excluded.ebay_status,
              paid_at = coalesce(excluded.paid_at, ebay_orders.paid_at),
              modified_at_ebay = excluded.modified_at_ebay,
              shipped_at_ebay = coalesce(excluded.shipped_at_ebay, ebay_orders.shipped_at_ebay),
              buyer_state = coalesce(excluded.buyer_state, ebay_orders.buyer_state),
              buyer_zip = coalesce(excluded.buyer_zip, ebay_orders.buyer_zip),
              shipping_charged = coalesce(excluded.shipping_charged, ebay_orders.shipping_charged),
              total = coalesce(excluded.total, ebay_orders.total),
              cancelled = excluded.cancelled,
              refund_total = greatest(ebay_orders.refund_total, excluded.refund_total),
              refunded_at = coalesce(excluded.refunded_at, ebay_orders.refunded_at),
              buyer_username = coalesce(excluded.buyer_username, ebay_orders.buyer_username),
              buyer_country = coalesce(excluded.buyer_country, ebay_orders.buyer_country),
              shipping_service = coalesce(excluded.shipping_service, ebay_orders.shipping_service),
              tax_total = case when excluded.tax_total > 0 then excluded.tax_total else ebay_orders.tax_total end,
              updated_at = now()
            returning id
            """
        ),
        {
            "s": store, "o": o["id"], "st": o["status"], "paid": o["paid"], "cr": o["created"], "mod": o["modified"],
            "sh": o["shipped"], "bs": o["state"], "bz": o["zip"], "sc": o["ship_charged"], "tot": o["total"],
            "can": cancelled, "rf": o["refund"], "rfat": o["refunded_at"],
            "bu": o["buyer"], "bc": o["country"], "sv": o["ship_service"], "tx": o["tax"],
        },
    ).first()
    order_pk = row.id
    counts["pk"] = order_pk
    # A FULLY refunded order is not a sale: treated like a cancelled one (parts released if not yet picked,
    # sales row removed). A partial refund keeps the sale and records the refunded amount against it.
    fully_refunded = o["refund"] > 0 and (o["total"] or 0) > 0 and o["refund"] >= (o["total"] - Decimal("0.01"))
    od = {
        "paid": paid, "cancelled": cancelled or fully_refunded, "shipped": o["shipped"], "state": o["state"], "zip": o["zip"],
        "ship_charged": o["ship_charged"],
    }
    changed: set = set()
    for idx, l in enumerate(o["lines"]):
        ex = db.execute(text(_LINE_SQL + "where order_pk = :o and line_key = :k"), {"o": order_pk, "k": l["line_key"]}).first()
        if ex is None:
            lid = db.execute(
                text(
                    """
                    insert into ebay_order_lines (order_pk, store, ebay_order_id, line_key, ebay_item_id, ebay_transaction_id,
                                                  title, custom_label, quantity, unit_price, fees, sale_date, state)
                    values (:op, :s, :o, :k, :i, :t, :ti, :cl, :q, :pr, :fe, :d, 'pending')
                    returning id
                    """
                ),
                {
                    "op": order_pk, "s": store, "o": o["id"], "k": l["line_key"], "i": l["item_id"], "t": l["txn"],
                    "ti": l["title"], "cl": l["label"], "q": l["qty"], "pr": l["price"], "fe": l["fee"],
                    "d": o["paid"] or o["created"],
                },
            ).scalar()
            counts["new_lines"] += 1
            ex = db.execute(text(_LINE_SQL + "where id = :l"), {"l": lid}).first()
        pid, c = _apply_line(db, ex, od, channel_id, skip_shipped, idx == 0)
        if pid:
            changed.add(pid)
        for k in ("reserved", "cancelled", "unmatched"):
            counts[k] += c[k]
    if o["refund"] > 0:
        counts["refunded"] = 1
        sales = db.execute(
            text(
                "select sales_transaction_id as sid, coalesce(unit_price, 0) * quantity as v from ebay_order_lines "
                "where order_pk = :o and sales_transaction_id is not null"
            ),
            {"o": order_pk},
        ).all()
        total_v = sum(float(r.v) for r in sales)
        for r in sales:
            share = o["refund"] * (Decimal(str(r.v)) / Decimal(str(total_v))) if total_v > 0 else o["refund"] / len(sales)
            db.execute(
                text("update sales_transactions set refund_amount = :a where id = :s"),
                {"a": share.quantize(Decimal("0.01")), "s": r.sid},
            )
    return changed, counts


def _rematch(db: Session, store: str, channel_id: Optional[int]) -> tuple[set, dict]:
    """Lines saved before their listing was linked to a product: try again."""
    counts = {"reserved": 0, "cancelled": 0, "unmatched": 0}
    changed: set = set()
    rows = db.execute(
        text(
            "select l.id, l.state, l.product_id, l.quantity, l.unit_price, l.fees, l.ebay_item_id, l.ebay_order_id, "
            "l.sales_transaction_id, l.sale_date, o.paid_at, o.cancelled, o.shipped_at_ebay, o.buyer_state, "
            "o.buyer_zip, o.shipping_charged from ebay_order_lines l join ebay_orders o on o.id = l.order_pk "
            "where l.store = :s and l.state = 'unmatched' limit 500"
        ),
        {"s": store},
    ).all()
    for r in rows:
        od = {
            "paid": bool(r.paid_at), "cancelled": r.cancelled, "shipped": r.shipped_at_ebay, "state": r.buyer_state,
            "zip": r.buyer_zip, "ship_charged": r.shipping_charged,
        }
        pid, c = _apply_line(db, r, od, channel_id, True, True)
        db.commit()
        if pid:
            changed.add(pid)
        counts["reserved"] += c["reserved"]
    return changed, counts


# --------------------------------------------------------------------------
# keeping eBay's quantities in step (every listing of a product)
# --------------------------------------------------------------------------
def _available(db: Session, product_id: int) -> int:
    return int(db.execute(text(f"select {available_sql('p')} from products p where p.id = :p"), {"p": product_id}).scalar() or 0)


def enqueue_product(db: Session, product_id: int) -> int:
    """Queue a quantity update for each eBay listing of this product, on stores that allow it."""
    avail = _available(db, product_id)
    rows = db.execute(
        text(
            f"select cl.store, {DIGITS.replace('external_id', 'cl.external_id')} as item "
            "from channel_listings cl join ebay_accounts a on a.store = cl.store "
            "where cl.product_id = :p and cl.external_id is not null "
            "and a.sales_qty_push_enabled and a.live_pushes_enabled and a.refresh_token_enc is not null"
        ),
        {"p": product_id},
    ).all()
    for r in rows:
        if not r.item:
            continue
        db.execute(
            text(
                "insert into ebay_qty_queue (store, ebay_item_id, product_id, target_qty) values (:s, :i, :p, :q) "
                "on conflict (store, ebay_item_id) where status = 'pending' do update set target_qty = excluded.target_qty"
            ),
            {"s": r.store, "i": r.item, "p": product_id, "q": avail},
        )
    return len(rows)


def run_queue(db: Session, store: str, limit: int = QTY_PER_RUN) -> dict:
    """Send the queued quantities to eBay (a few per check)."""
    flags = db.execute(
        text("select sales_qty_push_enabled as q, live_pushes_enabled as l from ebay_accounts where store = :s"), {"s": store}
    ).first()
    out = {"done": 0, "skipped": 0, "failed": 0}
    if not flags or not (flags.q and flags.l):
        return out
    rows = db.execute(
        text(
            "select id, ebay_item_id, product_id, target_qty, attempts from ebay_qty_queue "
            "where store = :s and status = 'pending' order by id limit :n"
        ),
        {"s": store, "n": limit},
    ).all()
    for r in rows:
        target = _available(db, r.product_id) if r.product_id else r.target_qty
        status, note = "done", None
        try:
            cur = _read_item(db, store, r.ebay_item_id)
            if cur.get("has_variations") or cur.get("status") != "Active" or cur.get("listing_type") not in (
                "FixedPriceItem", "StoresFixedPrice"
            ):
                status, note = "skipped", f"Not changed: {cur.get('status') or 'listing'} / {cur.get('listing_type') or ''}"
            elif cur["quantity_available"] != target:
                root = _trading(
                    db, store, "ReviseFixedPriceItem",
                    f"<Item><ItemID>{r.ebay_item_id}</ItemID><Quantity>{target + cur['quantity_sold']}</Quantity></Item>",
                )
                if _failed(root):
                    raise HTTPException(502, _first_message(root))
        except HTTPException as e:
            status, note = ("failed" if r.attempts + 1 >= QTY_MAX_ATTEMPTS else "pending"), str(e.detail)[:300]
        except Exception as e:  # never let one listing stop the rest
            status, note = ("failed" if r.attempts + 1 >= QTY_MAX_ATTEMPTS else "pending"), type(e).__name__
        db.execute(
            text(
                "update ebay_qty_queue set status = :st, attempts = attempts + 1, last_error = :n, "
                "target_qty = :t, done_at = case when :st in ('done','skipped','failed') then now() end where id = :id"
            ),
            {"st": status, "n": note, "t": target, "id": r.id},
        )
        db.commit()
        if status in out:
            out[status] += 1
    return out


# --------------------------------------------------------------------------
# one check of one store
# --------------------------------------------------------------------------
def _claim(db: Session, store: str) -> bool:
    r = db.execute(
        text(
            "update ebay_accounts set sales_lock_at = now() where store = :s "
            "and (sales_lock_at is null or sales_lock_at < now() - interval '10 minutes') returning store"
        ),
        {"s": store},
    ).first()
    db.commit()
    return r is not None


def poll_store(db: Session, store: str, days: Optional[int] = None) -> dict:
    """Check one store for new and changed orders. `days` = look back that far (history import)."""
    if not _claim(db, store):
        raise HTTPException(409, "A check for this store is already running. Try again in a minute.")
    summary = {"orders": 0, "new_lines": 0, "reserved": 0, "cancelled": 0, "unmatched": 0, "refunded": 0, "errors": 0, "fed": 0}
    error: Optional[str] = None
    started = _now()
    try:
        acct = db.execute(text("select sales_cursor from ebay_accounts where store = :s"), {"s": store}).first()
        if acct is None:
            raise HTTPException(404, "That store is not in the list.")
        if days:
            since, skip_shipped = started - timedelta(days=min(days, MAX_WINDOW_DAYS)), True
        elif acct.sales_cursor:
            since, skip_shipped = max(acct.sales_cursor - OVERLAP, started - timedelta(days=MAX_WINDOW_DAYS)), False
        else:
            since, skip_shipped = started - timedelta(days=FIRST_LOOKBACK_DAYS), True
        channel_id = _channel_id(db)
        changed: set = set()
        touched: list = []
        for o in _fetch_orders(db, store, since, started):
            try:
                ch, c = _save_order(db, store, o, channel_id, skip_shipped)
                db.commit()
                touched.append(c.pop("pk"))
                changed |= ch
                for k in ("orders", "new_lines", "reserved", "cancelled", "unmatched", "refunded"):
                    summary[k] += c[k]
            except Exception:
                db.rollback()
                summary["errors"] += 1
                log.exception("eBay order %s on %s could not be saved", o.get("id"), store)
        ch, c = _rematch(db, store, channel_id)
        changed |= ch
        summary["reserved"] += c["reserved"]
        for pid in changed:
            enqueue_product(db, pid)
        db.commit()
        fed = 0
        try:  # a problem here must never stop the sales themselves from being saved
            if _analytics_ready(db):
                everything = _api_rows(db, store.lower()) == 0
                fed = feed_analytics(db, store, None if everything else touched)
        except Exception:
            db.rollback()
            log.exception("sending %s orders to analytics failed", store)
        summary["fed"] = fed
        q = run_queue(db, store)
        msg = (
            f"{summary['orders']} orders checked, {summary['new_lines']} new lines, {summary['reserved']} set aside, "
            f"{summary['cancelled']} cancelled, {summary['refunded']} with refunds, {summary['unmatched']} not linked to a product, "
            f"{summary['fed']} orders sent to analytics"
        )
        if summary["errors"]:
            msg += f", {summary['errors']} could not be saved (see server log)"
        if q["done"] or q["failed"] or q["skipped"]:
            msg += f"; eBay quantities: {q['done']} updated, {q['skipped']} skipped, {q['failed']} failed"
        db.execute(
            text(
                "update ebay_accounts set sales_cursor = :c, sales_last_ok_at = now(), sales_last_summary = :m, "
                "sales_last_error = null where store = :s"
            ),
            {"c": started, "m": msg, "s": store},
        )
        db.commit()
        return {"ok": True, "summary": msg}
    except HTTPException as e:
        db.rollback()
        error = str(e.detail)[:400]
        raise
    except Exception as e:
        db.rollback()
        error = f"{type(e).__name__}: {str(e)[:300]}"
        raise HTTPException(500, error)
    finally:
        try:
            db.execute(
                text(
                    "update ebay_accounts set sales_last_polled_at = now(), sales_lock_at = null, "
                    "sales_last_error = coalesce(:e, sales_last_error) where store = :s"
                ),
                {"e": error, "s": store},
            )
            db.commit()
        except Exception:
            db.rollback()


def run_due() -> int:
    """Check every store whose own timer says it is due. Returns how many were checked."""
    db = _session_factory()()
    n = 0
    try:
        stores = [
            r.store
            for r in db.execute(
                text(
                    "select store from ebay_accounts where sales_polling_enabled and refresh_token_enc is not null "
                    "and (sales_last_polled_at is null or "
                    "     sales_last_polled_at + make_interval(mins => sales_poll_minutes) <= now())"
                )
            ).all()
        ]
        for s in stores:
            try:
                poll_store(db, s)
                n += 1
            except HTTPException:
                pass  # the reason is saved on the store (sales_last_error)
    finally:
        db.close()
    return n


def _loop() -> None:
    time.sleep(20)
    while True:
        try:
            run_due()
        except Exception:
            log.exception("eBay sales loop")
        time.sleep(30)


_started = False


def start_background() -> None:
    """Called once when the API starts. Set EBAY_SALES_THREAD=0 to turn the built-in timer off."""
    global _started
    if _started or os.environ.get("EBAY_SALES_THREAD", "1") == "0":
        return
    _started = True
    threading.Thread(target=_loop, name="ebay-sales", daemon=True).start()


@public_router.get("/internal/ebay-sales/poll-due")
def run_due_public(x_poll_key: Optional[str] = Header(None), key: Optional[str] = Query(None)):
    """For an outside pinger (a free cron service). Wakes a sleeping server and checks any store that is due.
    Needs EBAY_POLL_SECRET set on the API; send it as ?key= or the X-Poll-Key header."""
    secret = os.environ.get("EBAY_POLL_SECRET", "")
    given = x_poll_key or key or ""
    if not secret or not hmac.compare_digest(secret.encode(), given.encode()):
        raise HTTPException(404, "Not found")
    return {"checked": run_due()}


# --------------------------------------------------------------------------
# the live feed into the analytics tables (analytics_ebay_orders / analytics_ebay_order_items)
# --------------------------------------------------------------------------
API_SOURCE = "api"


def _analytics_ready(db: Session) -> bool:
    return bool(db.execute(text("select to_regclass('public.analytics_ebay_orders') is not null")).scalar())


def _api_rows(db: Session, store_lc: str) -> int:
    return int(
        db.execute(
            text("select count(*) from analytics_ebay_orders where store = :s and source_file = :a"),
            {"s": store_lc, "a": API_SOURCE},
        ).scalar()
        or 0
    )


def _boundary(db: Session, store_lc: str):
    """Reports and eBay's order call may number orders differently. If report-imported orders cover days the
    order call also returned but NONE of the numbers match, only orders after the last report day are added, so
    nothing is counted twice. When the numbers do match, everything is added and a report simply overwrites."""
    mx = db.execute(
        text("select max(sale_date) from analytics_ebay_orders where store = :s and source_file is distinct from :a"),
        {"s": store_lc, "a": API_SOURCE},
    ).scalar()
    if mx is None:
        return None
    overlap = db.execute(
        text(
            "select count(*) from ebay_orders o where lower(o.store) = :s "
            "and coalesce(o.created_at_ebay, o.paid_at)::date <= :mx"
        ),
        {"s": store_lc, "mx": mx},
    ).scalar()
    if not overlap:
        return None
    matches = db.execute(
        text(
            "select count(*) from ebay_orders o join analytics_ebay_orders x on x.store = lower(o.store) "
            "and x.order_number = o.ebay_order_id and x.source_file is distinct from :a where lower(o.store) = :s"
        ),
        {"s": store_lc, "a": API_SOURCE},
    ).scalar()
    return None if matches else mx


def feed_analytics(db: Session, store: str, order_pks: Optional[list] = None) -> int:
    """Copy this store's paid eBay orders into the analytics tables (all of them when order_pks is None).
    Rows that came from an uploaded report are never changed. Refunds are real amounts from eBay, and an order
    that was cancelled or fully refunded counts as no sale (net 0). Returns how many orders were written."""
    if not _analytics_ready(db):
        return 0
    s_lc = store.lower()
    boundary = _boundary(db, s_lc)
    where, params = "lower(o.store) = :s and o.paid_at is not null", {"s": s_lc}
    if order_pks is not None:
        if not order_pks:
            return 0
        where += " and o.id = any(cast(:ids as bigint[]))"
        params["ids"] = list(order_pks)
    orders = db.execute(
        text(
            "select o.id, o.ebay_order_id, o.paid_at, o.created_at_ebay, o.shipped_at_ebay, o.buyer_username, "
            "o.buyer_state, o.buyer_country, o.shipping_charged, o.total, o.cancelled, o.refund_total, "
            "o.shipping_service, o.tax_total from ebay_orders o where " + where
        ),
        params,
    ).all()
    if not orders:
        return 0
    lines: dict[int, list] = {}
    for r in db.execute(
        text(
            "select order_pk, ebay_item_id, ebay_transaction_id, custom_label, title, quantity, unit_price "
            "from ebay_order_lines where order_pk = any(cast(:ids as bigint[])) order by order_pk, id"
        ),
        {"ids": [o.id for o in orders]},
    ).all():
        lines.setdefault(r.order_pk, []).append(r)

    recs, items = [], []
    for o in orders:
        ls = lines.get(o.id) or []
        sale = (o.created_at_ebay or o.paid_at).date()
        if not ls or (boundary and sale <= boundary):
            continue
        subtotal = sum(((l.unit_price or Decimal(0)) * l.quantity for l in ls), Decimal(0))
        shipping = o.shipping_charged or Decimal(0)
        gross = subtotal + shipping
        before = o.total if o.total and o.total > 0 else gross + (o.tax_total or Decimal(0))
        tax = o.tax_total if o.tax_total and o.tax_total > 0 else max(before - gross, Decimal(0))
        refunded = min(o.refund_total or Decimal(0), before)
        paid_ex_tax = max(before - tax, Decimal(0))      # what the buyer actually paid, items + shipping, no tax
        # Refunds are eBay's real amounts, never guessed from a gap between totals. A coupon or discount lowers
        # net sales (the buyer paid less) but is not called a refund.
        if o.cancelled or (refunded > 0 and refunded >= before - Decimal("0.01")):
            net, refund, after = Decimal(0), gross, Decimal(0)           # not a sale at all
        elif refunded > 0:
            share = min(paid_ex_tax / before, Decimal(1)) if before > 0 else Decimal(1)   # the part of it that was not tax
            refund = min(refunded * share, paid_ex_tax)
            net, after = min(paid_ex_tax - refund, gross), before - refunded
        else:
            net, refund, after = min(paid_ex_tax, gross), Decimal(0), before
        shipped = o.shipped_at_ebay.date() if o.shipped_at_ebay else None
        recs.append(
            {
                "store": s_lc, "order_number": o.ebay_order_id[:40], "sale_date": sale, "paid": o.paid_at.date(),
                "shipped": shipped, "bu": o.buyer_username, "bs": o.buyer_state, "bc": o.buyer_country,
                "lc": len(ls), "units": sum(l.quantity for l in ls), "sub": subtotal.quantize(Decimal("0.01")),
                "ship": shipping, "tax": tax.quantize(Decimal("0.01")), "total": after.quantize(Decimal("0.01")),
                "gross": gross.quantize(Decimal("0.01")), "net": net.quantize(Decimal("0.01")),
                "refund": refund.quantize(Decimal("0.01")), "svc": o.shipping_service,
                "dts": (shipped - sale).days if shipped and shipped >= sale else None, "src": API_SOURCE,
            }
        )
        for i, l in enumerate(ls, start=1):
            items.append(
                {
                    "store": s_lc, "order_number": o.ebay_order_id[:40], "line_no": i, "item": l.ebay_item_id,
                    "title": (l.title or "")[:300] or None, "label": (l.custom_label or "")[:200] or None,
                    "txn": l.ebay_transaction_id, "qty": l.quantity, "price": l.unit_price or Decimal(0),
                }
            )
    if not recs:
        return 0
    db.execute(
        text(
            """
            insert into analytics_ebay_orders
              (store, order_number, sale_date, paid_on_date, shipped_on_date, buyer_username, buyer_state, buyer_country,
               line_count, units, items_subtotal, shipping_charged, tax_collected, total_price, gross_sales, net_sales,
               refund_est, shipping_service, days_to_ship, source_file, imported_at)
            values (:store, :order_number, :sale_date, :paid, :shipped, :bu, :bs, :bc, :lc, :units, :sub, :ship, :tax,
                    :total, :gross, :net, :refund, :svc, :dts, :src, now())
            on conflict (store, order_number) do update set
              sale_date = excluded.sale_date, paid_on_date = excluded.paid_on_date,
              shipped_on_date = excluded.shipped_on_date,
              buyer_username = coalesce(excluded.buyer_username, analytics_ebay_orders.buyer_username),
              buyer_state = coalesce(excluded.buyer_state, analytics_ebay_orders.buyer_state),
              buyer_country = coalesce(excluded.buyer_country, analytics_ebay_orders.buyer_country),
              line_count = excluded.line_count, units = excluded.units, items_subtotal = excluded.items_subtotal,
              shipping_charged = excluded.shipping_charged, tax_collected = excluded.tax_collected,
              total_price = excluded.total_price, gross_sales = excluded.gross_sales, net_sales = excluded.net_sales,
              refund_est = excluded.refund_est, shipping_service = excluded.shipping_service,
              days_to_ship = excluded.days_to_ship, imported_at = now()
            where analytics_ebay_orders.source_file = 'api'
            """
        ),
        recs,
    )
    numbers = [r["order_number"] for r in recs]
    owned = {
        r.order_number
        for r in db.execute(
            text(
                "select order_number from analytics_ebay_orders where store = :s and source_file = 'api' "
                "and order_number in :n"
            ).bindparams(bindparam("n", expanding=True)),
            {"s": s_lc, "n": numbers},
        ).all()
    }
    if owned:
        db.execute(
            text("delete from analytics_ebay_order_items where store = :s and order_number in :n").bindparams(
                bindparam("n", expanding=True)
            ),
            {"s": s_lc, "n": sorted(owned)},
        )
        mine = [i for i in items if i["order_number"] in owned]
        if mine:
            db.execute(
                text(
                    "insert into analytics_ebay_order_items (store, order_number, line_no, item_number, item_title, "
                    "custom_label, transaction_id, quantity, unit_price) values (:store, :order_number, :line_no, "
                    ":item, :title, :label, :txn, :qty, :price) on conflict do nothing"
                ),
                mine,
            )
    db.commit()
    return len(owned)


# --------------------------------------------------------------------------
# scanning a part out for an eBay order (called from the Ship screen)
# --------------------------------------------------------------------------
def _take_line(db: Session, line, pid: int, take: int, email: str, ref: str) -> None:
    lock_products(db, [pid])
    res = db.execute(
        text("select id, quantity from stock_reservations where ebay_order_line_id = :l and status = 'reserved' for update"),
        {"l": line.id},
    ).first()
    if not res:
        return
    take = min(take, res.quantity)
    before = db.execute(text("select coalesce(quantity_on_hand, 0) from products where id = :id"), {"id": pid}).scalar()
    after = db.execute(
        text(
            "update products set quantity_on_hand = greatest(0, coalesce(quantity_on_hand, 0) - :t), updated_at = now() "
            "where id = :id returning quantity_on_hand"
        ),
        {"t": take, "id": pid},
    ).scalar()
    if after != before:
        tid = db.execute(
            text(
                "insert into inventory_transactions (product_id, transaction_type, quantity_change, reference, legacy_created_by) "
                "values (:p, 'Adjustment', :d, :ref, :by) returning id"
            ),
            {"p": pid, "d": after - before, "ref": f"Picked for eBay order {ref}", "by": email},
        ).scalar()
        if line.sales_transaction_id:
            db.execute(
                text("update sales_transactions set inventory_transaction_id = coalesce(inventory_transaction_id, :t) where id = :s"),
                {"t": tid, "s": line.sales_transaction_id},
            )
    if res.quantity - take > 0:
        db.execute(text("update stock_reservations set quantity = quantity - :t where id = :id"), {"t": take, "id": res.id})
    else:
        db.execute(text("delete from stock_reservations where id = :id"), {"id": res.id})
        db.execute(text("update ebay_order_lines set state = 'picked', picked_at = now() where id = :l"), {"l": line.id})


def take_stock_for_reference(db: Session, reference: str, merged: dict, email: str) -> list[int]:
    """The parts in this scan-out leave the shelf for the eBay order with this number. Parts that are not
    on a reserved line of that order are ignored (the shipment is still recorded by the caller).
    Returns the line ids that were touched. Does not commit."""
    touched: list[int] = []
    for pid, m in merged.items():
        left = int(m["qty"])
        lines = db.execute(
            text(
                "select id, quantity, sales_transaction_id, ebay_order_id from ebay_order_lines "
                "where lower(ebay_order_id) = lower(:r) and product_id = :p and state = 'reserved' order by id"
            ),
            {"r": reference.strip(), "p": pid},
        ).all()
        for ln in lines:
            if left <= 0:
                break
            res_qty = db.execute(
                text("select coalesce(sum(quantity), 0) from stock_reservations where ebay_order_line_id = :l and status = 'reserved'"),
                {"l": ln.id},
            ).scalar()
            take = min(left, int(res_qty))
            if take > 0:
                _take_line(db, ln, pid, take, email, ln.ebay_order_id)
                left -= take
                touched.append(ln.id)
    return touched


# --------------------------------------------------------------------------
# admin screens: settings, sync now, the list of sales
# --------------------------------------------------------------------------
@router.get("/settings")
def get_settings_(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            """
            select a.store, a.display_name, (a.refresh_token_enc is not null) as authorized, a.live_pushes_enabled,
                   a.sales_polling_enabled, a.sales_poll_minutes, a.sales_last_polled_at, a.sales_last_ok_at,
                   a.sales_last_summary, a.sales_last_error, a.sales_qty_push_enabled,
                   (a.sales_lock_at is not null and a.sales_lock_at > now() - interval '10 minutes') as running,
                   (select count(*) from ebay_qty_queue q where q.store = a.store and q.status = 'pending') as qty_pending,
                   (select count(*) from ebay_qty_queue q where q.store = a.store and q.status = 'failed') as qty_failed,
                   (select count(*) from ebay_order_lines l where l.store = a.store and l.state = 'reserved') as set_aside,
                   (select count(*) from ebay_order_lines l where l.store = a.store and l.state = 'unmatched') as unmatched
            from ebay_accounts a order by a.store
            """
        )
    ).all()
    return {"stores": [dict(r._mapping) for r in rows]}


class SettingsIn(BaseModel):
    store: str
    sales_polling_enabled: Optional[bool] = None
    sales_poll_minutes: Optional[int] = Field(None, ge=1, le=1440)
    sales_qty_push_enabled: Optional[bool] = None


@router.put("/settings")
def put_settings(body: SettingsIn, db: Session = Depends(get_db)):
    a = db.execute(
        text("select store, (refresh_token_enc is not null) as authorized, live_pushes_enabled from ebay_accounts where lower(store) = lower(:s)"),
        {"s": body.store.strip()},
    ).first()
    if not a:
        raise HTTPException(404, "That store is not in the list.")
    sets, params = [], {"s": a.store}
    if body.sales_polling_enabled is not None:
        if body.sales_polling_enabled and not a.authorized:
            raise HTTPException(409, "Connect this store to eBay first.")
        sets.append("sales_polling_enabled = :pe")
        params["pe"] = body.sales_polling_enabled
    if body.sales_poll_minutes is not None:
        sets.append("sales_poll_minutes = :pm")
        params["pm"] = body.sales_poll_minutes
    if body.sales_qty_push_enabled is not None:
        if body.sales_qty_push_enabled and not a.live_pushes_enabled:
            raise HTTPException(409, "Turn Live pushes ON for this store first (it is the master switch for changing eBay).")
        sets.append("sales_qty_push_enabled = :qp")
        params["qp"] = body.sales_qty_push_enabled
    if not sets:
        return {"ok": True}
    db.execute(text(f"update ebay_accounts set {', '.join(sets)}, updated_at = now() where store = :s"), params)
    db.commit()
    return {"ok": True}


class PollIn(BaseModel):
    store: str
    days: Optional[int] = Field(None, ge=1, le=MAX_WINDOW_DAYS)


@router.post("/poll-now")
def poll_now(body: PollIn, db: Session = Depends(get_db)):
    a = db.execute(text("select store from ebay_accounts where lower(store) = lower(:s)"), {"s": body.store.strip()}).first()
    if not a:
        raise HTTPException(404, "That store is not in the list.")
    return poll_store(db, a.store, body.days)


class StoreIn(BaseModel):
    store: str


@router.post("/queue/run")
def queue_run(body: StoreIn, db: Session = Depends(get_db)):
    a = db.execute(text("select store from ebay_accounts where lower(store) = lower(:s)"), {"s": body.store.strip()}).first()
    if not a:
        raise HTTPException(404, "That store is not in the list.")
    return run_queue(db, a.store, QTY_PER_RUN)


@router.post("/feed-analytics")
def feed_analytics_now(body: StoreIn, db: Session = Depends(get_db)):
    """Send EVERY saved paid order of this store to the analytics tables again (uploaded-report rows are never changed)."""
    a = db.execute(text("select store from ebay_accounts where lower(store) = lower(:s)"), {"s": body.store.strip()}).first()
    if not a:
        raise HTTPException(404, "That store is not in the list.")
    if not _analytics_ready(db):
        raise HTTPException(409, "The analytics tables don't exist yet. Run analytics_ebay_tables.sql first.")
    try:
        return {"ok": True, "sent": feed_analytics(db, a.store, None)}
    except Exception:
        db.rollback()
        log.exception("sending %s orders to analytics failed", a.store)
        raise HTTPException(500, "That did not work and nothing was changed.")


_LINE_SORTS = {
    "date": "coalesce(l.sale_date, l.created_at)",
    "store": "l.store",
    "order": "l.ebay_order_id",
    "item": "l.ebay_item_id",
    "title": "coalesce(l.title, p.title)",
    "qty": "l.quantity",
    "price": "l.unit_price",
    "state": "l.state",
}


@router.get("/lines")
def lines(
    store: str = "",
    state: str = "",
    q: str = "",
    sort: str = "date",
    dir: str = "desc",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    where, params = ["true"], {"lim": limit, "off": offset}
    if store.strip():
        where.append("l.store = :store")
        params["store"] = store.strip()
    if state.strip():
        where.append("l.state = :state")
        params["state"] = state.strip()
    if q.strip():
        where.append("(l.ebay_order_id ilike :q or l.ebay_item_id ilike :q or l.title ilike :q or p.title ilike :q)")
        params["q"] = f"%{q.strip()}%"
    col = _LINE_SORTS.get(sort, _LINE_SORTS["date"])
    direction = "asc" if dir == "asc" else "desc"
    w = " and ".join(where)
    total = db.execute(
        text(f"select count(*) from ebay_order_lines l left join products p on p.id = l.product_id where {w}"), params
    ).scalar()
    rows = db.execute(
        text(
            f"""
            select l.id, l.store, l.ebay_order_id, l.ebay_item_id, coalesce(l.title, p.title) as title, l.quantity,
                   l.unit_price, l.state, l.product_id, p.sku_display as sku, coalesce(l.sale_date, l.created_at) as sale_date,
                   o.cancelled, o.refund_total
            from ebay_order_lines l
            join ebay_orders o on o.id = l.order_pk
            left join products p on p.id = l.product_id
            where {w}
            order by {col} {direction} nulls last, l.id desc
            limit :lim offset :off
            """
        ),
        params,
    ).all()
    return {"total": total, "items": [dict(r._mapping) for r in rows]}


@router.post("/lines/{line_id}/pick")
def pick_line(line_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Take this line's parts off the shelf now, without scanning (the part has left the building)."""
    ln = db.execute(
        text("select id, product_id, quantity, sales_transaction_id, ebay_order_id, state from ebay_order_lines where id = :l"),
        {"l": line_id},
    ).first()
    if not ln or ln.state != "reserved" or not ln.product_id:
        raise HTTPException(409, "Only a line whose parts are set aside can be marked picked.")
    try:
        _take_line(db, ln, ln.product_id, ln.quantity, user.email, ln.ebay_order_id)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "That did not work and nothing was changed.")
    return {"ok": True}


@router.post("/lines/{line_id}/release")
def release_line(line_id: int, db: Session = Depends(get_db)):
    """Stop setting these parts aside WITHOUT changing the shelf count (use when the sale should not hold stock)."""
    ln = db.execute(
        text("select id, product_id, state from ebay_order_lines where id = :l for update"), {"l": line_id}
    ).first()
    if not ln or ln.state != "reserved":
        raise HTTPException(409, "Only a line whose parts are set aside can be released.")
    db.execute(text("delete from stock_reservations where ebay_order_line_id = :l"), {"l": line_id})
    db.execute(text("update ebay_order_lines set state = 'history' where id = :l"), {"l": line_id})
    if ln.product_id:
        enqueue_product(db, ln.product_id)
    db.commit()
    return {"ok": True}
