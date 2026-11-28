"""
Automatic drop shipping, step 1: an eBay order that has to be drop shipped makes its purchase order by itself,
then WAITS for a person to look at the PO and confirm it before anything goes further.

Chain. The switch "Manual checks" (Drop ship page, default OFF) decides whether each step has a temporary review stop:
  OFF = everything below runs by itself, one step after another. ON = it stops at the three review steps (2, 4, 6) for a person.
  1. eBay order paid        -> drop ship PO made (buyer name, address, and the shipping service the buyer picked)
  2. review_po   a person checks the PO              <- BUILT (this file)
  3. quote       confirming the PO gets a quote from Automatic Distributors; the shipping option closest to the buyer's choice is picked
  4. review_quote a person checks the quote (and can change the shipping option), then places the order (owner/admin only)
  5. place       the order is placed through the order API (the existing safeguards still apply: test/live server, live switch)
  6. review_tracking tracking comes back (existing automatic check); a person checks it, then eBay is told it shipped, with the
                  tracking number, and eBay notifies the buyer

WEBSITE ORDERS use the same chain. A paid website order (Stripe) whose products are ALL ticked Drop ship ON THE WEBSITE (products.web_dropship_ok; eBay ticks are separate) makes its PO by itself;
the job is kept under the key "web:<order number>" (dropship_jobs.channel = 'web'). When the distributor's tracking comes back the
website order is marked shipped with it (the customer sees it on Track order). An order that mixes drop ship products with shelf
products is NOT automated: it is flagged "MIXED ORDER" and waits for a person.

A listing ticked Drop ship is drop shipped automatically while the distributor can fill the line (and only a product whose parts are all
matched to the distributor catalog can be ticked). If the distributor is out and our shelf covers the line, it stays an order we fill ourselves. An unticked listing that is short on our shelf asks a person on Pick & ship instead.
"""
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import ADMIN_ROLES, AuthUser, needs, require_route_access
from app.availability import line_units_sql
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/dropship", tags=["admin-dropship"], dependencies=[Depends(require_route_access)])

SYSTEM_USER = AuthUser(user_id="system", email="Automatic drop ship", role="admin", permissions=frozenset())
PERMS = ("inventory.purchase", "inventory.ship")
RECENT_DAYS = 10

_ready = False


def _ensure(db: Session) -> None:
    global _ready
    if _ready:
        return
    for stmt in (
        """create table if not exists dropship_jobs (
             id bigserial primary key,
             ebay_order_id text not null unique,
             store text,
             po_id bigint,
             po_number bigint,
             stage text not null default 'review_po',
             ship_service text,
             buyer_name text,
             problem text,
             created_by text,
             created_at timestamptz not null default now(),
             updated_at timestamptz not null default now(),
             confirmed_by text,
             confirmed_at timestamptz
           )""",
        """create table if not exists dropship_settings (
             id int primary key default 1,
             auto_po boolean not null default false,
             last_scan_at timestamptz,
             last_scan_note text
           )""",
        "insert into dropship_settings (id) values (1) on conflict (id) do nothing",
        "alter table dropship_jobs add column if not exists quote text",
        "alter table dropship_jobs add column if not exists ship_method text",
        "alter table dropship_jobs add column if not exists supplier_order text",
        "alter table dropship_jobs add column if not exists supplier_mode text",
        "alter table dropship_jobs add column if not exists tracking_number text",
        "alter table dropship_jobs add column if not exists tracking_carrier text",
        "alter table dropship_settings add column if not exists auto_ship boolean not null default false",
        "alter table dropship_settings add column if not exists manual_checks boolean not null default false",
        "alter table dropship_jobs add column if not exists tries integer not null default 0",
        "alter table dropship_jobs add column if not exists note text",
        "alter table dropship_jobs add column if not exists placed_at timestamptz",
        "alter table dropship_jobs add column if not exists shipped_at timestamptz",
        "alter table dropship_jobs add column if not exists channel text not null default 'ebay'",
        "alter table dropship_jobs add column if not exists web_order_id bigint",
        "alter table dropship_jobs add column if not exists manual_ship boolean not null default false",
        "create index if not exists dropship_jobs_channel_idx on dropship_jobs (channel, stage)",
        "create index if not exists dropship_jobs_created_idx on dropship_jobs (created_at)",
        "create index if not exists dropship_jobs_placed_idx on dropship_jobs (placed_at)",
        "create index if not exists dropship_jobs_shipped_idx on dropship_jobs (shipped_at)",
        "create index if not exists dropship_jobs_stage_idx on dropship_jobs (stage)",
    ):
        db.execute(text(stmt))
    db.commit()
    _ready = True
    _backfill_line_states(db)


def _backfill_line_states(db: Session) -> None:
    """Once per start: eBay order lines of orders that already have a drop ship PO move from Set aside to Drop ship (only lines still
    set aside are touched, so this does nothing once they have moved)."""
    try:
        from app import ebay_sales

        rows = db.execute(
            text(
                "select j.ebay_order_id, array_agg(distinct pl.product_id) as pids from dropship_jobs j "
                "join po_headers h on h.id = j.po_id and h.status <> 'cancelled' "
                "join po_lines pl on pl.po_id = h.id and pl.product_id is not null "
                "where j.stage not in ('dismissed', 'problem') group by j.ebay_order_id"
            )
        ).all()
        for r in rows:
            ebay_sales.mark_lines_dropship(db, r.ebay_order_id, list(r.pids or []))
    except Exception:
        db.rollback()
        log.exception("could not move existing drop ship orders' lines to the Drop ship state")


def _settings(db: Session) -> dict:
    _ensure(db)
    r = db.execute(text("select manual_checks, last_scan_at, last_scan_note from dropship_settings where id = 1")).first()
    return {"manual_checks": bool(r.manual_checks), "last_scan_at": r.last_scan_at, "last_scan_note": r.last_scan_note}


def _units_needed(db: Session, channel: str, order_ref: str, lines) -> dict[int, int]:
    """{product id: UNITS the order takes}: quantity bought x the listing's pieces per sale, summed over the order's lines of that product.
    The distributor's numbers and our shelf are counted in units, so a 5-pack line needs 5, not 1. If the lines cannot be read that way
    it falls back to the plain quantities."""
    try:
        with db.begin_nested():
            if channel == "amazon":
                rows = db.execute(
                    text(
                        "select l.product_id, coalesce(sum(l.quantity * coalesce(l.pieces_per_sale, 1)), 0) as u from amazon_order_lines l "
                        "where l.amazon_order_id = :o and l.product_id is not null group by l.product_id"
                    ),
                    {"o": order_ref},
                ).all()
            else:
                rows = db.execute(
                    text(
                        f"select l.product_id, coalesce(sum({line_units_sql('l')}), 0) as u from ebay_order_lines l "
                        "where l.ebay_order_id = :o and l.product_id is not null group by l.product_id"
                    ),
                    {"o": order_ref},
                ).all()
        got = {int(r.product_id): int(r.u) for r in rows if int(r.u or 0) > 0}
        if got:
            return got
    except Exception:
        log.exception("could not read the units of order %s; using the quantities bought", order_ref)
    out: dict[int, int] = {}
    for l in lines:
        if l.product_id is not None:
            out[int(l.product_id)] = out.get(int(l.product_id), 0) + int(l.quantity or 1)
    return out


def _flagged_short_lines(db: Session, order_id: str, honor_decision: bool = False) -> list[int]:
    """Products on this eBay order that are to be drop shipped: the listing the buyer bought from is ticked Drop ship (our shelf does
    not matter), and the product is matched to the distributor's catalog. A flag on another listing of the same product does not
    count. An UNticked listing is never made automatically: when it is short on our shelf Pick & ship asks a person. With
    honor_decision (the manual button only), an order a person approved as 'drop ship' there counts too, if it is short on our shelf."""
    from app import fulfillment
    from app.admin_purchase_orders import _ebay_order_and_lines
    from app.availability import has_distributor

    _o, lines = _ebay_order_and_lines(db, order_id)
    if not any(l.product_id is not None for l in lines):
        return []
    flags = fulfillment.ebay_line_dropship_flags(db, [_o.ebay_order_id])
    approved = False
    if honor_decision:
        approved = (fulfillment.decisions_for(db, [fulfillment.key_of("eBay", _o.ebay_order_id)]).get(fulfillment.key_of("eBay", _o.ebay_order_id)) or {}).get("decision") == "dropship"
    # A ticked listing is drop shipped only while the distributor can fill the line. When the distributor is out (or shows too few,
    # or its stock is unknown) and OUR shelf covers the line, it is not drop shipped: it stays a normal order we fill ourselves.
    avail = fulfillment.distributor_availability(db, [l.product_id for l in lines if l.product_id is not None])

    units = _units_needed(db, "ebay", _o.ebay_order_id, lines)

    def _ours_instead(l) -> bool:
        # DROP SHIP PAUSES for this line when the distributor cannot fill it: the line needs `need` UNITS (quantity x pieces per sale)
        # and the distributor's usable count (the better warehouse after the safety margin) is below that. When our own shelf covers it,
        # the line is filled from our shelf instead of using up the distributor's last units (the listing shows our shelf meanwhile).
        need = units.get(int(l.product_id), int(l.quantity or 1))
        a = avail.get(int(l.product_id))
        return (a is None or int(a.get("usable", a["available"])) < need) and int(l.on_hand or 0) >= need

    picks = {
        int(l.product_id)
        for l in lines
        if l.product_id is not None
        and (
            (flags.get((_o.ebay_order_id, int(l.product_id))) and not _ours_instead(l))
            or (approved and int(l.on_hand or 0) < int(l.quantity or 1))
        )
    }
    # a product without a matched distributor part cannot be drop shipped: it stays with Pick & ship
    return sorted(pid for pid in picks if has_distributor(db, pid))


def _make_job(db: Session, order_id: str, store: Optional[str], product_ids: list[int], user: AuthUser, manual: bool = False) -> dict:
    """Make the PO and record the job. A refusal (Global Shipping Program, incomplete address...) is recorded as 'problem'."""
    from app.admin_purchase_orders import EbayDropIn, po_from_ebay_order

    try:
        po = po_from_ebay_order(EbayDropIn(order=order_id, product_ids=product_ids), db=db, user=user)
    except HTTPException as e:
        db.rollback()
        msg = str(e.detail)[:400]
        db.execute(
            text(
                "insert into dropship_jobs (ebay_order_id, store, stage, problem, created_by) values (:o, :s, 'problem', :m, :u) "
                "on conflict (ebay_order_id) do nothing"
            ),
            {"o": order_id, "s": store, "m": msg, "u": user.email},
        )
        db.commit()
        return {"order": order_id, "problem": msg}
    db.execute(
        text(
            "insert into dropship_jobs (ebay_order_id, store, po_id, po_number, stage, ship_service, buyer_name, created_by, manual_ship) "
            "values (:o, :s, :pid, :pn, 'review_po', :svc, :bn, :u, :ms) on conflict (ebay_order_id) do nothing"
        ),
        {"o": order_id, "s": store, "pid": po["id"], "pn": po["po_number"], "svc": po.get("ship_service"), "bn": po.get("buyer_name"), "u": user.email, "ms": manual},
    )
    db.commit()
    return {"order": order_id, "po_number": po["po_number"]}


# --------------------------------------------------------------------------
# WEBSITE ORDERS. A paid website order whose products are all ticked Drop ship is ordered from the distributor by the same
# chain as an eBay order. Its job is keyed "web:<order number>" so it can never collide with an eBay order id.
# --------------------------------------------------------------------------
WEB_PREFIX = "web:"
WEB_CANCEL_PREFIX = "Website order cancelled"
AMAZON_PREFIX = "amazon:"   # an Amazon order's job key (dropship_jobs.channel = 'amazon'), so it can never collide with an eBay order id
AMAZON_CANCEL_PREFIX = "Amazon order cancelled"
_TRACK_OK = re.compile(r"^[A-Za-z0-9\-]{6,60}$")


def _is_web(key) -> bool:
    return str(key or "").startswith(WEB_PREFIX)


def _is_amazon(key) -> bool:
    return str(key or "").startswith(AMAZON_PREFIX)


def _web_ship_label(notes) -> Optional[str]:
    """The shipping choice the customer paid for (checkout writes it as 'Shipping: <label>' in the order notes)."""
    m = re.search(r"Shipping:\s*([^|]+)", str(notes or ""))
    return m.group(1).strip()[:80] if m else None


def _web_lines(db: Session, order_id: int) -> tuple[list[int], list[str]]:
    """(product ids to drop ship, names of the lines that are NOT drop ship) for a website order. A line is drop ship when its product
    has the website Drop ship tick (products.web_dropship_ok, separate from the eBay listing ticks) and is matched to the distributor's catalog; anything else (not ticked, unmatched, no product) stays on our shelf."""
    from app.availability import has_distributor

    rows = db.execute(
        text(
            "select oi.product_id, coalesce(p.sku_display, oi.part_number, oi.title, 'a part') as label, coalesce(p.web_dropship_ok, false) as ds "
            "from order_items oi left join products p on p.id = oi.product_id where oi.order_id = :o order by oi.id"
        ),
        {"o": order_id},
    ).all()
    drop: list[int] = []
    other: list[str] = []
    for r in rows:
        if r.product_id is not None and r.ds and has_distributor(db, int(r.product_id)):
            if int(r.product_id) not in drop:
                drop.append(int(r.product_id))
        else:
            other.append(str(r.label))
    return drop, other


def _web_problem(db: Session, number: str, order_id: int, msg: str, user: AuthUser) -> None:
    db.execute(
        text(
            "insert into dropship_jobs (ebay_order_id, store, stage, problem, created_by, channel, web_order_id) "
            "values (:k, 'Website', 'problem', :m, :u, 'web', :w) on conflict (ebay_order_id) do nothing"
        ),
        {"k": WEB_PREFIX + number, "m": msg[:400], "u": user.email, "w": order_id},
    )
    db.commit()


def _release_web_holds(db: Session, order_id: int, pids: list[int]) -> None:
    """The distributor fills these lines, not our shelf: drop the shelf hold (the shelf count itself is NOT changed)."""
    db.execute(
        text("delete from stock_reservations where order_id = :o and product_id = any(cast(:p as bigint[]))"),
        {"o": order_id, "p": list(pids)},
    )
    try:
        from app.ebay_sales import queue_stock_push

        queue_stock_push(db, list(pids))
    except Exception:
        log.exception("could not queue quantity updates after drop shipping website order lines")
    db.commit()


def _make_web_job(db: Session, o, pids: list[int], user: AuthUser, manual: bool = False) -> dict:
    """Make the PO for a website order and record the job. A refusal (incomplete address, no distributor...) is recorded as 'problem'."""
    from app.admin_purchase_orders import FromWebOrderIn, po_from_web_order

    key = WEB_PREFIX + o.order_number
    try:
        po = po_from_web_order(FromWebOrderIn(order_number=o.order_number, product_ids=pids), db=db, user=user)
    except HTTPException as e:
        db.rollback()
        msg = str(e.detail)[:400]
        _web_problem(db, o.order_number, o.id, msg, user)
        return {"order": key, "problem": msg}
    db.execute(
        text(
            "insert into dropship_jobs (ebay_order_id, store, po_id, po_number, stage, ship_service, buyer_name, created_by, channel, web_order_id, manual_ship) "
            "values (:k, 'Website', :pid, :pn, 'review_po', :svc, :bn, :u, 'web', :w, :ms) on conflict (ebay_order_id) do nothing"
        ),
        {"k": key, "pid": po["id"], "pn": po["po_number"], "svc": _web_ship_label(o.notes), "bn": o.customer_name, "u": user.email, "w": o.id, "ms": manual},
    )
    db.commit()
    try:
        _release_web_holds(db, o.id, pids)
    except Exception:
        db.rollback()
        log.exception("drop ship PO made for website order %s, but its shelf holds could not be released", o.order_number)
    return {"order": key, "po_number": po["po_number"]}


def scan_web_orders(db: Session, user: AuthUser = SYSTEM_USER) -> dict:
    """Paid website orders that contain a product ticked Drop ship and have no job yet. All lines drop ship -> PO and job. A mix of
    drop ship and shelf lines -> a 'problem' job saying so (nothing is ordered). Skipped: an order a person already decided to ship from
    our own stock, one that already has a live drop ship PO (made by hand), and one staff already started picking."""
    from app import fulfillment

    _ensure(db)
    rows = db.execute(
        text(
            "select o.id, o.order_number, o.customer_name, o.notes from orders o "
            "where o.status = 'paid' and o.shipped_at is null and o.picked_at is null and o.paid_at > now() - make_interval(days => :d) "
            "and not exists (select 1 from dropship_jobs j where j.ebay_order_id = 'web:' || o.order_number) "
            "and not exists (select 1 from po_headers h where h.source = 'web:' || o.order_number and h.status <> 'cancelled') "
            "and not exists (select 1 from inventory_batches b where b.order_id = o.id and b.kind = 'ship') "
            "and exists (select 1 from order_items oi join products p on p.id = oi.product_id "
            "            where oi.order_id = o.id and coalesce(p.web_dropship_ok, false)) "
            "order by o.id limit 50"
        ),
        {"d": RECENT_DAYS},
    ).all()
    made, problems = [], []
    for o in rows:
        try:
            k = fulfillment.key_of("Website", o.order_number)
            if (fulfillment.decisions_for(db, [k]).get(k) or {}).get("decision") == "stock":
                continue  # a person chose to ship this one from our own shelf
            drop, other = _web_lines(db, o.id)
            if not drop:
                continue
            if other:
                msg = (
                    f"MIXED ORDER: {len(drop)} part(s) are set to drop ship but {len(other)} are not ({', '.join(other)[:150]}). "
                    "Nothing was ordered. Use 'Ship from our stock' to pick the whole order here, or make the drop ship PO yourself."
                )
                _web_problem(db, o.order_number, o.id, msg, user)
                problems.append({"order": WEB_PREFIX + o.order_number, "problem": msg})
                continue
            key = WEB_PREFIX + o.order_number
            svc = _web_ship_label(o.notes)
            paid = _buyer_paid_shipping(db, key)
            if _hold_for_shipping(paid, svc):
                _hold_job(db, key, "Website", svc, o.customer_name, "web", o.id, user, paid)
                continue
            res = _make_web_job(db, o, drop, user)
            (problems if res.get("problem") else made).append(res)
        except Exception:
            db.rollback()
            log.exception("drop ship check failed for website order %s", o.order_number)
    return {"checked": len(rows), "made": made, "problems": problems}


def scan_new_orders(db: Session, user: AuthUser = SYSTEM_USER) -> dict:
    _ensure(db)
    rows = db.execute(
        text(
            "select o.ebay_order_id, o.store, o.shipping_service, o.shipping_charged from ebay_orders o "
            "where o.paid_at is not null and not o.cancelled and o.shipped_at_ebay is null "
            "and coalesce(o.created_at_ebay, o.paid_at) > now() - make_interval(days => :d) "
            "and not exists (select 1 from dropship_jobs j where j.ebay_order_id = o.ebay_order_id) "
            "order by o.id limit 50"
        ),
        {"d": RECENT_DAYS},
    ).all()
    made, problems, held = [], [], []
    for r in rows:
        try:
            picks = _flagged_short_lines(db, r.ebay_order_id)
            if not picks:
                continue
            paid = None if r.shipping_charged is None else float(r.shipping_charged) > 0
            if _hold_for_shipping(paid, r.shipping_service):
                _hold_job(db, r.ebay_order_id, r.store, r.shipping_service, None, "ebay", None, user, paid)
                held.append(r.ebay_order_id)
                continue
            res = _make_job(db, r.ebay_order_id, r.store, picks, user)
            (problems if res.get("problem") else made).append(res)
        except Exception:
            db.rollback()
            log.exception("drop ship check failed for eBay order %s", r.ebay_order_id)
    note = f"Checked {len(rows)} new order(s): {len(made)} PO(s) made" + (f", {len(held)} waiting for a shipping review" if held else "") + (f", {len(problems)} need attention" if problems else "") + "."
    db.execute(text("update dropship_settings set last_scan_at = now(), last_scan_note = :n where id = 1"), {"n": note})
    db.commit()
    return {"checked": len(rows), "made": made, "held": held, "problems": problems, "note": note}


# --------------------------------------------------------------------------
# AMAZON ORDERS. Same chain as eBay. A line on an Amazon listing ticked Drop ship (amazon_sales puts it in state 'dropship') is ordered
# from the distributor while the distributor can fill it; otherwise it goes back to Set aside so our shelf fills it. Because
# amazon_orders does not (yet) record what the buyer paid for shipping, EVERY Amazon drop ship order waits for a shipping review first.
# --------------------------------------------------------------------------
def _amazon_flagged_lines(db: Session, order_id: str, honor_decision: bool = False) -> list[int]:
    """Products on this Amazon order to be drop shipped: the listing bought from (or the product) is ticked Drop ship, the product is
    matched to the distributor catalog, and the distributor can fill the line (or our shelf cannot cover it)."""
    from app import fulfillment
    from app.admin_purchase_orders import _amazon_order_and_lines
    from app.availability import has_distributor

    o, lines = _amazon_order_and_lines(db, order_id)
    if not any(l.product_id is not None for l in lines):
        return []
    flags = fulfillment.amazon_line_dropship_flags(db, [o.amazon_order_id])
    approved = False
    if honor_decision:
        k = fulfillment.key_of("Amazon", o.amazon_order_id)
        approved = (fulfillment.decisions_for(db, [k]).get(k) or {}).get("decision") == "dropship"
    avail = fulfillment.distributor_availability(db, [l.product_id for l in lines if l.product_id is not None])

    units = _units_needed(db, "amazon", o.amazon_order_id, lines)

    def _ours_instead(l) -> bool:
        # DROP SHIP PAUSES for this line when the distributor cannot fill it: the line needs `need` UNITS (quantity x pieces per sale)
        # and the distributor's usable count (the better warehouse after the safety margin) is below that. When our own shelf covers it,
        # the line is filled from our shelf instead of using up the distributor's last units (the listing shows our shelf meanwhile).
        need = units.get(int(l.product_id), int(l.quantity or 1))
        a = avail.get(int(l.product_id))
        return (a is None or int(a.get("usable", a["available"])) < need) and int(l.on_hand or 0) >= need

    picks = {
        int(l.product_id)
        for l in lines
        if l.product_id is not None
        and (
            (flags.get((o.amazon_order_id, int(l.product_id))) and not _ours_instead(l))
            or (approved and int(l.on_hand or 0) < int(l.quantity or 1))
        )
    }
    return sorted(pid for pid in picks if has_distributor(db, pid))


def _amazon_problem(db: Session, order_id: str, account: Optional[str], msg: str, user: AuthUser) -> None:
    db.execute(
        text(
            "insert into dropship_jobs (ebay_order_id, store, stage, problem, created_by, channel) "
            "values (:k, :s, 'problem', :m, :u, 'amazon') on conflict (ebay_order_id) do nothing"
        ),
        {"k": AMAZON_PREFIX + order_id, "s": account, "m": msg[:400], "u": user.email},
    )
    db.commit()


def _make_amazon_job(db: Session, order_id: str, account: Optional[str], product_ids: list[int], user: AuthUser, manual: bool = False, ship_to: Optional[dict] = None) -> dict:
    """Make the PO and record the job. A refusal (no buyer address, FBA, policy not acknowledged...) is recorded as 'problem'."""
    from app.admin_purchase_orders import AmazonDropIn, po_from_amazon_order

    key = AMAZON_PREFIX + order_id
    try:
        po = po_from_amazon_order(AmazonDropIn(order=order_id, product_ids=product_ids, ship_to=ship_to), db=db, user=user)
    except HTTPException as e:
        db.rollback()
        msg = str(e.detail)[:400]
        _amazon_problem(db, order_id, account, msg, user)
        return {"order": key, "problem": msg}
    db.execute(
        text(
            "insert into dropship_jobs (ebay_order_id, store, po_id, po_number, stage, ship_service, buyer_name, created_by, channel, manual_ship) "
            "values (:k, :s, :pid, :pn, 'review_po', :svc, :bn, :u, 'amazon', :ms) on conflict (ebay_order_id) do nothing"
        ),
        {"k": key, "s": account, "pid": po["id"], "pn": po["po_number"], "svc": po.get("ship_service"), "bn": po.get("buyer_name"), "u": user.email, "ms": manual},
    )
    db.commit()
    return {"order": key, "po_number": po["po_number"]}


def scan_amazon_orders(db: Session, user: AuthUser = SYSTEM_USER) -> dict:
    """Paid Amazon orders with a line in state 'dropship' and no job yet. Lines the distributor cannot fill (or not matched to the
    distributor catalog) go back to Set aside, so nothing sits in Drop ship without a purchase order."""
    from app import amazon_sales, fulfillment

    _ensure(db)
    rows = db.execute(
        text(
            "select o.amazon_order_id, o.account, o.ship_service as shipping_service_note, o.shipping_charged from amazon_orders o "
            "where o.paid_at is not null and not o.cancelled and o.shipped_at is null "
            "and coalesce(o.created_at_amazon, o.paid_at) > now() - make_interval(days => :d) "
            "and exists (select 1 from amazon_order_lines l where l.order_pk = o.id and l.state = 'dropship') "
            "and not exists (select 1 from dropship_jobs j where j.ebay_order_id = 'amazon:' || o.amazon_order_id) "
            "and not exists (select 1 from po_headers h where h.source = 'amazon:' || o.amazon_order_id and h.status <> 'cancelled') "
            "order by o.id limit 50"
        ),
        {"d": RECENT_DAYS},
    ).all()
    made, problems, held = [], [], []
    for r in rows:
        try:
            k = fulfillment.key_of("Amazon", r.amazon_order_id)
            if (fulfillment.decisions_for(db, [k]).get(k) or {}).get("decision") == "stock":
                amazon_sales.undo_lines_dropship(db, r.amazon_order_id)
                continue
            picks = _amazon_flagged_lines(db, r.amazon_order_id)
            others = db.execute(
                text("select distinct product_id from amazon_order_lines where amazon_order_id = :o and state = 'dropship' and product_id is not null and not (product_id = any(cast(:p as bigint[])))"),
                {"o": r.amazon_order_id, "p": picks},
            ).all()
            if others:
                amazon_sales.undo_lines_dropship(db, r.amazon_order_id, [x.product_id for x in others])
            if not picks:
                continue
            paid = None if r.shipping_charged is None else float(r.shipping_charged) > 0
            if _hold_for_shipping(paid, r.shipping_service_note):
                _hold_job(db, AMAZON_PREFIX + r.amazon_order_id, r.account, r.shipping_service_note, None, "amazon", None, user, paid)
                held.append(r.amazon_order_id)
                continue
            res = _make_amazon_job(db, r.amazon_order_id, r.account, picks, user)
            (problems if res.get("problem") else made).append(res)
        except Exception:
            db.rollback()
            log.exception("drop ship check failed for Amazon order %s", r.amazon_order_id)
    return {"checked": len(rows), "made": made, "held": held, "problems": problems}


def scan_amazon_if_enabled() -> None:
    """Called by the Amazon timer after it has fetched orders (see amazon_sales._loop)."""
    db = _session_factory()()
    try:
        scan_amazon_orders(db)
        handle_cancelled_orders(db)
        run_automatic(db)
    except Exception:
        db.rollback()
        log.exception("automatic drop ship check of Amazon orders failed")
    finally:
        db.close()


def scan_if_enabled() -> None:
    """Called by the eBay sales timer after it has fetched orders: new orders for ticked listings make their PO, and every waiting
    drop ship order moves on."""
    db = _session_factory()()
    try:
        scan_new_orders(db)
        try:
            scan_web_orders(db)  # paid website orders for products ticked Drop ship
        except Exception:
            db.rollback()
            log.exception("automatic drop ship check of website orders failed")
        try:
            scan_amazon_orders(db)  # paid Amazon orders for listings ticked Drop ship
        except Exception:
            db.rollback()
            log.exception("automatic drop ship check of Amazon orders failed")
        handle_cancelled_orders(db)  # an eBay order the buyer cancelled stops here, before anything else moves
        run_automatic(db)
        _flag_cancel_window(db)
    except Exception:
        db.rollback()
        log.exception("automatic drop ship check failed")
    finally:
        db.close()


class SettingsIn(BaseModel):
    manual_checks: Optional[bool] = None  # ON = stop for a person at the PO, quote and tracking steps; OFF (default) = all automatic


@router.get("/settings")
@needs(*PERMS)
def get_settings(db: Session = Depends(get_db)):
    return _settings(db)


@router.put("/settings")
@needs(*PERMS)
def put_settings(body: SettingsIn, db: Session = Depends(get_db)):
    _ensure(db)
    if body.manual_checks is not None:
        db.execute(text("update dropship_settings set manual_checks = :a where id = 1"), {"a": body.manual_checks})
    db.commit()
    return _settings(db)


@router.get("/jobs")
@needs(*PERMS)
def list_jobs(stage: Optional[str] = Query(None, max_length=20), db: Session = Depends(get_db)):
    from app.admin_purchase_orders import _po_detail

    _ensure(db)
    rows = db.execute(
        text(
            "select j.id, j.ebay_order_id, j.store, j.po_id, coalesce(h.po_number, j.po_number) as po_number, j.stage, j.ship_service, "
            "j.buyer_name, j.problem, j.created_at, j.confirmed_by, j.confirmed_at, "
            "j.quote, j.ship_method, j.supplier_order, j.supplier_mode, j.tracking_number, j.tracking_carrier, j.note, j.tries, "
            "j.channel, j.web_order_id "
            "from dropship_jobs j left join po_headers h on h.id = j.po_id "
            + ("where j.stage in ('review_po', 'po_confirmed', 'review_quote', 'review_tracking') " if stage == "review"
               else "where j.stage = :s " if stage else "where j.stage <> 'dismissed' ")
            + "order by j.id desc limit 100"
        ),
        {"s": stage} if stage and stage != "review" else {},
    ).all()
    out = []
    for r in rows:
        j = dict(r._mapping)
        try:
            j["quote"] = json.loads(r.quote) if r.quote else None
        except ValueError:
            j["quote"] = None
        if r.po_id and r.stage in ("review_po", "po_confirmed", "review_quote", "review_tracking"):
            try:
                j["po"] = _po_detail(db, r.po_id)
            except HTTPException:
                j["po"] = None
        out.append(j)
    return {"jobs": out}


@router.post("/scan")
@needs(*PERMS)
def scan_now(db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    out = scan_new_orders(db, user)
    try:
        out["web"] = scan_web_orders(db, user)
    except Exception:
        db.rollback()
        log.exception("check of website orders failed")
        out["web"] = {"error": True}
    out["tracking_ready"] = advance_tracking(db)
    out["automatic"] = run_automatic(db)
    return out


class FromOrderIn(BaseModel):
    order: str = Field(..., min_length=3, max_length=40)


@router.post("/jobs/from-order", status_code=201)
@needs(*PERMS)
def job_from_order(body: FromOrderIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    from app.admin_purchase_orders import _ebay_order_and_lines

    _ensure(db)
    o, _lines = _ebay_order_and_lines(db, body.order)
    if db.execute(text("select 1 from dropship_jobs where ebay_order_id = :o and stage <> 'dismissed'"), {"o": o.ebay_order_id}).first():
        raise HTTPException(409, f"eBay order {o.ebay_order_id} is already in the list.")
    db.execute(text("delete from dropship_jobs where ebay_order_id = :o"), {"o": o.ebay_order_id})
    db.commit()
    picks = _flagged_short_lines(db, o.ebay_order_id, honor_decision=True)
    if not picks:
        raise HTTPException(
            409,
            "Nothing on that order can be drop shipped: no line is short on our shelf with its listing set to Drop ship, "
            "and nobody approved a drop ship for it on Pick & ship.",
        )
    res = _make_job(db, o.ebay_order_id, o.store, picks, user)
    if res.get("problem"):
        raise HTTPException(409, res["problem"])
    run_automatic(db)
    return res


def _job(db: Session, job_id: int):
    _ensure(db)
    j = db.execute(text("select id, stage, po_number from dropship_jobs where id = :i"), {"i": job_id}).first()
    if not j:
        raise HTTPException(404, "That drop ship order is not in the list.")
    return j


@router.post("/jobs/{job_id}/confirm")
@needs(*PERMS)
def confirm_po(job_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    j = _job(db, job_id)
    if j.stage != "review_po":
        raise HTTPException(409, "That order is not waiting for a PO check.")
    db.execute(
        text("update dropship_jobs set stage = 'po_confirmed', confirmed_by = :u, confirmed_at = now(), updated_at = now() where id = :i"),
        {"u": user.email, "i": job_id},
    )
    db.commit()
    return _run_quote(db, job_id, None)


# --------------------------------------------------------------------------
# step 3 and 4: quote, then place
# --------------------------------------------------------------------------
def _norm(v: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (v or "").lower())


def _service_words(name: str) -> tuple[Optional[str], Optional[str]]:
    """(carrier, speed) read out of a shipping service name, whichever side it comes from (eBay's or the distributor's)."""
    n = _norm(name)
    carrier = "fedex" if ("fedex" in n or "fdx" in n) else "ups" if "ups" in n else "usps" if ("usps" in n or "postal" in n) else None
    speed = None
    for key, words in (
        ("overnight", ("nextday", "overnight", "1day", "nextbusiness", "expressmail", "mailexpress")),
        ("2day", ("2day", "secondday", "2ndday", "twoday")),
        ("3day", ("3day", "threeday", "express saver", "expresssaver")),
        ("ground", ("ground", "standard", "economy", "homedelivery", "surepost", "groundadvantage")),
        ("priority", ("priority",)),
    ):
        if any(w in n for w in words):
            speed = key
            break
    return carrier, speed


# Free shipping goes out on the distributor's flat-rate "Best Way" service. "BEST" is always available and lets them pick from the
# services they have for the order; any other code is billed at a variable rate that is usually higher. Free is decided by what the
# buyer PAID ($0 shipping), because some listings offer Priority Mail as the free service. The names below are only used when the
# amount paid is unknown.
FLAT_RATE_CODE = "BEST"
FREE_NAMES = {"uspsparcel", "uspsgroundadvantage"}  # eBay's free service; the website's $0 option
PAID_CODES = {"uspspriority": "USPR", "uspsprioritymail": "USPR"}  # a buyer who paid for Priority Mail gets Priority Mail


def _buyer_paid_shipping(db: Session, key) -> Optional[bool]:
    """True/False when we know whether the buyer paid anything for shipping, None when unknown."""
    try:
        if _is_web(key):
            v = db.execute(text("select shipping_cents from orders where 'web:' || order_number = :k"), {"k": key}).scalar()
            return None if v is None else int(v) > 0
        if _is_amazon(key):
            v = db.execute(text("select shipping_charged from amazon_orders where 'amazon:' || amazon_order_id = :k limit 1"), {"k": key}).scalar()
            return None if v is None else float(v) > 0
        v = db.execute(text("select shipping_charged from ebay_orders where ebay_order_id = :o limit 1"), {"o": key}).scalar()
        return None if v is None else float(v) > 0
    except Exception:
        db.rollback()
        return None


def _hold_for_shipping(paid: Optional[bool], service: Optional[str]) -> bool:
    """True when an order must wait for a person to review its shipping: the buyer paid for shipping, or we cannot tell and the service
    is not a known free one. eBay's international hub orders are not held here: the PO step refuses them and they show as a problem."""
    n = _norm(service or "")
    if "intermi" in n or "global" in n:
        return False
    if paid is True:
        return True
    if paid is None:
        return n not in FREE_NAMES
    return False


def _hold_job(db: Session, key: str, store: Optional[str], service: Optional[str], buyer: Optional[str], channel: str, web_id: Optional[int], user: AuthUser, paid: Optional[bool]) -> None:
    """Record the order as waiting for a shipping review. No PO is made and nothing is sent to the distributor."""
    why = "Buyer paid for shipping" if paid else "Could not tell what the buyer paid for shipping"
    note = f"{why} ({service or 'no shipping service given'}). Choose our own stock, or drop ship and set the shipping."
    db.execute(
        text(
            "insert into dropship_jobs (ebay_order_id, store, stage, ship_service, buyer_name, created_by, channel, web_order_id, manual_ship, note) "
            "values (:k, :s, 'review_shipping', :svc, :bn, :u, :ch, :w, true, :n) on conflict (ebay_order_id) do nothing"
        ),
        {"k": key, "s": store, "svc": service, "bn": buyer, "u": user.email, "ch": channel, "w": web_id, "n": note[:400]},
    )
    db.commit()


def held_job_for(db: Session, channel: str, order_ref: str):
    """The job waiting for a shipping review for this order (channel 'eBay' or 'Website'), or None."""
    _ensure(db)
    ch = str(channel).lower()
    key = WEB_PREFIX + order_ref if ch == "website" else AMAZON_PREFIX + order_ref if ch == "amazon" else order_ref
    return db.execute(text("select id, stage from dropship_jobs where ebay_order_id = :k and stage = 'review_shipping'"), {"k": key}).first()


def approve_held_job(db: Session, job_id: int, user: AuthUser) -> dict:
    """Approve drop ship on an order held for a shipping review: make its PO and quote, then stop at the quote check. The job is marked
    manual, so the automatic runner never places it: a person sets the shipping on the Drop ship page and places the order."""
    from app import fulfillment

    _ensure(db)
    j = db.execute(
        text("select id, stage, ebay_order_id, store, channel, web_order_id from dropship_jobs where id = :i"), {"i": job_id}
    ).first()
    if not j:
        raise HTTPException(404, "That drop ship order is not in the list.")
    if j.stage != "review_shipping":
        raise HTTPException(409, "That order is not waiting for a shipping review.")
    key = str(j.ebay_order_id)
    web = j.channel == "web"
    amazon = j.channel == "amazon"
    o = None
    if amazon:
        drop = _amazon_flagged_lines(db, key[len(AMAZON_PREFIX):])
        if not drop:
            raise HTTPException(409, "Nothing on this order is set to drop ship any more. Ship it from our own stock, or make the PO by hand.")
    elif web:
        o = db.execute(text("select id, order_number, customer_name, notes from orders where id = :i"), {"i": j.web_order_id}).first()
        if not o:
            raise HTTPException(404, "The website order is no longer there.")
        drop, other = _web_lines(db, o.id)
        if not drop or other:
            raise HTTPException(409, "The parts on this order are no longer all set to drop ship. Ship it from our own stock, or make the PO by hand.")
    else:
        drop = _flagged_short_lines(db, key)
        if not drop:
            raise HTTPException(409, "Nothing on this order is set to drop ship any more. Ship it from our own stock, or make the PO by hand.")
    # the held job is replaced by the real one
    db.execute(text("delete from dropship_jobs where id = :i and stage = 'review_shipping'"), {"i": job_id})
    db.commit()
    if amazon:
        res = _make_amazon_job(db, key[len(AMAZON_PREFIX):], j.store, drop, user, manual=True)
    else:
        res = _make_web_job(db, o, drop, user, manual=True) if web else _make_job(db, key, j.store, drop, user, manual=True)
    if res.get("problem"):
        raise HTTPException(409, res["problem"])
    new_id = db.execute(text("select id from dropship_jobs where ebay_order_id = :k"), {"k": key}).scalar()
    ref = key[len(WEB_PREFIX):] if web else key[len(AMAZON_PREFIX):] if amazon else key
    fulfillment.record_decision(db, "Website" if web else "Amazon" if amazon else "eBay", ref, "dropship", user.email, ["Buyer paid for shipping; drop ship approved"])
    quoted = confirm_po(new_id, db=db, user=user)
    return {"ok": True, "job_id": new_id, "quote": quoted}


@router.post("/jobs/{job_id}/approve-drop-ship")
@needs(*PERMS)
def approve_drop_ship(job_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    return approve_held_job(db, job_id, user)


def pick_shipping_option(buyer_service: Optional[str], options: list[dict], free: bool = False) -> Optional[str]:
    """What to ship the order on. Free shipping (the buyer paid $0, or it is a known free service) is "BEST". A paid Priority Mail
    uses USPR when the distributor offers it. Anything else is the quoted option closest to the buyer's choice (same speed; the same
    carrier is preferred), or None when there is no match."""
    n = _norm(buyer_service or "")
    if free or n in FREE_NAMES:
        return FLAT_RATE_CODE
    code = PAID_CODES.get(n)
    if code and any(str(o.get("carrier_code") or "") == code for o in options or []):
        return code
    if not buyer_service or not options:
        return None
    bc, bs = _service_words(buyer_service)
    if not bs:
        return None
    best, best_score = None, 0
    for o in options:
        name = str(o.get("name") or "")
        oc, os_ = _service_words(name)
        if os_ != bs:
            continue
        score = 1 + (2 if bc and oc == bc else 0)
        try:
            cost = float(o.get("cost"))
        except (TypeError, ValueError):
            cost = 1e9
        # a higher score wins; among equals the cheaper one
        if score > best_score or (score == best_score and best is not None and cost < best[1]):
            best, best_score = (name, cost), score
    return best[0] if best else None


def method_code(method: Optional[str], options: list) -> str:
    """What the order API wants as the shipping method: the option's carrier CODE (the same value the Orders screen sends), not its
    display name. `method` may be a name (what the page shows and saves) or already a code; anything unknown is passed through."""
    m = (method or "").strip()
    if not m:
        return ""
    for o in options or []:
        if not isinstance(o, dict):
            continue
        name, code = str(o.get("name") or "").strip(), str(o.get("carrier_code") or "").strip()
        if m == name or m == code:
            return code or name
    return m


def _order_in(db: Session, job, method: Optional[str], confirm: bool):
    from app.autodist import OrderAddress, OrderIn, OrderLine, api_po

    po = api_po(job.po_id, db=db)
    missing = [l["item"] or l["description"] for l in po["lines"] if not l["number"]]
    if missing:
        raise HTTPException(409, "These PO lines are not matched to a distributor part number: " + ", ".join(str(m) for m in missing)[:200])
    if not po["lines"]:
        raise HTTPException(409, "That PO has no open lines.")
    addr = {k: v for k, v in (po["ship_to"] or {}).items() if k in OrderAddress.model_fields}
    return OrderIn(
        po_number=str(po["po_number"]),
        comment=(f"Drop ship for website order {str(job.ebay_order_id)[len(WEB_PREFIX):]}" if _is_web(job.ebay_order_id) else f"Drop ship for eBay order {job.ebay_order_id}")[:200],
        shipping_method=method or "",
        ship_to=OrderAddress(**addr),
        items=[OrderLine(number=l["number"], quantity=int(l["quantity"])) for l in po["lines"]],
        po_id=job.po_id,
        confirm=confirm,
    )


def _run_quote(db: Session, job_id: int, method: Optional[str]) -> dict:
    """Get the quote for a job's PO. With no method, the option closest to the buyer's eBay choice is picked when there is one.
    A failure keeps the job where it was and records the reason, so the page can show it and offer to try again."""
    from app.autodist import api_order_quote

    j = db.execute(
        text("select id, po_id, ebay_order_id, ship_service, stage from dropship_jobs where id = :i"), {"i": job_id}
    ).first()
    try:
        body = _order_in(db, j, method, False)
        first = api_order_quote(body)
        options = (first.get("totals") or {}).get("shipping_options") or []
        paid = _buyer_paid_shipping(db, j.ebay_order_id)
        chosen = method or pick_shipping_option(j.ship_service, options, free=(paid is False))
        fallback = False
        if not chosen and not _settings(db)["manual_checks"]:
            # no option matches what the buyer picked: with Manual checks OFF use the flat-rate Best Way service
            chosen = FLAT_RATE_CODE
            fallback = True
        quote = first
        code = method_code(chosen, options)  # the order API takes the option's code, not its display name
        if code and code != (body.shipping_method or ""):
            body.shipping_method = code
            quote = api_order_quote(body)
        quote["options"] = options
        quote["method"] = chosen
        quote["fallback"] = fallback
        quote["server_mode"] = first.get("mode")
    except HTTPException as e:
        db.rollback()
        msg = ("Quote failed: " + str(e.detail))[:400]
        db.execute(text("update dropship_jobs set problem = :m, updated_at = now() where id = :i"), {"m": msg, "i": job_id})
        db.commit()
        return {"ok": False, "stage": j.stage, "problem": msg}
    db.execute(
        text(
            "update dropship_jobs set stage = 'review_quote', quote = :q, ship_method = :m, problem = null, note = :n, updated_at = now() where id = :i"
        ),
        {
            "q": json.dumps(quote, default=str), "m": quote["method"], "i": job_id,
            "n": (f"No option matched the buyer's choice ({j.ship_service or 'none given'}); {quote['method']} (flat rate Best Way) was used." if quote.get("fallback") else None),
        },
    )
    db.commit()
    return {"ok": True, "stage": "review_quote", "method": quote["method"]}


class QuoteIn(BaseModel):
    method: Optional[str] = Field(None, max_length=60)


@router.post("/jobs/{job_id}/quote")
@needs(*PERMS)
def quote_job(job_id: int, body: QuoteIn, db: Session = Depends(get_db)):
    """Get (or redo) the quote. With a method, that shipping option is used; used for the pick at the quote check."""
    j = _job(db, job_id)
    if j.stage not in ("po_confirmed", "review_quote"):
        raise HTTPException(409, "That order is not at the quote step.")
    return _run_quote(db, job_id, (body.method or "").strip() or None)


@router.post("/jobs/{job_id}/place")
@needs(*PERMS)
def place_job(job_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """Place the order for real. Owners and admins only. The existing safeguards in the order API still apply."""
    from app.autodist import api_order_place

    if user.role not in ADMIN_ROLES:
        raise HTTPException(403, "Only an owner or admin can place the order with the distributor.")
    j = _job(db, job_id)
    if j.stage != "review_quote":
        raise HTTPException(409, "That order is not waiting for the quote check.")
    full = db.execute(
        text(
            "select j.id, j.po_id, j.ebay_order_id, j.ship_method, j.quote, coalesce(cast(h.po_number as text), cast(j.po_number as text)) as po_number_text "
            "from dropship_jobs j left join po_headers h on h.id = j.po_id where j.id = :i"
        ),
        {"i": job_id},
    ).first()
    if db.execute(
        text(
            "select 1 where exists (select 1 from ebay_orders where ebay_order_id = :o and cancelled) "
            "or exists (select 1 from orders where 'web:' || order_number = :o and status = 'cancelled') "
            "or exists (select 1 from amazon_orders where 'amazon:' || amazon_order_id = :o and cancelled)"
        ),
        {"o": full.ebay_order_id},
    ).first():
        # the buyer's cancellation already reached us: nothing is sent to the distributor
        handle_cancelled_orders(db, only_job=job_id)
        raise HTTPException(409, "This order was cancelled, so it was not placed with the distributor.")
    if not full.ship_method:
        raise HTTPException(409, "Pick a shipping option first.")
    try:
        saved_options = (json.loads(full.quote) if full.quote else {}).get("options") or []
    except ValueError:
        saved_options = []
    body = _order_in(db, full, method_code(full.ship_method, saved_options), True)
    # claim the job first so a double click cannot place it twice
    claimed = db.execute(
        text("update dropship_jobs set stage = 'placing', updated_at = now() where id = :i and stage = 'review_quote' returning id"),
        {"i": job_id},
    ).first()
    db.commit()
    if not claimed:
        raise HTTPException(409, "That order is already being placed.")
    try:
        res = api_order_place(body, db=db, user=user)
    except HTTPException as e:
        db.rollback()
        # A 504 means the order MAY have been placed: keep it out of the normal flow until a person has checked.
        new_stage = "problem" if e.status_code == 504 else "review_quote"
        detail = str(e.detail)
        low = detail.lower()
        if new_stage == "review_quote" and "po number" in low and ("exist" in low or "duplicate" in low):
            msg = (
                f"Not placed: the distributor already has an order with PO number {full.po_number_text}. Nothing was ordered. "
                "Give this PO a new, unused number in the purchase order editor, then place it again."
            )
        else:
            msg = ("Not placed: " if new_stage == "review_quote" else "CHECK WITH THE DISTRIBUTOR BEFORE TRYING AGAIN: ") + detail[:350]
        db.execute(
            text("update dropship_jobs set stage = :s, problem = :m, updated_at = now() where id = :i"),
            {"s": new_stage, "m": msg[:400], "i": job_id},
        )
        db.commit()
        raise
    if not res.get("placed"):
        db.execute(
            text("update dropship_jobs set stage = 'problem', problem = :m, updated_at = now() where id = :i"),
            {"m": "The distributor did not return an order number: " + str(res.get("message") or "")[:300], "i": job_id},
        )
        db.commit()
        raise HTTPException(502, "The distributor did not return an order number. Check with them before trying again.")
    db.execute(
        text(
            "update dropship_jobs set stage = 'placed', supplier_order = :o, supplier_mode = :m, problem = null, placed_at = now(), updated_at = now() where id = :i"
        ),
        {"o": str(res.get("order_number")), "m": res.get("mode"), "i": job_id},
    )
    db.commit()
    return {"ok": True, "stage": "placed", "order_number": res.get("order_number"), "mode": res.get("mode")}


# --------------------------------------------------------------------------
# the buyer cancelled the eBay order: stop the PO, or cancel the distributor's order, or say a return is needed
# --------------------------------------------------------------------------
CANCEL_PREFIX = "eBay order cancelled"


def _set_cancel_state(db: Session, job_id: int, problem: str, stage: Optional[str] = None) -> None:
    """Writes the message only when it changed, so a job that is retried every few minutes does not keep touching itself."""
    db.execute(
        text(
            "update dropship_jobs set problem = cast(:m as text), stage = coalesce(cast(:s as text), stage), updated_at = now() "
            "where id = :i and (problem is distinct from cast(:m as text) or (cast(:s as text) is not null and stage <> cast(:s as text)))"
        ),
        {"m": problem[:500], "s": stage, "i": job_id},
    )
    db.commit()


def _cancel_open_po(db: Session, po_id, reason: str) -> None:
    from app.admin_purchase_orders import CancelIn, cancel_po

    if not po_id:
        return
    st = db.execute(text("select status from po_headers where id = :p"), {"p": po_id}).scalar()
    if st and st not in ("cancelled", "received"):
        try:
            cancel_po(po_id, CancelIn(reason=reason), db=db, user=SYSTEM_USER)
        except HTTPException:
            db.rollback()  # already cancelled or received in the meantime: nothing more to do


def handle_cancelled_orders(db: Session, only_job: Optional[int] = None) -> dict:
    """Every drop ship job whose eBay order the buyer cancelled:
      * the distributor has no order yet (waiting at the PO / quote / hold steps): its PO is cancelled and the job closed;
      * the distributor has the order and has NOT invoiced it: the order is cancelled with them, then the PO, then the job closed;
      * the distributor already invoiced it (it has shipped): the job moves to 'return_needed' with the order number, because
        only a return can be arranged from there;
      * the distributor could not be reached: the job is left as it is with a note, and tried again after 15 minutes.
    A job being placed right now is skipped and picked up on the next pass."""
    from app.autodist import CancelOrderIn, api_order_cancel

    _ensure(db)
    out = {"stopped": 0, "distributor_cancelled": 0, "return_needed": 0, "retry_later": 0}
    rows = db.execute(
        text(
            "select j.id, j.stage, j.po_id, j.po_number, j.supplier_order, j.problem, j.ebay_order_id, j.channel "
            "from dropship_jobs j "
            "left join ebay_orders o on j.channel = 'ebay' and o.ebay_order_id = j.ebay_order_id "
            "left join orders w on j.channel = 'web' and 'web:' || w.order_number = j.ebay_order_id "
            "left join amazon_orders ao on j.channel = 'amazon' and 'amazon:' || ao.amazon_order_id = j.ebay_order_id "
            "where (coalesce(o.cancelled, false) or w.status = 'cancelled' or coalesce(ao.cancelled, false)) and j.stage not in ('done', 'dismissed', 'return_needed', 'placing') "
            "and (j.problem is null or (j.problem not like cast(:pfx as text) and j.problem not like cast(:wpfx as text) and j.problem not like cast(:apfx as text)) "
            "     or j.updated_at < now() - interval '15 minutes') "
            + ("and j.id = :jid " if only_job else "")
            + "order by j.id limit 50"
        ),
        {"pfx": CANCEL_PREFIX + "%", "wpfx": WEB_CANCEL_PREFIX + "%", "apfx": AMAZON_CANCEL_PREFIX + "%", **({"jid": only_job} if only_job else {})},
    ).all()
    for r in rows:
        web = r.channel == "web"
        amz = r.channel == "amazon"
        pfx = WEB_CANCEL_PREFIX if web else AMAZON_CANCEL_PREFIX if amz else CANCEL_PREFIX
        why = (
            f"Website order {str(r.ebay_order_id)[len(WEB_PREFIX):]} was cancelled" if web
            else f"Amazon order {str(r.ebay_order_id)[len(AMAZON_PREFIX):]} was cancelled" if amz
            else f"The buyer cancelled eBay order {r.ebay_order_id}"
        )
        try:
            so = None
            pn = str(r.po_number or "").strip()
            if r.supplier_order or pn:
                so = db.execute(
                    text(
                        "select id, order_number, status from supplier_orders where supplier = 'autodist' and order_number <> '' "
                        "and ((cast(:on as text) <> '' and order_number = cast(:on as text)) or (cast(:on as text) = '' and cast(:pn as text) <> '' and cast(po_number as text) = cast(:pn as text))) "
                        "order by id desc limit 1"
                    ),
                    {"on": r.supplier_order or "", "pn": pn},
                ).first()
            if so is None and r.stage == "problem" and (r.problem or "").startswith("CHECK WITH THE DISTRIBUTOR"):
                # placing the order got no answer: the distributor may have it and we cannot tell from here
                _set_cancel_state(db, r.id, f"{pfx}, and the order may already be with the distributor. Check Automatic Distributors for the order from PO {pn} and cancel it there if it exists.", "return_needed")
                out["return_needed"] += 1
                continue
            done_with = "no order had been placed with the distributor"
            if so is not None:
                if str(so.status or "").strip().lower() == "cancelled":
                    done_with = f"distributor order {so.order_number} was already cancelled"
                else:
                    try:
                        api_order_cancel(so.id, CancelOrderIn(confirm=True), db=db, user=SYSTEM_USER)
                    except HTTPException as e:
                        db.rollback()
                        detail = str(e.detail)
                        if e.status_code in (404, 409):
                            # invoiced / shipped, or on the other server: a person has to take it from here
                            ret = "invoiced" in detail.lower() or "shipped" in detail.lower()
                            head = "RETURN NEEDED: " if ret else "NEEDS YOU: "
                            _set_cancel_state(db, r.id, f"{pfx}. {head}{detail} Distributor order {so.order_number}, PO {pn}.", "return_needed")
                            out["return_needed"] += 1
                            continue
                        _set_cancel_state(db, r.id, f"{pfx}, but the distributor order {so.order_number} is NOT cancelled yet: {detail[:300]} It will be tried again.")
                        out["retry_later"] += 1
                        if e.status_code == 429:
                            break
                        continue
                    done_with = f"distributor order {so.order_number} cancelled"
                    out["distributor_cancelled"] += 1
            _cancel_open_po(db, r.po_id, f"{why}; {done_with}")
            db.execute(
                text("update dropship_jobs set stage = 'dismissed', problem = null, note = :n, updated_at = now() where id = :i"),
                {"n": f"{why}: {done_with}.", "i": r.id},
            )
            db.commit()
            out["stopped"] += 1
        except Exception:
            db.rollback()
            log.exception("could not handle the cancelled eBay order %s", r.ebay_order_id)
    return out


def _flag_cancel_window(db: Session) -> None:
    """The distributor has invoiced (shipped) an order but eBay has no tracking yet: the buyer can still ask to cancel on
    eBay, and nothing can stop this shipment any more. Say so on the job so that request gets declined, not accepted."""
    try:
        db.execute(
            text(
                "update dropship_jobs j set note = :m || case when coalesce(j.note, '') = '' then '' else ' | ' || j.note end, updated_at = now() "
                "from supplier_orders so, ebay_orders o "
                "where j.stage = 'placed' and so.supplier = 'autodist' and so.order_number = j.supplier_order "
                "and upper(coalesce(so.status, '')) in ('4', '4S', 'H') "
                "and o.ebay_order_id = j.ebay_order_id and not o.cancelled and o.shipped_at_ebay is null "
                "and coalesce(j.note, '') not like 'Heads-up:%'"
            ),
            {"m": "Heads-up: the distributor has already invoiced and shipped this order, but eBay has no tracking yet. If the buyer asks to cancel on eBay, DECLINE it (a cancel cannot stop this shipment); a return is the only way back."},
        )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not flag the cancel window")


@router.post("/jobs/{job_id}/dismiss")
@needs(*PERMS)
def dismiss_job(job_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """Clears an order that was cancelled on eBay after the distributor had shipped it, once the return (or the cancel at
    the distributor) has been taken care of by hand."""
    j = _job(db, job_id)
    if j.stage != "return_needed":
        raise HTTPException(409, "Only a cancelled order that is waiting for you can be cleared here.")
    db.execute(
        text("update dropship_jobs set stage = 'dismissed', note = :n, updated_at = now() where id = :i"),
        {"n": f"Cancelled on eBay; handled by {user.email}.", "i": job_id},
    )
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# step 6: tracking back, then eBay
# --------------------------------------------------------------------------
def _destination(db: Session, job_id: int) -> str:
    """Where a job's tracking goes, for messages."""
    ch = db.execute(text("select channel from dropship_jobs where id = :i"), {"i": job_id}).scalar()
    return "the website order" if ch == "web" else "Amazon" if ch == "amazon" else "eBay"


def _web_tracking(db: Session) -> list[int]:
    """A placed WEBSITE order whose distributor order is fully shipped and has a parcel tracking number moves to 'review_tracking'
    (the tracking is read straight from the distributor order the tracking check keeps up to date). Returns the job ids."""
    from app import autodist

    try:
        rows = db.execute(
            text(
                "select j.id, so.tracking, so.status from dropship_jobs j "
                "join supplier_orders so on so.supplier = 'autodist' and so.order_number = j.supplier_order "
                "where j.channel in ('web', 'amazon') and j.stage = 'placed' and so.tracking is not null"
            )
        ).all()
    except Exception:
        db.rollback()
        log.exception("could not look for tracking on website drop ship orders")
        return []
    out: list[int] = []
    for r in rows:
        try:
            items = r.tracking if isinstance(r.tracking, list) else json.loads(r.tracking or "[]")
        except ValueError:
            continue
        if not isinstance(items, list) or autodist._not_shipped_yet(r.status):
            continue  # not everything has gone out yet: wait for the rest
        parcel = next((i for i in items if isinstance(i, dict) and i.get("kind") == "tracking" and i.get("number")), None)
        if not parcel:
            continue
        num = re.sub(r"\s+", "", str(parcel["number"]))
        if not _TRACK_OK.match(num):
            continue
        got = db.execute(
            text(
                "update dropship_jobs set stage = 'review_tracking', problem = null, tracking_number = :t, tracking_carrier = :c, updated_at = now() "
                "where id = :i and stage = 'placed' returning id"
            ),
            {"t": num, "c": autodist._guess_carrier(num, parcel.get("carrier")), "i": r.id},
        ).first()
        if got:
            out.append(int(got.id))
    db.commit()
    return out


def advance_tracking(db: Session) -> int:
    """A placed order whose tracking has reached its eBay order (the Automatic Distributors tracking check does that) moves to
    'review_tracking'. With "Manual checks" OFF it is then sent to eBay straight away; with it ON
    nothing is sent here and a person checks it first."""
    _ensure(db)
    web_ids = _web_tracking(db)
    rows = db.execute(
        text(
            "update dropship_jobs j set stage = 'review_tracking', problem = null, tracking_number = o.tracking_number, "
            "tracking_carrier = o.tracking_carrier, updated_at = now(), "
            "note = case when j.note like 'Heads-up:%' then null else j.note end "
            "from ebay_orders o where j.stage = 'placed' and o.ebay_order_id = j.ebay_order_id and not o.cancelled "
            "and coalesce(o.tracking_number, '') <> '' returning j.id"
        )
    ).all()
    db.commit()
    ids = [r.id for r in rows] + web_ids
    if ids and not _settings(db)["manual_checks"]:
        for jid in ids:
            try:
                _send_tracking_now(db, jid, SYSTEM_USER)
            except Exception as e:
                db.rollback()
                msg = str(getattr(e, "detail", e))[:300]
                log.warning("automatic send of tracking failed for job %s: %s", jid, msg)
                db.execute(
                    text("update dropship_jobs set problem = :m, updated_at = now() where id = :i"),
                    {"m": f"Tracking was NOT sent to {_destination(db, jid)} automatically: " + msg + " Check it here and send it yourself.", "i": jid},
                )
                db.commit()
    return len(ids)


def run_automatic(db: Session) -> dict:
    """With "Manual checks" OFF, carry every waiting drop ship order through the review steps by itself:
    PO check -> quote -> place the order -> (tracking, handled by advance_tracking) -> tell eBay.
    An order that has a problem noted (a failed quote, a refused order, eBay refusing the tracking) is NOT retried here; it waits for a
    person, so nothing is hammered or ordered twice. Order placing keeps every existing safeguard (test/live server, live switch,
    duplicate PO number)."""
    _ensure(db)
    if _settings(db)["manual_checks"]:
        return {"manual": True}
    done = {"confirmed": 0, "placed": 0, "stopped": 0}
    for _ in range(2):  # a second pass lets an order that was just confirmed continue to the quote and the order in the same run
        ids = db.execute(
            text("select id, stage, ship_method from dropship_jobs where stage in ('review_po', 'review_quote') and problem is null and not coalesce(manual_ship, false) order by id limit 25")
        ).all()
        if not ids:
            break
        for r in ids:
            try:
                if r.stage == "review_po":
                    confirm_po(r.id, db=db, user=SYSTEM_USER)  # confirms, then gets the quote
                    done["confirmed"] += 1
                elif r.stage == "review_quote":
                    if not r.ship_method:
                        db.execute(
                            text("update dropship_jobs set problem = 'No shipping option matched the buyer''s choice. Pick one and place the order yourself.', updated_at = now() where id = :i"),
                            {"i": r.id},
                        )
                        db.commit()
                        done["stopped"] += 1
                        continue
                    place_job(r.id, db=db, user=SYSTEM_USER)
                    done["placed"] += 1
            except HTTPException as e:
                db.rollback()
                done["stopped"] += 1
                log.warning("automatic drop ship step failed for job %s: %s", r.id, e.detail)
                # place_job and the quote already record their own reason; make sure there is one so the loop does not retry it
                db.execute(
                    text("update dropship_jobs set problem = coalesce(problem, :m), updated_at = now() where id = :i and stage in ('review_po', 'review_quote')"),
                    {"m": ("Automatic step stopped: " + str(e.detail))[:400], "i": r.id},
                )
                db.commit()
            except Exception:
                db.rollback()
                log.exception("automatic drop ship step crashed for job %s", r.id)
                db.execute(
                    text("update dropship_jobs set problem = coalesce(problem, 'Automatic step crashed; check the log and do it yourself.'), updated_at = now() where id = :i and stage in ('review_po', 'review_quote')"),
                    {"i": r.id},
                )
                db.commit()
    done["retried"] = _retry_failures(db)
    done["tracking_sent"] = advance_tracking(db)
    _flag_freight(db)
    return done


MAX_TRIES = 3


def _retry_failures(db: Session) -> int:
    """With Manual checks OFF, a quote that failed or a tracking number eBay refused is tried again on the next checks, up to
    MAX_TRIES times in all (they are often a passing glitch). The order itself is never re-sent to the distributor here."""
    n = 0
    for r in db.execute(
        text(
            "select id, stage, problem from dropship_jobs where tries < :m and problem is not null and "
            "((stage in ('review_po', 'po_confirmed') and problem like 'Quote failed%') or "
            " (stage = 'review_tracking' and problem like 'Tracking was NOT sent%')) order by id limit 20"
        ),
        {"m": MAX_TRIES},
    ).all():
        db.execute(text("update dropship_jobs set tries = tries + 1, updated_at = now() where id = :i"), {"i": r.id})
        db.commit()
        n += 1
        try:
            if r.stage == "review_tracking":
                _send_tracking_now(db, r.id, SYSTEM_USER)
            else:
                _run_quote(db, r.id, None)
        except Exception as e:
            db.rollback()
            msg = str(getattr(e, "detail", e))[:300]
            if r.stage == "review_tracking":
                db.execute(
                    text("update dropship_jobs set problem = :m where id = :i"),
                    {"m": f"Tracking was NOT sent to {_destination(db, r.id)} automatically: {msg} Check it and send it yourself.", "i": r.id},
                )
                db.commit()
    return n


def _flag_freight(db: Session) -> None:
    """A placed order whose only tracking is a freight PRO number (never a parcel number eBay accepts) would wait forever.
    Say so on the order, so a person can enter the carrier and tracking number by hand."""
    try:
        rows = db.execute(
            text(
                "select j.id, so.tracking from dropship_jobs j join supplier_orders so on so.order_number = j.supplier_order and so.supplier = 'autodist' "
                "where j.stage = 'placed' and j.problem is null and so.tracking is not null"
            )
        ).all()
    except Exception:
        db.rollback()
        return
    for r in rows:
        try:
            items = r.tracking if isinstance(r.tracking, list) else json.loads(r.tracking or "[]")
        except ValueError:
            continue
        if not isinstance(items, list):
            continue
        pro = next((i for i in items if isinstance(i, dict) and i.get("kind") == "pro" and i.get("number")), None)
        if pro and not any(isinstance(i, dict) and i.get("kind") == "tracking" and i.get("number") for i in items):
            db.execute(
                text("update dropship_jobs set problem = :m, updated_at = now() where id = :i and stage = 'placed' and problem is null"),
                {"m": f"Shipping by freight (PRO number {str(pro['number'])[:30]}): there is no parcel tracking number eBay accepts. Enter the carrier and tracking number yourself.", "i": r.id},
            )
    db.commit()


def _ship_web_order(db: Session, job_id: int, j, user: AuthUser) -> None:
    """The distributor shipped a website order: mark it shipped with the tracking number (the customer sees it on Track order and in
    their order), and finish the job. Parts never touched our shelf, so no stock changes here."""
    num = (j.tracking_number or "").strip()
    if not num:
        raise HTTPException(409, "No tracking number is on that order yet.")
    carrier = (j.tracking_carrier or "").strip() or None
    number = str(j.ebay_order_id)[len(WEB_PREFIX):]
    row = db.execute(
        text(
            "update orders set status = 'shipped', shipped_at = now(), shipped_by = :by, tracking_number = :t, carrier = :c "
            "where lower(order_number) = lower(:n) and status = 'paid' and shipped_at is null returning id, order_number"
        ),
        {"by": user.email, "t": num, "c": carrier, "n": number},
    ).first()
    if not row:
        db.rollback()
        raise HTTPException(409, f"Website order {number} is not waiting to ship (it was cancelled or is already shipped).")
    from app import order_activity

    order_activity.log_action(db, user, "tracking_shipped", "Website", row.order_number, f"{carrier + ' ' if carrier else ''}{num}")
    db.execute(text("update dropship_jobs set stage = 'done', problem = null, shipped_at = now(), updated_at = now() where id = :i"), {"i": job_id})
    db.commit()


def _ship_amazon_order(db: Session, job_id: int, j, user: AuthUser) -> None:
    """The distributor shipped an Amazon order: confirm the shipment to Amazon with the tracking number, and finish the job. Needs Live
    pushes ON for the account (amazon_sales.mark_shipped refuses otherwise, and the job then shows the reason)."""
    from app.amazon_sales import ShipIn as AmazonShipIn
    from app.amazon_sales import mark_shipped as amazon_mark_shipped

    num = (j.tracking_number or "").strip()
    if not num:
        raise HTTPException(409, "No tracking number is on that order yet.")
    number = str(j.ebay_order_id)[len(AMAZON_PREFIX):]
    o = db.execute(text("select id from amazon_orders where amazon_order_id = :o order by id desc limit 1"), {"o": number}).first()
    if not o:
        raise HTTPException(404, f"Amazon order {number} is no longer in the list.")
    amazon_mark_shipped(o.id, AmazonShipIn(carrier=(j.tracking_carrier or "Other"), tracking_number=num), db=db, user=user)
    db.execute(text("update dropship_jobs set stage = 'done', problem = null, shipped_at = now(), updated_at = now() where id = :i"), {"i": job_id})
    db.commit()


def _send_tracking_now(db: Session, job_id: int, user: AuthUser) -> None:
    """Tell eBay the order shipped with this job's tracking number (eBay then notifies the buyer), and mark the job done."""
    from app.ebay_sales import ShipIn, mark_shipped

    w = db.execute(
        text("select j.channel, j.ebay_order_id, j.tracking_number, j.tracking_carrier from dropship_jobs j where j.id = :i"), {"i": job_id}
    ).first()
    if w is not None and w.channel == "web":
        return _ship_web_order(db, job_id, w, user)
    if w is not None and w.channel == "amazon":
        return _ship_amazon_order(db, job_id, w, user)
    r = db.execute(
        text("select j.tracking_number, j.tracking_carrier, o.id as order_pk from dropship_jobs j join ebay_orders o on o.ebay_order_id = j.ebay_order_id where j.id = :i"),
        {"i": job_id},
    ).first()
    if not r or not r.tracking_number:
        raise HTTPException(409, "No tracking number is on that order yet.")
    mark_shipped(r.order_pk, ShipIn(carrier=r.tracking_carrier or "Other", tracking_number=r.tracking_number), db=db, user=user)
    db.execute(text("update dropship_jobs set stage = 'done', problem = null, shipped_at = now(), updated_at = now() where id = :i"), {"i": job_id})
    db.commit()


@router.post("/jobs/{job_id}/send-tracking")
@needs(*PERMS)
def send_tracking(job_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """The tracking is correct: tell eBay the order shipped, with the tracking number. eBay then notifies the buyer."""
    j = _job(db, job_id)
    if j.stage != "review_tracking":
        raise HTTPException(409, "That order is not waiting for the tracking check.")
    _send_tracking_now(db, job_id, user)
    return {"ok": True, "stage": "done"}


@router.get("/health")
@needs(*PERMS)
def health(db: Session = Depends(get_db)):
    """Warnings for the top of the Drop ship page: things that stop a ticked listing from running by itself."""
    import os

    from app import autodist

    _ensure(db)
    warnings: list[str] = []
    try:
        ticked = db.execute(
            text(
                "select cl.store, count(*) as n from channel_listings cl join products p on p.id = cl.product_id "
                "where (coalesce(cl.dropship_ok, false) or coalesce(p.dropship_ok, false)) group by cl.store"
            )
        ).all()
    except Exception:
        db.rollback()
        ticked = []
    total = sum(int(r.n) for r in ticked)
    if total:
        # 1. the automatic tracking check
        try:
            autodist._ensure_tracking_settings(db)
            cfg = db.execute(
                text("select tracking_enabled, tracking_interval_minutes from supplier_sync_settings where supplier = :s"), {"s": autodist.SUPPLIER}
            ).first()
            if cfg is None or not cfg.tracking_enabled:
                warnings.append(
                    "The automatic tracking check is OFF (Distributor data feed tab). Tracking will not come back by itself, so eBay will not be told the orders shipped."
                )
        except Exception:
            db.rollback()
        # 2. the eBay order check, store by store
        try:
            accts = {
                (r.store or "").lower(): r
                for r in db.execute(text("select store, sales_polling_enabled, refresh_token_enc is not null as connected from ebay_accounts")).all()
            }
            for r in ticked:
                a = accts.get((r.store or "").lower())
                if a is None or not a.connected:
                    warnings.append(f"{r.store}: the store is not connected to eBay, so its orders are never checked.")
                elif not a.sales_polling_enabled:
                    warnings.append(f"{r.store}: the eBay order check is OFF. New orders will not be seen, so nothing there will drop ship by itself.")
        except Exception:
            db.rollback()
        # 3. ticked but no distributor part
        try:
            from app.availability import linked_sql

            bad = db.execute(
                text(f"select count(*) from products p where (coalesce(p.dropship_ok, false) or exists (select 1 from channel_listings c where c.product_id = p.id and c.dropship_ok)) and not coalesce({linked_sql('p')}, false)")
            ).scalar()
            if bad:
                warnings.append(f"{int(bad)} product(s) are ticked Drop ship but have no distributor part linked. Their orders cannot drop ship; they go to Pick & ship.")
        except Exception:
            db.rollback()
        # 4. real orders switched off
        try:
            url, _key = autodist._api_settings()
            if autodist._api_mode(url) == "live" and os.environ.get("AUTOMATIC_DIST_ALLOW_LIVE_ORDERS", "").strip().lower() not in ("1", "true", "yes"):
                warnings.append("This is the live server and real orders are switched off. Drop ship orders will stop at the order step until AUTOMATIC_DIST_ALLOW_LIVE_ORDERS=1 is set on Render.")
        except Exception:
            db.rollback()
    return {"ticked": total, "warnings": warnings}


class CarrierTrackingIn(BaseModel):
    carrier: str = Field(..., min_length=2, max_length=20)
    tracking_number: str = Field(..., min_length=6, max_length=80)


@router.post("/jobs/{job_id}/tracking")
@needs(*PERMS)
def enter_tracking(job_id: int, body: CarrierTrackingIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """For an order the distributor shipped by freight (or whose tracking never came back): a person types the carrier and tracking
    number, and eBay is told it shipped with them."""
    import re

    j = _job(db, job_id)
    if j.stage not in ("placed", "review_tracking"):
        raise HTTPException(409, "That order has not been placed with the distributor, or is already finished.")
    num = re.sub(r"\s+", "", body.tracking_number)
    db.execute(
        text("update dropship_jobs set tracking_number = :t, tracking_carrier = :c, stage = 'review_tracking', problem = null, updated_at = now() where id = :i"),
        {"t": num, "c": body.carrier.strip(), "i": job_id},
    )
    db.commit()
    try:
        _send_tracking_now(db, job_id, user)
    except HTTPException as e:
        db.rollback()
        db.execute(text("update dropship_jobs set problem = :m where id = :i"), {"m": f"The order did not accept that tracking: {str(e.detail)[:300]}", "i": job_id})
        db.commit()
        raise
    return {"ok": True, "stage": "done"}


def _web_ship_from_stock(db: Session, j, user: AuthUser) -> dict:
    """Website version of the way out: cancel the PO if there is one, set the parts aside on our shelf again, and leave the order to be
    picked normally."""
    from app import fulfillment
    from app.admin_purchase_orders import CancelIn, cancel_po
    from app.stock import lock_products

    number = str(j.ebay_order_id)[len(WEB_PREFIX):]
    if j.po_id:
        st = db.execute(text("select status from po_headers where id = :p"), {"p": j.po_id}).scalar()
        if st and st != "cancelled":
            cancel_po(j.po_id, CancelIn(reason=f"Shipping website order {number} from our own stock instead"), db=db, user=user)
    lines = db.execute(
        text(
            "select oi.product_id, sum(oi.quantity) as q from order_items oi join orders o on o.id = oi.order_id "
            "where lower(o.order_number) = lower(:n) and o.status = 'paid' and oi.product_id is not null "
            "and not exists (select 1 from stock_reservations r where r.order_id = oi.order_id and r.product_id = oi.product_id) "
            "group by oi.product_id"
        ),
        {"n": number},
    ).all()
    if lines:
        oid = db.execute(text("select id from orders where lower(order_number) = lower(:n)"), {"n": number}).scalar()
        lock_products(db, [int(l.product_id) for l in lines])
        for l in lines:
            db.execute(
                text("insert into stock_reservations (order_id, product_id, quantity, status, expires_at) values (:o, :p, :q, 'reserved', null)"),
                {"o": oid, "p": int(l.product_id), "q": int(l.q)},
            )
        try:
            from app.ebay_sales import queue_stock_push

            queue_stock_push(db, [int(l.product_id) for l in lines])
        except Exception:
            log.exception("could not queue quantity updates after moving website order %s to our own stock", number)
    fulfillment.record_decision(db, "Website", number, "stock", user.email, ["Buyer paid for shipping; shipping from our own stock" if j.stage == "review_shipping" else "Could not be drop shipped; shipping from our own stock"])
    db.execute(text("update dropship_jobs set stage = 'dismissed', updated_at = now() where id = :i"), {"i": j.id})
    db.commit()
    return {"ok": True}


@router.post("/jobs/{job_id}/ship-from-stock")
@needs(*PERMS)
def ship_from_stock(job_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """The way out for an order that cannot be drop shipped (Global Shipping Program, incomplete address, no matching shipping option,
    a refused order): cancel its PO if it has one, put its lines back on our shelf, and hand the order to Pick & ship as 'ship from
    our own stock'. Not possible once the distributor has the order."""
    from app import fulfillment
    from app.admin_purchase_orders import CancelIn, cancel_po
    from app.ebay_sales import undo_lines_dropship

    j = db.execute(text("select id, stage, po_id, ebay_order_id, channel from dropship_jobs where id = :i"), {"i": job_id}).first()
    if not j:
        raise HTTPException(404, "That drop ship order is not in the list.")
    if j.stage in ("placing", "placed", "review_tracking", "done", "dismissed"):
        raise HTTPException(409, "The distributor already has this order, so it cannot be moved to our own stock from here.")
    if j.channel == "web":
        return _web_ship_from_stock(db, j, user)
    if j.channel == "amazon":
        from app import amazon_sales

        number = str(j.ebay_order_id)[len(AMAZON_PREFIX):]
        if j.po_id:
            st = db.execute(text("select status from po_headers where id = :p"), {"p": j.po_id}).scalar()
            if st and st != "cancelled":
                cancel_po(j.po_id, CancelIn(reason=f"Shipping Amazon order {number} from our own stock instead"), db=db, user=user)
        amazon_sales.undo_lines_dropship(db, number)
        fulfillment.record_decision(db, "Amazon", number, "stock", user.email, ["Buyer paid for shipping; shipping from our own stock" if j.stage == "review_shipping" else "Could not be drop shipped; shipping from our own stock"])
        db.execute(text("update dropship_jobs set stage = 'dismissed', updated_at = now() where id = :i"), {"i": job_id})
        db.commit()
        return {"ok": True}
    pids: list[int] = []
    if j.po_id:
        pids = [int(r.product_id) for r in db.execute(text("select product_id from po_lines where po_id = :p and product_id is not null"), {"p": j.po_id}).all()]
        st = db.execute(text("select status from po_headers where id = :p"), {"p": j.po_id}).scalar()
        if st and st != "cancelled":
            cancel_po(j.po_id, CancelIn(reason=f"Shipping eBay order {j.ebay_order_id} from our own stock instead"), db=db, user=user)
    if pids:
        undo_lines_dropship(db, j.ebay_order_id, pids)
    fulfillment.record_decision(db, "eBay", j.ebay_order_id, "stock", user.email, ["Buyer paid for shipping; shipping from our own stock" if j.stage == "review_shipping" else "Could not be drop shipped; shipping from our own stock"])
    db.execute(text("update dropship_jobs set stage = 'dismissed', updated_at = now() where id = :i"), {"i": job_id})
    db.commit()
    return {"ok": True}


@router.post("/jobs/{job_id}/hold")
@needs(*PERMS)
def hold_job(job_id: int, db: Session = Depends(get_db)):
    j = _job(db, job_id)
    if j.stage in ("held", "dismissed"):
        return {"ok": True, "stage": j.stage}
    db.execute(text("update dropship_jobs set stage = 'held', updated_at = now() where id = :i"), {"i": job_id})
    db.commit()
    return {"ok": True, "stage": "held"}


class AmazonAddressIn(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=120)
    line1: str = Field(..., min_length=1, max_length=120)
    line2: str = Field("", max_length=120)
    city: str = Field(..., min_length=1, max_length=80)
    region: str = Field(..., min_length=2, max_length=40)
    postal_code: str = Field(..., min_length=3, max_length=12)
    country: str = Field("US", max_length=2)
    phone: str = Field("", max_length=30)


@router.post("/jobs/{job_id}/amazon-address")
@needs(*PERMS)
def amazon_address(job_id: int, body: AmazonAddressIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """Staff pasted the ship-to from Seller Central for an Amazon order that stopped for lack of an address: replace the problem job
    with a real one (PO + job). The address goes onto the PO only, like every other buyer address."""
    _job(db, job_id)
    j = db.execute(text("select id, stage, ebay_order_id, store, channel from dropship_jobs where id = :i"), {"i": job_id}).first()
    if j.channel != "amazon" or j.stage != "problem":
        raise HTTPException(409, "Only an Amazon order that needs attention can be given an address.")
    key = str(j.ebay_order_id)
    order_id = key[len(AMAZON_PREFIX):]
    picks = _amazon_flagged_lines(db, order_id)
    if not picks:
        raise HTTPException(409, "Nothing on this order is set to drop ship any more. Ship it from our own stock, or make the PO by hand.")
    db.execute(text("delete from dropship_jobs where id = :i and stage = 'problem'"), {"i": job_id})
    db.commit()
    res = _make_amazon_job(db, order_id, j.store, picks, user, ship_to=body.model_dump())
    if res.get("problem"):
        raise HTTPException(409, res["problem"])
    return {"ok": True, **res}


@router.post("/jobs/{job_id}/retry")
@needs(*PERMS)
def retry_job(job_id: int, db: Session = Depends(get_db)):
    j = _job(db, job_id)
    if j.stage != "problem":
        raise HTTPException(409, "Only an order that needs attention can be tried again.")
    db.execute(text("delete from dropship_jobs where id = :i"), {"i": job_id})
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# activity: what drop shipped in a time frame (built to stay quick with thousands of orders a day: every number is counted
# in the database over an indexed time window, and the list is paged)
# --------------------------------------------------------------------------
_PLACED = "coalesce(j.placed_at, case when j.stage in ('placed', 'review_tracking', 'done') then j.updated_at end)"
_SHIPPED = "coalesce(j.shipped_at, case when j.stage = 'done' then j.updated_at end)"
_BASIS = {"received": "j.created_at", "placed": _PLACED, "shipped": _SHIPPED}
_WAITING_STAGES = ("review_po", "po_confirmed", "review_quote", "placing", "placed", "review_tracking", "held", "return_needed", "problem")


def _when(raw: str, default):
    from datetime import datetime, timezone

    if not (raw or "").strip():
        return default
    try:
        d = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "The dates must look like 2026-11-15T09:30:00Z.")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@router.get("/activity")
@needs(*PERMS)
def activity(
    since: str = "",
    until: str = "",
    basis: str = Query("received", pattern="^(received|placed|shipped)$"),
    stage: str = "",
    q: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Drop ship orders in a time frame. since / until are ISO date-times (the page sends its own local day and hour boundaries).
    basis says which moment decides if an order is in the frame: received (the eBay order reached us), placed (we placed it with the
    distributor) or shipped (eBay was told it shipped). The summary counts all three for the frame, plus what is waiting right now."""
    from datetime import datetime, timedelta, timezone

    _ensure(db)
    now = datetime.now(timezone.utc)
    until_d = _when(until, now)
    since_d = _when(since, until_d - timedelta(hours=24))
    if since_d >= until_d:
        raise HTTPException(422, "The start of the time frame must be before its end.")
    params = {"s": since_d, "u": until_d}
    # the three moments all fall within weeks of an order being received, which keeps every scan on the indexed column
    bound = "j.created_at >= cast(:s as timestamptz) - interval '90 days' and j.stage <> 'dismissed'"
    summ = db.execute(
        text(
            f"""
            select count(*) filter (where j.created_at >= :s and j.created_at < :u) as received,
                   count(*) filter (where {_PLACED} >= :s and {_PLACED} < :u) as placed,
                   count(*) filter (where {_SHIPPED} >= :s and {_SHIPPED} < :u) as shipped,
                   count(*) filter (where j.stage = 'problem' and j.updated_at >= :s and j.updated_at < :u) as problems
            from dropship_jobs j where {bound}
            """
        ),
        params,
    ).first()
    waiting = {
        r.stage: int(r.n)
        for r in db.execute(
            text("select stage, count(*) as n from dropship_jobs where stage = any(cast(:st as text[])) group by stage"),
            {"st": list(_WAITING_STAGES)},
        ).all()
    }
    col = _BASIS[basis]
    where = [bound, f"{col} >= :s", f"{col} < :u"]
    if stage.strip():
        where.append("j.stage = :stage")
        params["stage"] = stage.strip()
    if q.strip():
        where.append(
            "(j.ebay_order_id ilike :like or j.buyer_name ilike :like or cast(coalesce(h.po_number, j.po_number) as text) ilike :like "
            "or j.supplier_order ilike :like or j.tracking_number ilike :like)"
        )
        params["like"] = f"%{q.strip()}%"
    w = " and ".join(where)
    frm = "from dropship_jobs j left join po_headers h on h.id = j.po_id"
    total = db.execute(text(f"select count(*) {frm} where {w}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            select j.id, j.ebay_order_id, j.store, coalesce(h.po_number, j.po_number) as po_number, j.stage, j.buyer_name, j.ship_service,
                   j.supplier_order, j.supplier_mode, j.tracking_number, j.tracking_carrier, j.problem, j.created_at,
                   {_PLACED} as placed_at, {_SHIPPED} as shipped_at,
                   case when j.channel = 'web' then (select coalesce(sum(oi.quantity), 0) from order_items oi where oi.order_id = j.web_order_id)
                        when j.channel = 'amazon' then (select coalesce(sum(l.quantity * coalesce(l.pieces_per_sale, 1)), 0) from amazon_order_lines l where 'amazon:' || l.amazon_order_id = j.ebay_order_id)
                        else (select coalesce(sum({line_units_sql('l')}), 0) from ebay_order_lines l where l.ebay_order_id = j.ebay_order_id) end as units,
                   case when j.channel = 'web' then (select min(coalesce(oi.title, '')) from order_items oi where oi.order_id = j.web_order_id)
                        when j.channel = 'amazon' then (select min(coalesce(l.title, '')) from amazon_order_lines l where 'amazon:' || l.amazon_order_id = j.ebay_order_id)
                        else (select min(coalesce(l.title, '')) from ebay_order_lines l where l.ebay_order_id = j.ebay_order_id) end as title,
                   j.channel
            {frm} where {w} order by {col} desc nulls last, j.id desc limit :lim offset :off
            """
        ),
        {**params, "lim": limit, "off": offset},
    ).all()
    return {
        "since": since_d,
        "until": until_d,
        "basis": basis,
        "summary": {
            "received": int(summ.received or 0), "placed": int(summ.placed or 0), "shipped": int(summ.shipped or 0),
            "problems": int(summ.problems or 0), "waiting": waiting,
        },
        "total": int(total or 0),
        "rows": [dict(r._mapping) for r in rows],
    }
