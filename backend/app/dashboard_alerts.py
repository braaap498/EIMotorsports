"""
Dashboard messages: the few things that need a person's eyes, shown across the top of the admin Dashboard in colors by importance.

  GET /admin/dashboard/alerts   [{id, level, title, detail, href, items: [{label, text, href}]}], most important first

LEVELS (the colors on the Dashboard)
  critical  red     money or customers are at risk right now (an order that needs a decision, drop ship stopped, a kit that cannot be made)
  warning   orange  something is getting low or out of date; fix it soon
  notice    yellow  worth knowing
  info      blue    the guard rail or another automatic rule did something

EVERY check is independent: one that fails (a table that does not exist yet, a slow query) is logged and skipped, it never hides the others.
Nothing here changes data.

THE KIT-PART MESSAGE
  A kit is made from our own single-part products. When ONE of its parts is getting low on our shelf (the kits we can still make from that part
  is at or under "alert_kit_own_max", default 3) AND the distributor has few left too (kits' worth at Indiana + Nevada at or under
  "alert_kit_dist_max", default 10), the kit is about to run out with no easy way to refill it. Both numbers are settings on the Drop ship page.
  It goes RED when the kit can already not be made and the distributor has none of that part either.
"""
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import needs, require_route_access
from app.availability import (
    SUPPLIER,
    _ACTIVE_LISTING,
    _int_setting,
    available_sql,
    dist_sql,
    dist_usable_sql,
)
from app.database import get_db
from app.stock import same_part_sql, set_aside_sql

log = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["admin-dashboard"], dependencies=[Depends(require_route_access)])

ORDER = {"critical": 0, "warning": 1, "notice": 2, "info": 3}
READ = ("products.view", "inventory.ship", "inventory.purchase")
MAX_ITEMS = 8


def _alert(id_: str, level: str, title: str, detail: str = "", href: Optional[str] = None, items: Optional[list] = None) -> dict:
    return {"id": id_, "level": level, "title": title, "detail": detail, "href": href, "items": (items or [])[:MAX_ITEMS], "more": max(0, len(items or []) - MAX_ITEMS)}


def _urgent_orders(db: Session) -> Optional[dict]:
    from app import fulfillment

    found = fulfillment.urgent_orders(db)
    if not found:
        return None
    items = [{"label": k.replace("|", " "), "text": (v[0] if v else "")[:220], "href": "/admin/inventory"} for k, v in sorted(found.items())]
    n = len(found)
    return _alert(
        "urgent_orders", "critical",
        f"{n} order{'s' if n != 1 else ''} need{'' if n != 1 else 's'} a decision now",
        "Paid orders our shelf cannot fill, or that take a big share of what the distributor has. Choose drop ship or ship from our stock on Pick & ship.",
        "/admin/inventory", items,
    )


def _dropship_problems(db: Session) -> Optional[dict]:
    rows = db.execute(
        text(
            "select ebay_order_id, stage, problem, channel from dropship_jobs "
            "where stage not in ('done', 'dismissed') and (problem is not null or stage in ('problem', 'return_needed')) "
            "order by updated_at desc limit 40"
        )
    ).all()
    if not rows:
        return None
    items = [
        {"label": f"{r.channel or 'ebay'} {r.ebay_order_id}", "text": (r.problem or r.stage or "")[:220], "href": "/admin/dropship"}
        for r in rows
    ]
    n = len(rows)
    return _alert(
        "dropship_problems", "critical",
        f"{n} drop ship order{'s' if n != 1 else ''} stopped and need{'' if n != 1 else 's'} you",
        "The automatic chain stopped on these and will not retry by itself. Open Drop ship > Orders for the reason and the way out.",
        "/admin/dropship", items,
    )


def _dropship_health(db: Session) -> Optional[dict]:
    from app import dropship_auto

    h = dropship_auto.health(db=db)
    warns = h.get("warnings") or []
    if not warns:
        return None
    return _alert(
        "dropship_health", "critical", "Automatic drop shipping will not run by itself",
        f"{len(warns)} thing{'s' if len(warns) != 1 else ''} to fix on the Drop ship page.", "/admin/dropship",
        [{"label": "", "text": w, "href": "/admin/dropship"} for w in warns],
    )


def _feed_stale(db: Session) -> Optional[dict]:
    """The distributor stock numbers come from a feed. When it stops, every listing keeps offering stale numbers."""
    r = db.execute(
        text(
            "select max(last_seen_at) as seen, extract(epoch from (now() - max(last_seen_at))) / 3600.0 as age_h "
            "from supplier_items where supplier = :s and active"
        ),
        {"s": SUPPLIER},
    ).first()
    if not r or r.age_h is None:
        return None
    limit = float(os.environ.get("FULFILL_FEED_MAX_HOURS", "48") or 48)
    age = float(r.age_h)
    if age < 24:
        return None
    level = "critical" if age >= limit else "warning"
    return _alert(
        "feed_stale", level, f"Distributor stock is {int(age)} hours old",
        "Listings are being sized from the last stock feed that came in. Check the Distributor data feed tab (the automatic stock sync). "
        f"Orders treat anything older than {int(limit)} hours as unknown and fall back to our own shelf.",
        "/admin/distributors/autodist",
    )


def _qty_push_failed(db: Session) -> Optional[dict]:
    rows = db.execute(
        text(
            "select store, ebay_item_id, last_error from ebay_qty_queue "
            "where status = 'failed' and done_at > now() - interval '3 days' order by done_at desc limit 40"
        )
    ).all()
    if not rows:
        return None
    items = [{"label": f"{r.store} {r.ebay_item_id}", "text": (r.last_error or "")[:200], "href": "/admin/ebay-listings"} for r in rows]
    n = len(rows)
    return _alert(
        "qty_push_failed", "warning", f"eBay refused {n} quantity update{'s' if n != 1 else ''} in the last 3 days",
        "Those listings may still show the old number on eBay, which is how an oversell happens. Look at the reason and fix or set them by hand.",
        "/admin/ebay-listings", items,
    )


def _kit_parts_low(db: Session) -> Optional[dict]:
    own_max = _int_setting(db, "alert_kit_own_max", 3)
    dist_max = _int_setting(db, "alert_kit_dist_max", 10)
    rows = db.execute(
        text(
            "select p.id, p.sku_display as sku, p.title, k.part_number, greatest(coalesce(k.quantity, 1), 1) as need, "
            "coalesce((select sum(greatest(0, coalesce(p2.quantity_on_hand, 0) - " + set_aside_sql("p2") + ") * greatest(coalesce(k2.quantity, 1), 1)) "
            "  from product_kit_parts k2 join products p2 on p2.id = k2.product_id "
            f"  where {same_part_sql('k', 'k2')} and p2.id <> p.id "
            "    and (select count(*) from product_kit_parts k3 where k3.product_id = p2.id) = 1), 0) as own_units, "
            "(si.id is not null and coalesce(si.active, false)) as linked, "
            "coalesce(si.qty_indiana, 0) as ind, coalesce(si.qty_nevada, 0) as nev "
            "from products p join product_kit_parts k on k.product_id = p.id "
            f"left join supplier_items si on si.id = k.supplier_item_id and si.supplier = '{SUPPLIER}' "
            "where p.id in (select product_id from product_kit_parts group by product_id having count(*) >= 2) "
            f"and (coalesce(p.is_published, false) or exists (select 1 from channel_listings cl where cl.product_id = p.id and {_ACTIVE_LISTING})) "
            "order by p.id, k.sort_order, k.id"
        )
    ).all()
    kits: dict[int, dict] = {}
    for r in rows:
        need = int(r.need)
        own_kits = int(r.own_units) // need
        dist_units = (int(r.ind) + int(r.nev)) if r.linked else 0
        dist_kits = dist_units // need
        if own_kits > own_max or dist_kits > dist_max:
            continue
        k = kits.setdefault(r.id, {"sku": r.sku, "title": r.title, "parts": [], "dead": False, "worst": 10**9})
        dead = own_kits == 0 and dist_kits == 0
        k["dead"] = k["dead"] or dead
        k["worst"] = min(k["worst"], own_kits)
        k["parts"].append(
            f"{r.part_number}: our shelf makes {own_kits} kit{'s' if own_kits != 1 else ''}, "
            + (f"the distributor has {dist_units} ({dist_kits} kit{'s' if dist_kits != 1 else ''}' worth)" if r.linked else "not matched to the distributor")
        )
    if not kits:
        return None
    ordered = sorted(kits.items(), key=lambda kv: (not kv[1]["dead"], kv[1]["worst"], kv[1]["sku"] or ""))
    items = [
        {"label": (v["sku"] or "") + (" · " + v["title"][:60] if v["title"] else ""), "text": "; ".join(v["parts"])[:260], "href": f"/admin/products/{pid}"}
        for pid, v in ordered
    ]
    dead = sum(1 for _, v in ordered if v["dead"])
    n = len(ordered)
    return _alert(
        "kit_parts_low", "critical" if dead else "warning",
        f"{n} kit{'s' if n != 1 else ''} running out of a part"
        + (f" ({dead} cannot be made and the distributor has none)" if dead else ""),
        f"A part is down to {own_max} kit{'s' if own_max != 1 else ''} or fewer on our shelf and the distributor has {dist_max} or fewer kits' worth. "
        "Reorder the part before the kit's listings go to 0.",
        None, items,
    )


def _guard_rail_holds(db: Session) -> Optional[dict]:
    """Drop ship products where the distributor cannot fill a sale (empty, or too little left after the safety margin). Drop ship is
    paused for them: the listing sells from OUR shelf, or shows 0 when we have none either. It resumes by itself when the distributor restocks."""
    flagged = f"(p.dropship_ok or exists (select 1 from channel_listings c2 where c2.product_id = p.id and c2.dropship_ok and lower(coalesce(c2.status, '')) = 'active'))"
    rows = db.execute(
        text(
            f"select p.id, p.sku_display as sku, p.title, {dist_sql('p')} as raw, {available_sql('p')} as own, count(*) over () as n_all "
            f"from products p where {flagged} "
            f"and exists (select 1 from channel_listings cl where cl.product_id = p.id and {_ACTIVE_LISTING}) "
            f"and {dist_usable_sql('p')} <= 0 order by (({available_sql('p')}) > 0) desc, p.title limit 60"
        )
    ).all()
    if not rows:
        return None
    items = []
    for r in rows:
        why = f"the distributor shows {int(r.raw)}, below the safety line" if int(r.raw) > 0 else "the distributor is out"
        items.append(
            {
                "label": (r.sku or "") + (" · " + r.title[:60] if r.title else ""),
                "text": why + "; " + (f"drop ship paused, selling from our shelf ({int(r.own)})" if int(r.own) > 0 else "nothing on our shelf either, the listing is at 0"),
                "href": f"/admin/products/{r.id}",
            }
        )
    n = int(rows[0].n_all)
    return _alert(
        "guard_rail", "info", f"Drop ship is paused on {n} product{'s' if n != 1 else ''}: the distributor cannot fill a sale",
        "Those listings sell from our own shelf (or show 0 when we have none) and orders ship from us. They go back to the distributor by themselves when it restocks.",
        "/admin/dropship", items,
    )


CHECKS = (_urgent_orders, _dropship_problems, _dropship_health, _kit_parts_low, _feed_stale, _qty_push_failed, _guard_rail_holds)


@router.get("/alerts")
@needs(*READ)
def alerts(db: Session = Depends(get_db)):
    out: list[dict] = []
    for fn in CHECKS:
        try:
            a = fn(db)
            if a:
                out.append(a)
        except Exception:
            db.rollback()
            log.exception("dashboard check %s failed", fn.__name__)
    out.sort(key=lambda a: ORDER.get(a["level"], 9))
    return {"alerts": out}
