"""Large-order protection, step 1: the URGENT status.

Shown on Inventory > Pick & ship > Online sales (website and eBay orders together). An order is URGENT when a paid order
cannot be filled from our own shelf and either

  * the distributor cannot fill the rest either (no distributor link, stale or missing distributor stock, or fewer units than
    the order needs), or
  * the rest is a big share of what the distributor has (so a few dealers ordering at the same moment could leave us short).

Urgent orders are pinned to the top of the list in red until a person decides: approve a drop ship, or ship from our own stock.
That decision is recorded (who and when) and the order goes back to its normal place.

THIS STEP RECORDS THE DECISION ONLY. It does not place the distributor order.

How the numbers are worked out
  * Our shelf: paid orders for a part are served oldest first (website and eBay together) against stock on hand, the same
    rule as stock.order_shortfalls. An order is "short" by the units that come after the shelf runs out.
  * Distributor stock: Indiana + Nevada from the catalog feed (Maine is decommissioned and never counted). A product made of
    several distributor parts (product_kit_parts) is only as available as its scarcest part (quantity / qty per kit). If any
    part has no catalog match, is inactive, or its feed row is older than FULFILL_FEED_MAX_HOURS, the product counts as ZERO at
    the distributor: no information means the decision rests on our own stock.

Settings (environment variables, so the numbers are easy to change):
  FULFILL_URGENT_PCT        default 50   urgent when the units we would need from the distributor are at least this percent of what it shows
  FULFILL_URGENT_MIN_UNITS  default 5    ...and at least this many units (so one or two units never raise the alarm on their own)
  FULFILL_FEED_MAX_HOURS    default 48   distributor stock older than this counts as unknown
"""
import logging
import os
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

DECISIONS = ("dropship", "stock")
_table_ready = False


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def key_of(channel: str, order_ref: str) -> str:
    return f"{channel}|{order_ref}"


def ensure_table(db: Session) -> None:
    global _table_ready
    if _table_ready:
        return
    db.execute(
        text(
            """
            create table if not exists order_fulfillment_decisions (
                channel text not null,
                order_ref text not null,
                decision text not null,
                decided_by text not null,
                decided_at timestamptz not null default now(),
                reasons text,
                primary key (channel, order_ref)
            )
            """
        )
    )
    db.commit()
    _table_ready = True


def distributor_availability(db: Session, product_ids) -> dict[int, dict]:
    """{product_id: {available, detail, unknown}} for products that have distributor parts. A product with no parts is
    simply absent (it has no distributor link)."""
    ids = sorted({int(i) for i in product_ids if i is not None})
    out: dict[int, dict] = {}
    if not ids:
        return out
    max_h = _env_num("FULFILL_FEED_MAX_HOURS", 48)
    rows = db.execute(
        text(
            """
            select k.product_id, k.part_number, coalesce(k.quantity, 1) as per_kit,
                   s.id as sid, s.active,
                   coalesce(s.qty_indiana, 0) as indiana, coalesce(s.qty_nevada, 0) as nevada,
                   extract(epoch from (now() - s.last_seen_at)) / 3600.0 as age_h
            from product_kit_parts k
            left join supplier_items s on s.id = k.supplier_item_id
            where k.product_id = any(cast(:ids as bigint[]))
            order by k.product_id, k.sort_order
            """
        ),
        {"ids": ids},
    ).all()
    by: dict[int, list] = {}
    for r in rows:
        by.setdefault(r.product_id, []).append(r)
    for pid, parts in by.items():
        kits: list[int] = []
        unknown: list[str] = []
        for r in parts:
            per = max(1, int(r.per_kit or 1))
            if r.sid is None or r.active is False or r.age_h is None or float(r.age_h) > max_h:
                unknown.append(str(r.part_number))
                kits.append(0)
                continue
            kits.append((int(r.indiana) + int(r.nevada)) // per)
        avail = min(kits) if kits else 0
        if unknown:
            detail = "the distributor's stock is unknown for part " + ", ".join(unknown)
        elif len(parts) == 1:
            r = parts[0]
            detail = f"the distributor shows {avail} (Indiana {int(r.indiana)}, Nevada {int(r.nevada)})"
        else:
            detail = f"the distributor can supply {avail} complete kit{'s' if avail != 1 else ''} (limited by the scarcest part)"
        out[pid] = {"available": avail, "detail": detail, "unknown": bool(unknown)}
    return out


def shortages(db: Session) -> list:
    """Every paid, not-yet-picked reservation (website and eBay together) that the shelf cannot cover, oldest order first
    getting the shelf. One row per order and part with the units it is short."""
    return db.execute(
        text(
            """
            with res as (
              select r.id as rid, r.product_id, r.quantity, 'Website'::text as channel, o.order_number as order_ref,
                     coalesce(o.paid_at, now()) as placed_at
              from stock_reservations r join orders o on o.id = r.order_id
              where r.status = 'reserved' and r.ebay_order_line_id is null
              union all
              select r.id, r.product_id, r.quantity, 'eBay'::text, l.ebay_order_id,
                     coalesce(l.sale_date, l.created_at)
              from stock_reservations r join ebay_order_lines l on l.id = r.ebay_order_line_id
              where r.status = 'reserved'
            ), run as (
              select res.*, sum(res.quantity) over (partition by res.product_id order by res.placed_at, res.rid) as running
              from res
            )
            select run.channel, run.order_ref, run.product_id, p.sku_display as sku,
                   run.quantity::int as ordered,
                   greatest(0, least(run.quantity, run.running - coalesce(p.quantity_on_hand, 0)))::int as short
            from run join products p on p.id = run.product_id
            where run.running - coalesce(p.quantity_on_hand, 0) > 0
            order by run.channel, run.order_ref, run.product_id
            """
        )
    ).all()


def ebay_line_dropship_flags(db: Session, order_ids) -> dict[tuple[str, int], bool]:
    """{(eBay order id, product id): True when the listing the buyer BOUGHT FROM is set to Drop ship}. The listing's own
    checkbox counts, and so does a Drop ship tick on the product itself (which means every listing). A flag on some OTHER
    listing of the same product does not count. A line whose listing cannot be found falls back to the product's own flag."""
    ids = sorted({str(o) for o in order_ids if o})
    if not ids:
        return {}
    rows = db.execute(
        text(
            """
            select l.ebay_order_id, l.product_id,
                   bool_or(coalesce(p.dropship_ok, false) or coalesce(cl.dropship_ok, false)) as flagged
            from ebay_order_lines l
            join products p on p.id = l.product_id
            left join channel_listings cl on cl.product_id = l.product_id
                 and lower(coalesce(cl.store, '')) = lower(coalesce(l.store, ''))
                 and regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g')
                   = regexp_replace(cast(l.ebay_item_id as text), '[^0-9]', '', 'g')
            where l.ebay_order_id = any(cast(:o as text[])) and l.product_id is not null
            group by l.ebay_order_id, l.product_id
            """
        ),
        {"o": ids},
    ).all()
    return {(r.ebay_order_id, int(r.product_id)): bool(r.flagged) for r in rows}


def urgent_orders(db: Session) -> dict[str, list[str]]:
    """{ 'Channel|order ref': [reason, ...] } for every open order that needs a human decision and has not had one."""
    ensure_table(db)
    short = shortages(db)
    if not short:
        return {}
    avail = distributor_availability(db, [r.product_id for r in short])
    pct = _env_num("FULFILL_URGENT_PCT", 50)
    min_units = _env_num("FULFILL_URGENT_MIN_UNITS", 5)
    found: dict[str, list[str]] = {}
    try:
        flags = ebay_line_dropship_flags(db, [r.order_ref for r in short if r.channel == "eBay"])
    except Exception:
        db.rollback()
        flags = {}
    for r in short:
        d = avail.get(r.product_id)
        have = d["available"] if d else 0
        detail = d["detail"] if d else "no distributor is linked to this product"
        covered = r.ordered - r.short
        unflagged = r.channel == "eBay" and flags.get((r.order_ref, int(r.product_id))) is False
        note = (
            f"{r.sku}: short on our shelf (by {r.short}), and the listing it was bought from is not set to Drop ship, so no "
            f"drop ship order is made by itself; {detail}. Decide whether to drop ship it or ship it from our own stock."
        )
        if have < r.short:
            reason = (
                f"{r.sku}: {r.ordered} ordered, our shelf covers {covered}, {detail}. "
                f"Nothing on hand can fill the other {r.short}."
            )
        elif r.short >= min_units and have > 0 and r.short * 100.0 >= pct * have:
            reason = (
                f"{r.sku}: {r.ordered} ordered, our shelf covers {covered}; the other {r.short} would come from the "
                f"distributor, and {detail} (that is {round(r.short * 100.0 / have)}% of it)."
            )
        elif unflagged:
            reason = note
        else:
            continue
        if unflagged and reason is not note:
            found.setdefault(key_of(r.channel, r.order_ref), []).append(note)
        found.setdefault(key_of(r.channel, r.order_ref), []).append(reason)
    if not found:
        return {}
    decided = {
        key_of(x.channel, x.order_ref)
        for x in db.execute(
            text(
                "select channel, order_ref from order_fulfillment_decisions "
                "where (channel || '|' || order_ref) = any(cast(:k as text[]))"
            ),
            {"k": list(found)},
        ).all()
    }
    return {k: v for k, v in found.items() if k not in decided}


def decisions_for(db: Session, keys: list[str]) -> dict[str, dict]:
    ensure_table(db)
    if not keys:
        return {}
    rows = db.execute(
        text(
            "select channel, order_ref, decision, decided_by, decided_at from order_fulfillment_decisions "
            "where (channel || '|' || order_ref) = any(cast(:k as text[]))"
        ),
        {"k": keys},
    ).all()
    return {
        key_of(r.channel, r.order_ref): {"decision": r.decision, "by": r.decided_by.split("@")[0], "at": r.decided_at}
        for r in rows
    }


def record_decision(db: Session, channel: str, order_ref: str, decision: str, who: str, reasons: Optional[list[str]]) -> None:
    ensure_table(db)
    db.execute(
        text(
            "insert into order_fulfillment_decisions (channel, order_ref, decision, decided_by, decided_at, reasons) "
            "values (:c, :o, :d, :w, now(), :r) "
            "on conflict (channel, order_ref) do update set decision = excluded.decision, "
            "decided_by = excluded.decided_by, decided_at = now(), reasons = coalesce(excluded.reasons, order_fulfillment_decisions.reasons)"
        ),
        {"c": channel, "o": order_ref, "d": decision, "w": who, "r": " | ".join(reasons) if reasons else None},
    )
    db.commit()
