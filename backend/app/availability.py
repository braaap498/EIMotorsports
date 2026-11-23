"""
What is AVAILABLE to sell, counting the distributor's stock as well as our own shelf. Mounted at /admin/availability/* (admin only).

DROP SHIP NUMBER: a product or listing marked DROP SHIP shows our shelf PLUS the distributor's stock (Indiana + Nevada), dropship_qty_sql.
A product NOT marked drop ship shows our shelf only (minus what is set aside).

THE RULE (a product or listing marked DROP SHIP counts the distributor's stock; everything else is OUR SHELF ONLY: the shelf count
minus what is set aside. The master switch "Count distributor stock" no longer adds distributor stock to an unmarked product)
  own          quantity_on_hand minus parts set aside (app/stock.py), never below 0
  distributor  Automatic Distributors Indiana + Nevada stock for the product's parts (product_kit_parts). With several pieces
               the scarcest piece decides: min over the pieces of floor(stock / pieces needed per product)
  NOT marked drop ship    available = own only (single part or kit)
  marked drop ship        products.dropship_ok = all its listings, channel_listings.dropship_ok = just that one listing.
                          The number is our shelf plus the distributor's stock (dropship_qty_sql).
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

from app.auth import AuthUser, needs, require_admin, require_route_access
from app.database import _session_factory, get_db
from app.stock import available_sql, same_part_sql, set_aside_sql, shelf_sql

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
            # the WEBSITE's own Drop ship tick on the product. It has nothing to do with eBay: eBay is set per listing (dropship_ok above).
            "alter table products add column if not exists web_dropship_ok boolean not null default false",
            "alter table channel_listings add column if not exists dropship_ok boolean not null default false",
            # PIECES PER SALE: how many units of the product one sale of this listing uses (a 3-pack listing of a one-part product = 3)
            "alter table channel_listings add column if not exists pieces_per_sale integer not null default 1",
            # the listings' ticks as they were before the whole product was switched on (so it can be switched off and put back)
            "alter table products add column if not exists dropship_prev text",
            "create table if not exists availability_settings (key text primary key, value text not null)",
            "create table if not exists availability_state ("
            "product_id bigint primary key, effective integer not null, updated_at timestamptz not null default now())",
        ):
            db.execute(text(stmt))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not set up the availability tables; counting distributor stock will not work until they exist")
    try:
        # frozen on each eBay order line when it is set aside, so editing a listing later never changes an old order (null = not frozen yet)
        db.execute(text("alter table ebay_order_lines add column if not exists pieces_per_sale integer"))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not add pieces_per_sale to ebay_order_lines; multi-pack listings will count as 1 piece until it exists")
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


def linked_sql(p: str = "p") -> str:
    """SQL boolean: product `p` has at least one piece and EVERY piece is matched to a live Automatic Distributors catalog item.
    Only such a product can be drop shipped."""
    return (
        "(select count(*) > 0 and not bool_or(si.id is null) "
        "from product_kit_parts k "
        f"left join supplier_items si on si.id = k.supplier_item_id and si.supplier = '{SUPPLIER}' and si.active "
        f"where k.product_id = {p}.id)"
    )


def has_distributor(db: Session, product_id: int) -> bool:
    try:
        return bool(db.execute(text(f"select coalesce({linked_sql('p')}, false) from products p where p.id = :i"), {"i": product_id}).scalar())
    except Exception:
        db.rollback()
        return False


NO_DISTRIBUTOR_MSG = (
    "That product has no Automatic Distributors part linked (or one of its pieces is not matched to the distributor's catalog), "
    "so it cannot be drop shipped. Link its parts to the distributor catalog first."
)


def wh_sql(p: str, col: str) -> str:
    """Stock of ONE distributor warehouse (`qty_indiana` or `qty_nevada`) for product `p`: the scarcest piece decides, 0 when there
    are no pieces or a piece has no live catalog item."""
    return (
        "(select case when count(*) = 0 or bool_or(si.id is null) then 0 else "
        f"coalesce(min(cast(floor(cast(coalesce(si.{col}, 0) as numeric) "
        "/ greatest(coalesce(k.quantity, 1), 1)) as integer)), 0) end "
        "from product_kit_parts k "
        f"left join supplier_items si on si.id = k.supplier_item_id and si.supplier = '{SUPPLIER}' and si.active "
        f"where k.product_id = {p}.id)"
    )


def dropship_qty_sql(p: str = "p") -> str:
    """The number a DROP SHIP product or listing shows: our own shelf PLUS the distributor's stock (Indiana + Nevada; with several
    pieces the scarcest piece decides). Before, it was the lowest non-empty of the three."""
    return f"({available_sql(p)} + {dist_sql(p)})"


def extra_sql(p: str = "p", ovr: str = "false", ignore_switch: bool = False, flag: str = "dropship_ok") -> str:
    """What the distributor adds on top of our own count for product `p`.
    Only a product or listing marked DROP SHIP (`{p}.dropship_ok`, or `ovr`, an SQL boolean such as "coalesce(cl.dropship_ok, false)")
    counts the distributor's stock, whether it is a single part or a kit. Anything NOT marked is our own shelf only: the master
    switch no longer adds distributor stock to an unmarked product. (`ignore_switch` is kept so callers do not change.)"""
    return f"(case when {p}.{flag} or {ovr} then {dist_sql(p)} else 0 end)"


def effective_sql(p: str = "p", ovr: str = "false", ignore_switch: bool = False, flag: str = "dropship_ok") -> str:
    """SQL: what can be sold from product `p` now. A product or listing marked DROP SHIP shows the lowest non-empty of our shelf,
    Indiana and Nevada (dropship_qty_sql); anything else is our own count plus the distributor's where the rule allows."""
    return (
        f"(case when {p}.{flag} or {ovr} then {dropship_qty_sql(p)} "
        f"else {available_sql(p)} + {extra_sql(p, ovr, ignore_switch, flag)} end)"
    )


def web_effective_sql(p: str = "p") -> str:
    """What the WEBSITE can sell of product `p`. Only the product's own website Drop ship tick (products.web_dropship_ok) counts here;
    the eBay drop ship ticks (per listing, in Sales channels) never change what the website shows, and neither does the
    'Count distributor stock' switch.
      ticked   -> our shelf plus the distributor's stock; 0 (out of stock, cannot be ordered) when both are empty
      unticked -> our own shelf only (what is not set aside for other orders)"""
    return f"(case when coalesce({p}.web_dropship_ok, false) then {dropship_qty_sql(p)} else {available_sql(p)} end)"


def stock_mode_sql(p: str = "p") -> str:
    """SQL: how the website shows stock for product `p`: 'available' or 'count'."""
    return f"coalesce(nullif({p}.stock_display, ''), {_setting_sql('stock_display_default', 'available')})"


QTY_READ = ("products.view", "products.edit_details", "products.create")


@router.get("/products/{product_id}/quantities")
@needs(*QTY_READ)
def product_quantities(product_id: int, db: Session = Depends(get_db)):
    """Everything the product screen's Quantities box shows, read only: our shelf (total and per warehouse), the distributor's
    Indiana and Nevada, and the Total Quantity Available (our shelf only, or our shelf plus the distributor when the product is
    set to drop ship)."""
    r = db.execute(
        text(
            f"select p.id, {shelf_sql('p')} as qoh, {available_sql('p')} as own, {parts_count_sql('p')} as parts, "
            f"{dist_sql('p')} as dist, {wh_sql('p', 'qty_indiana')} as indiana, {wh_sql('p', 'qty_nevada')} as nevada, "
            "coalesce(p.dropship_ok, false) as ds, "
            "(select count(*) from channel_listings cl where cl.product_id = p.id and coalesce(cl.dropship_ok, false)) as ds_listings "
            "from products p where p.id = :i"
        ),
        {"i": product_id},
    ).first()
    if not r:
        raise HTTPException(404, "Product not found")
    whs: list[dict] = []
    try:
        rows = [] if int(r.parts or 0) > 1 else db.execute(
            text(
                "select w.id, w.name, w.is_default, coalesce(ws.quantity, 0) as q from warehouses w "
                "left join warehouse_stock ws on ws.warehouse_id = w.id and ws.product_id = :i "
                "where coalesce(w.active, true) order by w.is_default desc, w.id"
            ),
            {"i": product_id},
        ).all()
        others = sum(int(x.q) for x in rows if not x.is_default)
        for x in rows:
            q = max(0, int(r.qoh) - others) if x.is_default else int(x.q)
            whs.append({"id": x.id, "name": x.name, "quantity": q, "main": bool(x.is_default)})
    except Exception:
        db.rollback()
    dist = int(r.dist or 0)
    # whose stock the distributor numbers are (they are counted from the catalog of this one supplier)
    dist_names: list[str] = []
    try:
        dist_names = [
            x.name
            for x in db.execute(
                text(
                    "select distinct d.name from product_kit_parts k join po_distributors d on d.id = k.distributor_id "
                    "join supplier_items si on si.id = k.supplier_item_id and si.supplier = :s where k.product_id = :i order by d.name"
                ),
                {"i": product_id, "s": SUPPLIER},
            ).all()
        ]
    except Exception:
        db.rollback()
    kit_parts: list[dict] = []
    if int(r.parts or 0) > 1:
        try:
            for k in db.execute(
                text(
                    "select k.part_number, greatest(coalesce(k.quantity, 1), 1) as need, "
                    "(coalesce(k.label_use_mfr, false) and k.supplier_item_id is not null) as use_mfr, "
                    "coalesce(nullif(btrim(k.mfr_number), ''), nullif(btrim(si0.mfr_number), '')) as mfr_no, coalesce(("
                    "  select sum(greatest(0, coalesce(p2.quantity_on_hand, 0) - " + set_aside_sql("p2") + ") * greatest(coalesce(k2.quantity, 1), 1)) "
                    "  from product_kit_parts k2 join products p2 on p2.id = k2.product_id "
                    f"  where {same_part_sql('k', 'k2')} and p2.id <> :i "
                    "    and (select count(*) from product_kit_parts k3 where k3.product_id = p2.id) = 1), 0) as units "
                    "from product_kit_parts k left join supplier_items si0 on si0.id = k.supplier_item_id "
                    "where k.product_id = :i order by k.sort_order, k.id"
                ),
                {"i": product_id},
            ).all():
                kit_parts.append(
                    {
                        "part_number": k.part_number,
                        "need": int(k.need),
                        "on_our_shelf": int(k.units),
                        "kits": int(k.units) // int(k.need),
                        # a part set to "use the manu. part # for the custom label" is shown by its manufacturer number
                        "use_mfr_number": bool(k.use_mfr and k.mfr_no),
                        "mfr_number": k.mfr_no if (k.use_mfr and k.mfr_no) else None,
                    }
                )
        except Exception:
            db.rollback()
    return {
        "kit_parts": kit_parts,
        "ei_total": int(r.qoh),
        "own_available": int(r.own),
        "warehouses": whs,
        "indiana": int(r.indiana or 0),
        "nevada": int(r.nevada or 0),
        "distributor": dist,
        "distributor_name": ", ".join(dist_names) or "Automatic Distributors",
        "parts": int(r.parts or 0),
        "is_kit": int(r.parts or 0) > 1,
        "drop_ship": bool(r.ds),
        "drop_ship_listings": int(r.ds_listings or 0),
        "total_available": int(r.own) + (dist if r.ds else 0),
    }


# --------------------------------------------------------------------------
# helpers for the other modules
# --------------------------------------------------------------------------
def extra_stock(db: Session, ids) -> dict[int, int]:
    """{product id: units the distributor adds} for the website checkout: the distributor's stock for a product ticked Drop ship on the
    website, 0 for everything else. Uses the website's own tick, not the eBay ones, and ignores the 'Count distributor stock' switch."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return {}
    try:
        rows = db.execute(
            text(f"select p.id, (case when coalesce(p.web_dropship_ok, false) then {dist_sql('p')} else 0 end) as e from products p where p.id in :ids").bindparams(bindparam("ids", expanding=True)),
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


def pieces_of(v) -> int:
    """A pieces-per-sale value made safe: a whole number, at least 1."""
    try:
        return max(1, int(v or 1))
    except (TypeError, ValueError):
        return 1


def packs(avail: int, pieces) -> int:
    """How many SALES of a listing the available units cover: units / pieces per sale, rounded down. A 3-pack listing with 2 units
    available shows 0. With pieces = 1 this is the number itself."""
    return max(0, int(avail or 0)) // pieces_of(pieces)


def line_pieces_sql(l: str = "l") -> str:
    """SQL: pieces per sale of an eBay order line `l` (an ebay_order_lines alias): the number frozen on the line when its parts were set
    aside, else what its listing says now, else 1."""
    return (
        f"coalesce({l}.pieces_per_sale, (select cl.pieces_per_sale from channel_listings cl "
        f"where regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = regexp_replace(cast({l}.ebay_item_id as text), '[^0-9]', '', 'g') "
        f"and lower(cl.store) = lower(coalesce({l}.store, '')) limit 1), 1)"
    )


def line_units_sql(l: str = "l") -> str:
    """SQL: units of the product an eBay order line takes (quantity bought x pieces per sale)."""
    return f"({l}.quantity * {line_pieces_sql(l)})"


def listing_pieces(db: Session, store: str, item_id: str) -> int:
    """Pieces per sale of the eBay listing with this item number in this store (1 when it is not linked or the column is not there yet)."""
    try:
        with db.begin_nested():
            v = db.execute(
                text(
                    "select cl.pieces_per_sale from channel_listings cl "
                    "where regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = regexp_replace(cast(:i as text), '[^0-9]', '', 'g') "
                    "and lower(cl.store) = lower(:s) limit 1"
                ),
                {"i": item_id, "s": store},
            ).scalar()
        return pieces_of(v)
    except Exception:
        log.exception("could not read pieces per sale for %s", item_id)
        return 1


def listing_quantity(db: Session, product_id: int, store: str, item_id: str, listing_dropship: bool = False) -> int:
    """The quantity to show on this eBay listing: units available (drop ship counted when the listing allows it) / its pieces per sale."""
    return packs(effective(db, product_id, listing_dropship), listing_pieces(db, store, item_id))


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
            f"select p.id, p.sku_display as sku, p.title, p.custom_label_sku, {shelf_sql('p')} as on_hand, "
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
                f"coalesce(cl.dropship_ok, false) as dropship_ok, coalesce(cl.pieces_per_sale, 1) as pieces_per_sale "
                f"from channel_listings cl where cl.product_id in :ids and {_ACTIVE_LISTING} "
                "order by cl.store, cl.id"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ).all():
            listings.setdefault(l.product_id, []).append(
                {"listing_id": l.id, "store": l.store, "item_id": l.item_id, "dropship_ok": bool(l.dropship_ok),
                 "pieces_per_sale": pieces_of(l.pieces_per_sale)}
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
    """Queue eBay quantity updates for this product's listings (the existing queue; only stores that allow it). Ticking or unticking
    Drop ship changes the number, so this runs whatever the master switch says."""
    from app.ebay_sales import enqueue_product

    return enqueue_product(db, product_id)


class ProductDropIn(BaseModel):
    on: bool
    # when turning OFF: "restore" puts each listing back the way it was before the product was switched on; "reset" unticks them all
    off_mode: Optional[str] = Field(None, pattern="^(restore|reset)$")


@router.post("/products/{product_id}/dropship")
def api_product_dropship(product_id: int, body: ProductDropIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Switch a whole product on or off for drop shipping. ON ticks every listing of the product (overwriting what was set one by one;
    the earlier ticks are remembered). OFF either restores the earlier ticks or unticks them all."""
    import json

    p = db.execute(text("select id, coalesce(dropship_ok, false) as on_, dropship_prev from products where id = :p for update"), {"p": product_id}).first()
    if not p:
        raise HTTPException(404, "Product not found.")
    if body.on and not has_distributor(db, product_id):
        db.rollback()
        raise HTTPException(409, NO_DISTRIBUTOR_MSG)
    ids = [r.id for r in db.execute(text("select id from channel_listings where product_id = :p"), {"p": product_id}).all()]
    restored = 0
    if body.on:
        if not p.on_:
            before = {
                str(r.id): bool(r.dropship_ok)
                for r in db.execute(text("select id, coalesce(dropship_ok, false) as dropship_ok from channel_listings where product_id = :p"), {"p": product_id}).all()
            }
            db.execute(text("update products set dropship_ok = true, dropship_prev = :v where id = :p"), {"v": json.dumps(before), "p": product_id})
        db.execute(text("update channel_listings set dropship_ok = true where product_id = :p"), {"p": product_id})
    else:
        mode = body.off_mode or "reset"
        prev: dict = {}
        if mode == "restore" and p.dropship_prev:
            try:
                prev = json.loads(p.dropship_prev)
            except ValueError:
                prev = {}
        for lid in ids:
            db.execute(
                text("update channel_listings set dropship_ok = :v where id = :l"),
                {"v": bool(prev.get(str(lid))) if mode == "restore" else False, "l": lid},
            )
            restored += 1 if (mode == "restore" and prev.get(str(lid))) else 0
        db.execute(text("update products set dropship_ok = false, dropship_prev = null where id = :p"), {"p": product_id})
    queued = _queue_product(db, product_id)
    db.commit()
    return {"dropship_ok": body.on, "queued": queued, "restored": restored}


class PiecesIn(BaseModel):
    pieces: int = Field(..., ge=1, le=999)


@router.post("/listings/{listing_id}/pieces")
def api_listing_pieces(listing_id: int, body: PiecesIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """PIECES PER SALE for one listing: how many units of the product one sale of it uses (1 = a single, 3 = a 3-pack). The listing then shows
    units available / pieces (rounded down), and each order for it sets aside quantity x pieces. Orders already set aside keep the number they
    had when they came in. The new quantity is queued to eBay."""
    row = db.execute(
        text("update channel_listings set pieces_per_sale = :n where id = :l returning product_id"), {"n": body.pieces, "l": listing_id}
    ).first()
    if not row:
        raise HTTPException(404, "Listing not found.")
    queued = _queue_product(db, row.product_id) if row.product_id else 0
    db.commit()
    return {"pieces": body.pieces, "queued": queued}


@router.post("/listings/{listing_id}/dropship")
def api_listing_dropship(listing_id: int, body: OnIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    if body.on:
        pid = db.execute(text("select product_id from channel_listings where id = :l"), {"l": listing_id}).scalar()
        if pid is None or not has_distributor(db, int(pid)):
            raise HTTPException(409, NO_DISTRIBUTOR_MSG)
    row = db.execute(
        text("update channel_listings set dropship_ok = :v where id = :l returning product_id"), {"v": body.on, "l": listing_id}
    ).first()
    if not row:
        raise HTTPException(404, "Listing not found.")
    if row.product_id and not body.on:
        # a listing was unticked, so the product is no longer "every listing": the others keep their ticks
        db.execute(text("update products set dropship_ok = false, dropship_prev = null where id = :p and coalesce(dropship_ok, false)"), {"p": row.product_id})
    queued = _queue_product(db, row.product_id) if row.product_id else 0
    db.commit()
    return {"dropship_ok": body.on, "queued": queued}


@router.get("/products")
def api_products(
    q: Optional[str] = Query(None, max_length=100),
    show: str = Query("all", pattern="^(all|on|off|zero)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(40, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Products that have an active eBay listing, with those listings underneath, for the Drop ship page.
    show: all | on (some listing ticked) | off (none ticked) | zero (nothing can be sold now)."""
    params: dict = {}
    where = [f"exists (select 1 from channel_listings cl where cl.product_id = p.id and {_ACTIVE_LISTING})"]
    ticked = f"(coalesce(p.dropship_ok, false) or exists (select 1 from channel_listings c2 where c2.product_id = p.id and c2.dropship_ok and {_ACTIVE_LISTING.replace('cl.', 'c2.')}))"
    if show == "on":
        where.append(ticked)
    elif show == "off":
        where.append(f"not {ticked}")
    elif show == "zero":
        where.append(f"{effective_sql('p', ignore_switch=True)} <= 0")
    if q and q.strip():
        for i, w in enumerate(q.split()[:5]):
            where.append(f"(p.title ilike :w{i} or p.sku_display ilike :w{i} or p.custom_label_sku ilike :w{i} or p.manufacturer_part_number ilike :w{i})")
            params[f"w{i}"] = f"%{w}%"
    cond = " and ".join(where)
    total = int(db.execute(text(f"select count(*) from products p where {cond}"), params).scalar() or 0)
    rows = db.execute(
        text(
            f"select p.id, p.sku_display as sku, p.title, p.custom_label_sku, {shelf_sql('p')} as on_hand, "
            f"{available_sql('p')} as own, {parts_count_sql('p')} as parts, {dist_sql('p')} as distributor, "
            f"coalesce(p.dropship_ok, false) as dropship_ok, (p.dropship_prev is not null) as has_prev, coalesce({linked_sql('p')}, false) as linked "
            f"from products p where {cond} order by p.title, p.id limit :lim offset :off"
        ),
        {**params, "lim": per_page, "off": (page - 1) * per_page},
    ).all()
    ids = [r.id for r in rows]
    listings: dict[int, list] = {}
    if ids:
        for l in db.execute(
            text(
                "select cl.id, cl.product_id, cl.store, regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') as item_id, "
                f"coalesce(cl.dropship_ok, false) as dropship_ok, coalesce(cl.pieces_per_sale, 1) as pieces_per_sale "
                f"from channel_listings cl where cl.product_id in :ids and {_ACTIVE_LISTING} "
                "order by cl.store, cl.id"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ).all():
            listings.setdefault(l.product_id, []).append(
                {"listing_id": l.id, "store": l.store, "item_id": l.item_id, "dropship_ok": bool(l.dropship_ok),
                 "pieces_per_sale": pieces_of(l.pieces_per_sale)}
            )
    items = []
    for r in rows:
        ls = listings.get(r.id, [])
        for x in ls:   # what the listing shows on eBay: units (drop ship adds the distributor's) / pieces per sale, rounded down
            x["listing_qty"] = packs(int(r.own) + (int(r.distributor) if (r.dropship_ok or x["dropship_ok"]) else 0), x["pieces_per_sale"])
        items.append(
            {
                "product_id": r.id, "sku": r.sku, "title": r.title, "custom_label": r.custom_label_sku,
                "on_hand": int(r.on_hand), "own_available": int(r.own), "distributor": int(r.distributor),
                "piece_count": int(r.parts), "dropship_ok": bool(r.dropship_ok), "has_prev": bool(r.has_prev), "has_distributor": bool(r.linked),
                "ticked": sum(1 for x in ls if x["dropship_ok"]), "listings": ls,
            }
        )
    return {"total": total, "page": page, "per_page": per_page, "items": items, "settings": _settings(db)}


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
    from app.ebay_sales import enqueue_product

    on = switch_on(db)
    flagged = "exists (select 1 from channel_listings cl2 where cl2.product_id = p.id and cl2.dropship_ok)"

    raise_ok = get_setting(db, "auto_raise", "off") == "on"
    rows = db.execute(
        text(
            f"select p.id, {effective_sql('p', flagged)} as eff, st.effective as prev, (p.dropship_ok or {flagged}) as ds from products p "
            "left join availability_state st on st.product_id = p.id "
            "where exists (select 1 from product_kit_parts k where k.product_id = p.id) "
            f"and exists (select 1 from channel_listings cl where cl.product_id = p.id and {_ACTIVE_LISTING}) "
            + ("" if on else f"and (p.dropship_ok or {flagged})")
        )
    ).all()
    for n, r in enumerate(rows, 1):
        eff = int(r.eff or 0)
        out["checked"] += 1
        if r.prev is None:
            out["baseline"] += 1
        elif eff == r.prev:
            continue
        elif eff < r.prev or raise_ok or r.ds:   # drop ship numbers move up when one source runs out, so they are always sent
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
