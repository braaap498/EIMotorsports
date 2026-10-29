"""
Stock rules shared by the storefront (checkout, cart, product page) and the warehouse screens.

THE THREE NUMBERS
  quantity_on_hand   what is physically on the shelf. Only a committed pick, a receive, a manual
                     adjustment or an order cancellation changes it.
  set aside          parts promised to orders (table stock_reservations):
                       'hold'      a customer is in checkout; it expires by itself (CHECKOUT_MINUTES)
                       'reserved'  the order is paid and waiting to be picked
  available          quantity_on_hand minus everything set aside, never below 0. This is the only
                     number customers ever see. They never see a negative or an "oversold" message,
                     just in stock / only N left / sold out (or Backordered when on_order is set).

Every change to a product's reservations happens while that product's row is locked
(select ... for update, always in id order), so two checkouts can never both take the last unit.
"""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

# Stripe refuses a checkout session that lasts under 30 minutes, so the hold is a little longer.
CHECKOUT_MINUTES = 32
# The hold row outlives the Stripe session slightly, so a payment that lands in the last seconds
# (webhooks can arrive late) still finds its parts set aside.
HOLD_GRACE_MINUTES = 5


def _live(alias: str = "r") -> str:
    """A reservation that currently counts: paid, or a checkout hold that has not expired."""
    return f"({alias}.status = 'reserved' or ({alias}.status = 'hold' and {alias}.expires_at > now()))"


def set_aside_sql(p: str = "p") -> str:
    """SQL: units of product `p` promised to paid orders or open checkouts."""
    return (
        "coalesce((select sum(r.quantity) from stock_reservations r "
        f"where r.product_id = {p}.id and {_live('r')}), 0)"
    )


def reserved_paid_sql(p: str = "p") -> str:
    """SQL: units of product `p` promised to PAID orders that have not been picked yet."""
    return (
        "coalesce((select sum(r.quantity) from stock_reservations r "
        f"where r.product_id = {p}.id and r.status = 'reserved'), 0)"
    )


def available_sql(p: str = "p") -> str:
    """SQL: what customers can still buy. Never negative."""
    return f"greatest(0, coalesce({p}.quantity_on_hand, 0) - {set_aside_sql(p)})"


def lock_products(db: Session, ids) -> dict:
    """Lock these product rows (in id order, so two people can't deadlock). Returns {id: row}."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return {}
    rows = db.execute(
        text(
            "select id, sku_display as sku, quantity_on_hand from products "
            "where id = any(cast(:ids as bigint[])) order by id for update"
        ),
        {"ids": ids},
    ).all()
    return {r.id: r for r in rows}


def place_holds(db: Session, order_id: int, wanted: dict[int, int]) -> None:
    """Set parts aside for a customer who is about to pay. Raises 409 if any is no longer available.

    Call this inside the transaction that creates the order, before committing it.
    """
    locked = lock_products(db, wanted)
    ids = list(wanted)
    # Tidy up checkouts that were abandoned (their hold ran out) for these parts.
    db.execute(
        text(
            "delete from stock_reservations "
            "where status = 'hold' and expires_at <= now() and product_id = any(cast(:ids as bigint[]))"
        ),
        {"ids": ids},
    )
    unavailable = False
    for pid, qty in wanted.items():
        row = locked.get(pid)
        if row is None:
            unavailable = True
            break
        taken = db.execute(
            text(
                "select coalesce(sum(r.quantity), 0) from stock_reservations r "
                f"where r.product_id = :p and {_live('r')}"
            ),
            {"p": pid},
        ).scalar()
        if qty > max(0, (row.quantity_on_hand or 0) - int(taken)):
            unavailable = True
            break
    if unavailable:
        db.rollback()
        raise HTTPException(
            409,
            "Some items in your cart just sold out or are being held by another checkout. "
            "Please go back to your cart to review it.",
        )
    for pid, qty in wanted.items():
        db.execute(
            text(
                "insert into stock_reservations (order_id, product_id, quantity, status, expires_at) "
                "values (:o, :p, :q, 'hold', now() + (cast(:m as integer) * interval '1 minute'))"
            ),
            {"o": order_id, "p": pid, "q": qty, "m": CHECKOUT_MINUTES + HOLD_GRACE_MINUTES},
        )


def release_holds(db: Session, order_id: int) -> None:
    """Let go of a checkout's parts (payment failed, session expired, customer started over)."""
    db.execute(text("delete from stock_reservations where order_id = :o and status = 'hold'"), {"o": order_id})


def confirm_holds(db: Session, order_id: int) -> None:
    """Payment arrived: turn the order's holds into firm reservations.

    Rebuilt from the order's own items, so it also works when the hold had already been tidied
    up, and when the order was started before reservations existed. A customer who paid always
    gets their order; if the shelf can't cover it, the order is flagged red in the warehouse.
    Running it twice gives the same result.
    """
    items = db.execute(
        text(
            "select product_id, sum(quantity) as q from order_items "
            "where order_id = :o and product_id is not null group by product_id"
        ),
        {"o": order_id},
    ).all()
    lock_products(db, [i.product_id for i in items])
    db.execute(text("delete from stock_reservations where order_id = :o"), {"o": order_id})
    for i in items:
        db.execute(
            text(
                "insert into stock_reservations (order_id, product_id, quantity, status, expires_at) "
                "values (:o, :p, :q, 'reserved', null)"
            ),
            {"o": order_id, "p": i.product_id, "q": int(i.q)},
        )
    db.execute(text("update orders set reserved_stock = true where id = :o"), {"o": order_id})


def order_shortfalls(db: Session, order_ids) -> list:
    """Paid, un-picked orders whose parts the shelf can't cover yet.

    Paid orders for a part are served oldest first: the earlier ones keep their parts and only the
    order(s) that push the total past what is on the shelf come up short. Each row has product_id,
    short (units this order is short), on_order (a purchase order is placed), expected_date and
    days_until (days from today, when an arrival date was entered). Customers see only
    "backordered" and the date; the numbers here are never sent to the browser as stock counts.
    """
    ids = [int(i) for i in order_ids]
    if not ids:
        return []
    return db.execute(
        text(
            """
            with r as (
              select s.order_id, s.product_id, s.quantity,
                     sum(s.quantity) over (partition by s.product_id order by o.paid_at nulls last, o.id) as running
              from stock_reservations s join orders o on o.id = s.order_id
              where s.status = 'reserved'
            )
            select r.order_id, r.product_id,
                   greatest(0, least(r.quantity, r.running - coalesce(p.quantity_on_hand, 0)))::int as short,
                   coalesce(p.on_order, false) as on_order,
                   p.expected_date,
                   case when p.expected_date is null then null else (p.expected_date - current_date) end as days_until
            from r join products p on p.id = r.product_id
            where r.order_id = any(cast(:ids as bigint[]))
              and r.running - coalesce(p.quantity_on_hand, 0) > 0
            """
        ),
        {"ids": ids},
    ).all()
