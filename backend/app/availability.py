"""
What is AVAILABLE to sell, counting the distributor's stock as well as our own shelf. Mounted at /admin/availability/* (admin only).

THE RULE (only when the master switch "Count distributor stock" is ON; while it is OFF every number is the shelf count minus what
is set aside, exactly as before)
  own          quantity_on_hand minus parts set aside (app/stock.py), never below 0
  distributor  Automatic Distributors Indiana + Nevada stock for the product's parts (product_kit_parts). With several pieces
               the scarcest piece decides: min over the pieces of floor(stock / pieces needed per product)
  a SINGLE-part product   available = own + distributor
  a KIT (more than one piece) is made from our own shelf, so available = own only, UNLESS drop shipping is allowed for it:
                          products.dropship_ok = all its listings, channel_listings.dropship_ok = just that one listing.
                          Then available = own + distributor (the scarcest piece).
  A piece with no live catalog item (not linked, or the item is inactive) counts as 0 distributor stock.

WHO USES IT
  eBay quantities       ebay_sales.enqueue_product / run_queue (the same queue that already follows sales), plus relist and restore-from-0
  the website           product page and cart: stock_mode (Available / "35 Available") and out of stock; checkout may take up to this
  reconcile()           a timer (every few minutes, while the switch is ON) that notices distributor stock changes and queues the
                        listings whose number went DOWN (to 0 included). Numbers going back UP are only queued when "raise listings
                        automatically" is ON. The first look at a product only records its number (a baseline), so turning the switch
                        on never pushes anything by itself: use "Set listings to 0 now" after reviewing the At 0 list.

  GET  /availability/settings                 the switches
  PUT  /availability/settings                 change them
  GET  /availability/zero                     what is at 0 now (and why), with the drop ship overrides
  POST /availability/products/{id}/dropship   allow / stop drop shipping for every listing of a kit
  POST /availability/listings/{id}/dropship   allow / stop drop shipping for one listing
  POST /availability/apply-zeros              queue an eBay quantity update for everything at 0 now
  GET  /availability/display/products         website display setting of products (search)
  POST /availability/display                  set the website display (Available / count / store default) for all or chosen products
"""
import logging
import os
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin, require_route_access
from app.database import _session_factory, get_db
from app.stock import available_sql

log = logging.getLogger(__name__)
router = APIRouter(prefix="/availability", tags=["admin-availability"], dependencies=[Depends(require_route_access)])

SUPPLIER = "autodist"
INTERVAL_SECONDS = 600
DISPLAY_MODES = ("available", "count")


# --------------------------------------------------------------------------
# settings + columns
# --------------------------------------------------------------------------
def ensure_tables() -> None:
    """Called once at startup. Everything starts OFF / "Available" and every product follows the store default."""
    db = _session_factory()()
    try:
        for stmt in (
            "alter table products add column if not exists stock_display text",
            "alter table products add column if not exists dropship_ok boolean not null default false",
            "alter table channel_listings add column if not exists dropship_ok boolean not null default false",
            "create table if not exists availability_settings (key text primary key, value text not null)",
            "create table if not exists availability_state ("
            "product_id bigint primary key, effective integer not null, updated_at timestamptz not null default now())",
        ):
            db.execute(text(stmt))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not set up the availability tables; counting distributor stock will not work until they exist")
    finally:
        db.close()


def _setting_sql(key: str, default: str) -> str:
    return f"coalesce((select s.value from availability_settings s where s.key = '{key}'), '{default}')"


def get_setting(db: Session, key: str, default: str) -> str:
    v = db.execute(text("select value from availability_settings where key = :k"), {"k": key}).scalar()
    return v if v is not None else default


def set_setting(db: Session, key: str, value: str) -> None:
    db.execute(
        text("insert into availability_settings (key, value) values (:k, :v) on conflict (key) do update set value = excluded.value"),
        {"k": key, "v": value},
    )


def switch_on(db: Session) -> bool:
    try:
        return get_setting(db, "use_distributor_stock", "off") == "on"
    except Exception:
        db.rollback()
        return False


# --------------------------------------------------------------------------
# the counting, as SQL (one place; change the rule here)
# --------------------------------------------------------------------------
def parts_count_sql(p: str = "p") -> str:
    return f"(select count(*) from product_kit_parts k where k.product_id = {p}.id)"


def dist_sql(p: str = "p") -> str:
    """Distributor stock for product `p`: the scarcest piece decides. 0 when there are no pieces or a piece has no live catalog item."""
    return (
        "(select case when count(*) = 0 or bool_or(si.id is null) then 0 else "
        "coalesce(min(cast(floor(cast(coalesce(si.qty_indiana, 0) + coalesce(si.qty_nevada, 0) as numeric) "
        "/ greatest(coalesce(k.quantity, 1), 1)) as integer)), 0) end "
        "from product_kit_parts k "
        f"left join supplier_items si on si.id = k.supplier_item_id and si.supplier = '{SUPPLIER}' and si.active "
        f"where k.product_id = {p}.id)"
    )


def extra_sql(p: str = "p", ovr: str = "false", ignore_switch: bool = False) -> str:
    """What the distributor adds on top of our own count for product `p` (0 when the switch is off or the rule says no).
    `ovr` is an SQL boolean for a listing-level drop ship override, e.g. "coalesce(cl.dropship_ok, false)"."""
    inner = f"(case when {parts_count_sql(p)} = 1 or {p}.dropship_ok or {ovr} then {dist_sql(p)} else 0 end)"
    if ignore_switch:
        return inner
    return f"(case when {_setting_sql('use_distributor_stock', 'off')} = 'on' then {inner} else 0 end)"


def effective_sql(p: str = "p", ovr: str = "false", ignore_switch: bool = False) -> str:
    """SQL: what can be sold from product `p` now (own count, plus the distributor's where the rule allows)."""
    return f"({available_sql(p)} + {extra_sql(p, ovr, ignore_switch)})"


def stock_mode_sql(p: str = "p") -> str:
    """SQL: how the website shows stock for product `p`: 'available' or 'count'."""
    return f"coalesce(nullif({p}.stock_display, ''), {_setting_sql('stock_display_default', 'available')})"


# --------------------------------------------------------------------------
# helpers for the other modules
# --------------------------------------------------------------------------
def extra_stock(db: Session, ids) -> dict[int, int]:
    """{product id: units the distributor adds} for the checkout (0 everywhere while the switch is off)."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return {}
    try:
        rows = db.execute(
            text(f"select p.id, {extra_sql('p')} as e from products p where p.id in :ids").bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ).all()
        return {r.id: max(0, int(r.e or 0)) for r in rows}
    except Exception:
        db.rollback()
        log.exception("could not read distributor stock for the checkout")
        return {}


def listing_override(db: Session, store: str, item_id: str) -> bool:
    """Is drop shipping allowed for this one listing?"""
    try:
        return bool(
            db.execute(
                text(
                    "select coalesce(cl.dropship_ok, false) from channel_listings cl "
                    "where regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = :i and lower(cl.store) = lower(:s) limit 1"
                ),
                {"i": item_id, "s": store},
            ).scalar()
        )
    except Exception:
        db.rollback()
        return False


def effective(db: Session, product_id: int, listing_dropship: bool = False) -> int:
    return int(
        db.execute(
            text(f"select {effective_sql('p', 'true' if listing_dropship else 'false')} from products p where p.id = :p"),
            {"p": product_id},
        ).scalar()
        or 0
    )


# --------------------------------------------------------------------------
# settings routes
# --------------------------------------------------------------------------
class SettingsIn(BaseModel):
    use_distributor_stock: Optional[bool] = None
    auto_raise: Optional[bool] = None
    display_default: Optional[str] = Field(None, pattern="^(available|count)$")


def _settings(db: Session) -> dict:
    return {
        "use_distributor_stock": get_setting(db, "use_distributor_stock", "off") == "on",
        "auto_raise": get_setting(db, "auto_raise", "off") == "on",
        "display_default": get_setting(db, "stock_display_default", "available"),
    }


@router.get("/settings")
def api_get_settings(db: Session = Depends(get_db)):
    return _settings(db)


@router.put("/settings")
def api_put_settings(body: SettingsIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    if body.use_distributor_stock is not None:
        set_setting(db, "use_distributor_stock", "on" if body.use_distributor_stock else "off")
    if body.auto_raise is not None:
        set_setting(db, "auto_raise", "on" if body.auto_raise else "off")
    if body.display_default is not None:
        set_setting(db, "stock_display_default", body.display_default)
    db.commit()
    return _settings(db)


# --------------------------------------------------------------------------
# the At 0 list
# --------------------------------------------------------------------------
_ACTIVE_LISTING = "lower(coalesce(cl.status, '')) = 'active'"


@router.get("/zero")
def api_zero(
    scope: str = Query("listings", pattern="^(listings|website|all)$"),
    store: Optional[str] = Query(None, max_length=80),
    q: Optional[str] = Query(None, max_length=120),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    """Products that are at 0 under the rule above (always shown with the distributor counted, even while the switch is OFF),
    that have an active eBay listing (scope listings), are published on the website (website), or either (all)."""
    where = [f"{effective_sql('p', ignore_switch=True)} <= 0"]
    params: dict = {}
    listing_cond = f"exists (select 1 from channel_listings cl where cl.product_id = p.id and {_ACTIVE_LISTING}"
    if store:
        listing_cond += " and lower(cl.store) = lower(:store)"
        params["store"] = store
    listing_cond += ")"
    if scope == "listings":
        where.append(listing_cond)
    elif scope == "website":
        where.append("p.is_published = true")
    else:
        where.append(f"({listing_cond} or p.is_published = true)")
    if q and q.strip():
        for i, w in enumerate(q.split()[:5]):
            where.append(f"(p.title ilike :w{i} or p.sku_display ilike :w{i} or p.custom_label_sku ilike :w{i} or p.manufacturer_part_number ilike :w{i})")
            params[f"w{i}"] = f"%{w}%"
    cond = " and ".join(where)
    total = int(db.execute(text(f"select count(*) from products p where {cond}"), params).scalar() or 0)
    rows = db.execute(
        text(
            f"select p.id, p.sku_display as sku, p.title, p.custom_label_sku, coalesce(p.quantity_on_hand, 0) as on_hand, "
            f"{available_sql('p')} as own, {parts_count_sql('p')} as parts, {dist_sql('p')} as distributor, "
            f"p.dropship_ok, coalesce(p.is_published, false) as published "
            f"from products p where {cond} order by p.title, p.id limit :lim offset :off"
        ),
        {**params, "lim": per_page, "off": (page - 1) * per_page},
    ).all()
    ids = [r.id for r in rows]
    parts: dict[int, list] = {}
    listings: dict[int, list] = {}
    if ids:
        for k in db.execute(
            text(
                "select k.product_id, k.part_number, coalesce(k.quantity, 1) as q, si.id as si_id, si.active, "
                "coalesce(si.qty_indiana, 0) as ind, coalesce(si.qty_nevada, 0) as nev "
                "from product_kit_parts k left join supplier_items si on si.id = k.supplier_item_id and si.supplier = :s "
                "where k.product_id in :ids order by k.product_id, k.sort_order, k.id"
            ).bindparams(bindparam("ids", expanding=True)),
            {"s": SUPPLIER, "ids": ids},
        ).all():
            parts.setdefault(k.product_id, []).append(
                {"part_number": k.part_number, "per_product": int(k.q), "in_catalog": k.si_id is not None and bool(k.active),
                 "distributor_stock": int(k.ind) + int(k.nev)}
            )
        for l in db.execute(
            text(
                "select cl.id, cl.product_id, cl.store, regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') as item_id, "
                f"coalesce(cl.dropship_ok, false) as dropship_ok from channel_listings cl where cl.product_id in :ids and {_ACTIVE_LISTING} "
                "order by cl.store, cl.id"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ).all():
            listings.setdefault(l.product_id, []).append(
                {"listing_id": l.id, "store": l.store, "item_id": l.item_id, "dropship_ok": bool(l.dropship_ok)}
            )
    out = []
    for r in rows:
        pr = parts.get(r.id, [])
        if r.parts == 0:
            why = "Not linked to a distributor part, and nothing on our shelf."
        elif r.parts > 1 and not r.dropship_ok:
            why = "Kit made from our own shelf, and none is left. Drop shipping is not allowed for it."
        elif any(not x["in_catalog"] for x in pr):
            why = "Nothing on our shelf, and a piece is not in the live distributor catalog."
        else:
            why = "Nothing on our shelf and the distributor has none (the scarcest piece decides)."
        out.append(
            {
                "product_id": r.id, "sku": r.sku, "title": r.title, "custom_label": r.custom_label_sku,
                "on_hand": int(r.on_hand), "own_available": int(r.own), "distributor": int(r.distributor),
                "piece_count": int(r.parts), "dropship_ok": bool(r.dropship_ok), "published": bool(r.published),
                "reason": why, "pieces": pr, "listings": listings.get(r.id, []),
            }
        )
    return {"total": total, "page": page, "per_page": per_page, "items": out, "settings": _settings(db)}


# --------------------------------------------------------------------------
# drop ship overrides + queueing
# --------------------------------------------------------------------------
class OnIn(BaseModel):
    on: bool


def _queue_product(db: Session, product_id: int) -> int:
    """Queue eBay quantity updates for this product's listings (the existing queue; only stores that allow it). Nothing when the switch is off."""
    if not switch_on(db):
        return 0
    from app.ebay_sales import enqueue_product

    return enqueue_product(db, product_id)


@router.post("/products/{product_id}/dropship")
def api_product_dropship(product_id: int, body: OnIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    n = db.execute(text("update products set dropship_ok = :v where id = :p"), {"v": body.on, "p": product_id}).rowcount
    if not n:
        raise HTTPException(404, "Product not found.")
    queued = _queue_product(db, product_id)
    db.commit()
    return {"dropship_ok": body.on, "queued": queued}


@router.post("/listings/{listing_id}/dropship")
def api_listing_dropship(listing_id: int, body: OnIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    row = db.execute(
        text("update channel_listings set dropship_ok = :v where id = :l returning product_id"), {"v": body.on, "l": listing_id}
    ).first()
    if not row:
        raise HTTPException(404, "Listing not found.")
    queued = _queue_product(db, row.product_id) if row.product_id else 0
    db.commit()
    return {"dropship_ok": body.on, "queued": queued}


@router.post("/apply-zeros")
def api_apply_zeros(db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Queue an eBay quantity update for every product that is at 0 now and has an active listing. The queue sends them within
    about a minute, only to stores whose live-push and sales-quantity switches are ON, and only changes a listing whose
    quantity differs. Needs the master switch ON."""
    if not switch_on(db):
        raise HTTPException(409, "Turn on \"Count distributor stock\" first; otherwise the numbers would not match the website.")
    from app.ebay_sales import enqueue_product

    ids = [
        r.id
        for r in db.execute(
            text(
                f"select p.id from products p where {effective_sql('p')} <= 0 "
                f"and exists (select 1 from channel_listings cl where cl.product_id = p.id and {_ACTIVE_LISTING}) order by p.id limit 3000"
            )
        ).all()
    ]
    listings = 0
    for pid in ids:
        try:
            with db.begin_nested():
                listings += enqueue_product(db, pid)
        except Exception:
            log.exception("could not queue product %s", pid)
    db.commit()
    return {"products": len(ids), "listings": listings}


# --------------------------------------------------------------------------
# website display
# --------------------------------------------------------------------------
class DisplayIn(BaseModel):
    mode: Optional[str] = Field(None, pattern="^(available|count)$")  # None = follow the store default
    all_products: bool = False
    product_ids: list[int] = Field(default_factory=list, max_length=2000)


@router.get("/display/products")
def api_display_products(q: str = Query("", max_length=120), db: Session = Depends(get_db)):
    where, params = ["true"], {}
    for i, w in enumerate(q.split()[:5]):
        where.append(f"(p.title ilike :w{i} or p.sku_display ilike :w{i} or p.custom_label_sku ilike :w{i})")
        params[f"w{i}"] = f"%{w}%"
    rows = db.execute(
        text(
            f"select p.id, p.sku_display as sku, p.title, nullif(p.stock_display, '') as stock_display, {stock_mode_sql('p')} as shown "
            f"from products p where {' and '.join(where)} order by p.title, p.id limit 30"
        ),
        params,
    ).all()
    return {"items": [dict(r._mapping) for r in rows]}


@router.post("/display")
def api_display(body: DisplayIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Set how the website shows stock: 'available' (just Available), 'count' (35 Available) or None (follow the store default),
    for every product or for the chosen ones. Out of stock always shows as Out of stock."""
    if body.all_products:
        n = db.execute(text("update products set stock_display = :m"), {"m": body.mode}).rowcount
    elif body.product_ids:
        n = db.execute(
            text("update products set stock_display = :m where id in :ids").bindparams(bindparam("ids", expanding=True)),
            {"m": body.mode, "ids": sorted(set(body.product_ids))},
        ).rowcount
    else:
        raise HTTPException(400, "Choose the products, or all products.")
    db.commit()
    return {"updated": n}


# --------------------------------------------------------------------------
# the timer
# --------------------------------------------------------------------------
def reconcile(db: Session) -> dict:
    """Look at every product that has pieces and an active eBay listing. Record its number the first time (baseline, no push).
    When the number went DOWN since last time, queue its listings; when it went UP, only if "raise listings automatically" is ON."""
    out = {"checked": 0, "baseline": 0, "queued": 0}
    if not switch_on(db):
        return out
    from app.ebay_sales import enqueue_product

    raise_ok = get_setting(db, "auto_raise", "off") == "on"
    rows = db.execute(
        text(
            f"select p.id, {effective_sql('p')} as eff, st.effective as prev from products p "
            "left join availability_state st on st.product_id = p.id "
            "where exists (select 1 from product_kit_parts k where k.product_id = p.id) "
            f"and exists (select 1 from channel_listings cl where cl.product_id = p.id and {_ACTIVE_LISTING})"
        )
    ).all()
    for n, r in enumerate(rows, 1):
        eff = int(r.eff or 0)
        out["checked"] += 1
        if r.prev is None:
            out["baseline"] += 1
        elif eff == r.prev:
            continue
        elif eff < r.prev or raise_ok:
            try:
                with db.begin_nested():
                    out["queued"] += enqueue_product(db, r.id)
            except Exception:
                log.exception("could not queue product %s", r.id)
        db.execute(
            text(
                "insert into availability_state (product_id, effective, updated_at) values (:p, :e, now()) "
                "on conflict (product_id) do update set effective = excluded.effective, updated_at = now()"
            ),
            {"p": r.id, "e": eff},
        )
        if n % 200 == 0:
            db.commit()
    db.commit()
    return out


def _loop() -> None:
    time.sleep(180)
    while True:
        db = _session_factory()()
        try:
            reconcile(db)
        except Exception:
            db.rollback()
            log.exception("availability check failed")
        finally:
            db.close()
        time.sleep(INTERVAL_SECONDS)


_started = False


def start_background() -> None:
    """Called once when the API starts. Set AVAILABILITY_THREAD=0 to turn the timer off."""
    global _started
    if _started or os.environ.get("AVAILABILITY_THREAD", "1") == "0":
        return
    _started = True
    threading.Thread(target=_loop, name="availability", daemon=True).start()
