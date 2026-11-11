"""More warehouses: contact details, adding a warehouse, and the stock kept in each one (Inventory > Warehouses / Stock).

  POST /inventory/warehouses                       add a warehouse (name, address, manager, phone, email, notes)
  PUT  /inventory/warehouses/{id}/details          change a warehouse's name and details
  GET  /inventory/warehouses/all/stock             every part with its total across warehouses (read-only)
  GET  /inventory/warehouses/{id}/stock            the parts a warehouse holds, with view (all/low/out), status and search
  PUT  /inventory/warehouses/{id}/stock/{product}  set how many of a part a warehouse holds (adds the part if new; 0 keeps it)
  PUT  /inventory/warehouses/{id}/stock/{product}/restock-level  set the restock level of a part in this warehouse
  DELETE /inventory/warehouses/{id}/stock/{product}              take a part out of a (non-main) warehouse
  GET  /inventory/warehouses/changes               who changed what, when and where (manual changes only)

HOW STOCK WORKS
  products.quantity_on_hand is the TOTAL on hand across every warehouse. Because of that, everything that already reads it
  (what the website and eBay can sell, reservations, urgent/short checks, eBay quantity updates) counts every warehouse
  without any change.

  warehouse_stock holds what each NON-main warehouse has. The main (default) warehouse is the rest:
        main = quantity_on_hand - sum(other warehouses)

  PICKING TAKES FROM THE MAIN WAREHOUSE FIRST. A pick lowers the total, so the main warehouse's share goes down first. Only
  when the total falls below what the other warehouses hold does a database trigger (wh_trim_others) take the difference
  from them, oldest warehouse first, and write it to warehouse_stock_log. The order activity log shows those rows as
  "Taken from <warehouse>". The trigger covers every way stock can go down (scan pick, mark picked, cancel, manual count),
  so no pick code needed changing.

  Setting a warehouse's count changes the total by the same amount and is written to the stock ledger.

  A part stays on a non-main warehouse's list at 0 (so it can show under Out of stock there) until someone removes it.
  The main warehouse lists every part except ones that are held only in another warehouse.

  RESERVED PER WAREHOUSE: reservations belong to the part, not a warehouse. They are shown against the warehouses in the
  same order picking draws from them (main first, then the others by id), so Reserved and Available add up to the part's.

  warehouse_change_log records every manual change (who, when, which warehouse, which part). Automatic movements
  (picks, the main-ran-out trim) are not in it; those are in the order activity log and the stock ledger.
"""
import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.admin_inventory import ANY_INVENTORY, WAREHOUSE_NAME_MAX, _CONTROL, MAX_QTY, _OVERSOLD_SQL, _ensure_warehouses
from app.stock import available_sql, set_aside_sql
from app.ebay_sales import queue_stock_push
from app.auth import AuthUser, current_user, needs, require_route_access
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/inventory/warehouses", tags=["admin-warehouses"], dependencies=[Depends(require_route_access)])

DETAIL_COLS = ("address", "city", "state", "zip", "manager", "phone", "email", "notes")


def ensure_tables() -> None:
    """Startup: the extra warehouse columns and the per-warehouse stock table (on their own connection)."""
    db = _session_factory()()
    try:
        db.execute(
            text(
                """
                create table if not exists warehouses (
                    id bigserial primary key,
                    name text not null,
                    is_default boolean not null default false,
                    active boolean not null default true,
                    created_at timestamptz not null default now()
                )
                """
            )
        )
        for c in DETAIL_COLS:
            db.execute(text(f"alter table warehouses add column if not exists {c} text"))
        db.execute(
            text(
                """
                create table if not exists warehouse_stock (
                    warehouse_id bigint not null references warehouses(id) on delete cascade,
                    product_id bigint not null,
                    quantity integer not null default 0,
                    updated_at timestamptz not null default now(),
                    updated_by text,
                    primary key (warehouse_id, product_id)
                )
                """
            )
        )
        db.execute(text("alter table warehouse_stock add column if not exists updated_by text"))
        db.execute(text("alter table warehouse_stock add column if not exists reorder_point integer"))
        db.execute(
            text(
                """
                create table if not exists warehouse_change_log (
                    id bigint generated always as identity primary key,
                    at timestamptz not null default now(),
                    user_email text,
                    warehouse_id bigint,
                    warehouse_name text,
                    product_id bigint,
                    sku text,
                    title text,
                    action text not null,
                    old_value text,
                    new_value text
                )
                """
            )
        )
        db.execute(text("create index if not exists warehouse_change_log_at_idx on warehouse_change_log (at desc)"))
        db.execute(
            text(
                """
                create table if not exists warehouse_stock_log (
                    id bigint generated always as identity primary key,
                    at timestamptz not null default now(),
                    txid bigint not null default txid_current(),
                    product_id bigint not null,
                    warehouse_id bigint not null,
                    delta integer not null
                )
                """
            )
        )
        db.execute(text("create table if not exists warehouse_stock_meta (key text primary key, done_at timestamptz not null default now())"))
        db.commit()  # the tables are in place even if the trigger step below fails
        try:
            # When the total on hand falls below what the other warehouses hold, the main warehouse has run out: take the rest
            # from the other warehouses, the one added first first.
            conn = db.connection()
            conn.exec_driver_sql(
                """
                create or replace function wh_trim_others() returns trigger as $fn$
                declare
                    excess integer;
                    r record;
                    took integer;
                begin
                    select coalesce(sum(quantity), 0) - greatest(coalesce(new.quantity_on_hand, 0), 0) into excess
                      from warehouse_stock where product_id = new.id;
                    if excess > 0 then
                        for r in select warehouse_id, quantity from warehouse_stock
                                  where product_id = new.id and quantity > 0 order by warehouse_id loop
                            exit when excess <= 0;
                            took := least(r.quantity, excess);
                            update warehouse_stock set quantity = quantity - took, updated_at = now(), updated_by = 'automatic (main warehouse ran out)'
                             where warehouse_id = r.warehouse_id and product_id = new.id;
                            insert into warehouse_stock_log (product_id, warehouse_id, delta) values (new.id, r.warehouse_id, -took);
                            excess := excess - took;
                        end loop;
                    end if;
                    return null;
                end
                $fn$ language plpgsql
                """
            )
            conn.exec_driver_sql("drop trigger if exists wh_trim_others_trg on products")
            conn.exec_driver_sql(
                "create trigger wh_trim_others_trg after update of quantity_on_hand on products for each row "
                "when (new.quantity_on_hand is not null and old.quantity_on_hand is not null and new.quantity_on_hand < old.quantity_on_hand) "
                "execute function wh_trim_others()"
            )
            # One time: counts saved in other warehouses before the total included them are added to the total now.
            first = db.execute(
                text("insert into warehouse_stock_meta (key) values ('total_includes_other_warehouses') on conflict do nothing returning key")
            ).first()
            moved: list[int] = []
            if first:
                rows = db.execute(
                    text("select product_id, sum(quantity) as q from warehouse_stock where quantity > 0 group by product_id")
                ).all()
                for r in rows:
                    db.execute(
                        text("update products set quantity_on_hand = coalesce(quantity_on_hand, 0) + :q, updated_at = now() where id = :p"),
                        {"q": int(r.q), "p": r.product_id},
                    )
                    db.execute(
                        text(
                            "insert into inventory_transactions (product_id, transaction_type, quantity_change, reference, legacy_created_by) "
                            "values (:p, 'Adjustment', :q, 'Other warehouses now counted in the total on hand', 'system')"
                        ),
                        {"p": r.product_id, "q": int(r.q)},
                    )
                    moved.append(int(r.product_id))
                if moved:
                    queue_stock_push(db, moved)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("Could not install the warehouse pick-fallback trigger; picks will not draw on other warehouses")
    except Exception:
        db.rollback()
        log.exception("Could not prepare the warehouse tables")
    finally:
        db.close()


class WarehouseIn(BaseModel):
    name: str = Field(..., max_length=WAREHOUSE_NAME_MAX)
    address: Optional[str] = Field(None, max_length=200)
    city: Optional[str] = Field(None, max_length=80)
    state: Optional[str] = Field(None, max_length=40)
    zip: Optional[str] = Field(None, max_length=20)
    manager: Optional[str] = Field(None, max_length=100)
    phone: Optional[str] = Field(None, max_length=40)
    email: Optional[str] = Field(None, max_length=120)
    notes: Optional[str] = Field(None, max_length=500)


def _clean(body: WarehouseIn) -> dict:
    name = " ".join((body.name or "").split())
    if not name or _CONTROL.search(name):
        raise HTTPException(400, "Type a name for the warehouse.")
    if len(name) > WAREHOUSE_NAME_MAX:
        raise HTTPException(400, f"Keep the name to {WAREHOUSE_NAME_MAX} characters or fewer.")
    out = {"name": name}
    for c in DETAIL_COLS:
        v = " ".join(str(getattr(body, c) or "").split()) if c != "notes" else str(body.notes or "").strip()
        if _CONTROL.search(v.replace("\n", " ")):
            raise HTTPException(400, f"The {c} has characters that can't be saved.")
        out[c] = v or None
    return out


@router.post("")
@needs("inventory.adjust")
def add_warehouse(body: WarehouseIn, db: Session = Depends(get_db)):
    _ensure_warehouses(db)
    v = _clean(body)
    try:
        row = db.execute(
            text(
                "insert into warehouses (name, address, city, state, zip, manager, phone, email, notes) "
                "values (:name, :address, :city, :state, :zip, :manager, :phone, :email, :notes) returning id"
            ),
            v,
        ).first()
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Another warehouse already has that name.")
    return {"ok": True, "id": row.id}


@router.put("/{warehouse_id}/details")
@needs("inventory.adjust")
def update_details(warehouse_id: int, body: WarehouseIn, db: Session = Depends(get_db)):
    _ensure_warehouses(db)
    v = _clean(body)
    try:
        row = db.execute(
            text(
                "update warehouses set name = :name, address = :address, city = :city, state = :state, zip = :zip, "
                "manager = :manager, phone = :phone, email = :email, notes = :notes where id = :id returning id"
            ),
            {**v, "id": warehouse_id},
        ).first()
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Another warehouse already has that name.")
    if not row:
        raise HTTPException(404, "No such warehouse.")
    return {"ok": True}


def _warehouse(db: Session, warehouse_id: int):
    w = db.execute(text("select id, name, is_default from warehouses where id = :i"), {"i": warehouse_id}).first()
    if not w:
        raise HTTPException(404, "No such warehouse.")
    return w


_OTHERS = "(select product_id, sum(quantity) as s from warehouse_stock group by product_id)"


def log_change(db: Session, who: Optional[str], w, product_id: int, action: str, old, new) -> None:
    """Write one line to the warehouse change log (inside the caller's transaction). action: added, count, removed, restock."""
    p = db.execute(text("select sku_display as sku, title from products where id = :p"), {"p": product_id}).first()
    db.execute(
        text(
            "insert into warehouse_change_log (user_email, warehouse_id, warehouse_name, product_id, sku, title, action, old_value, new_value) "
            "values (:u, :w, :wn, :p, :sku, :t, :a, :o, :n)"
        ),
        {
            "u": who,
            "w": w.id,
            "wn": w.name,
            "p": product_id,
            "sku": p.sku if p else None,
            "t": (p.title if p else None),
            "a": action,
            "o": None if old is None else str(old),
            "n": None if new is None else str(new),
        },
    )


def _stock_cte(kind: str, status: str, words: int) -> str:
    """kind: "main", "other" or "all". One row per part this warehouse lists (or every part, for "all"), with its count, bin, restock level, and its share of the part's reservations."""
    is_main = kind == "main"
    binx = "p.bin_location"  # the bin/shelf comes from the Barcodes and labels page and belongs to the part
    conds = []
    if status == "active":
        conds.append("coalesce(p.is_active, true)")
    elif status == "inactive":
        conds.append("not coalesce(p.is_active, true)")
    for i in range(words):
        conds.append(
            f"(p.title ilike cast(:q{i} as text) or p.sku ilike cast(:q{i} as text) or p.sku_display ilike cast(:q{i} as text) "
            f"or p.ebay_item_id ilike cast(:q{i} as text) or p.manufacturer_part_number ilike cast(:q{i} as text) "
            f"or {binx} ilike cast(:q{i} as text) "
            f"or exists (select 1 from product_barcodes b where b.product_id = p.id and b.code ilike cast(:q{i} as text)))"
        )
    extra = (" and " + " and ".join(conds)) if conds else ""
    main_qty = "greatest(0, coalesce(p.quantity_on_hand, 0) - coalesce(o.s, 0))"
    if kind == "all":
        base = (
            f"select p.id, p.sku_display as sku, p.title, coalesce(p.is_active, true) as is_active, coalesce(p.quantity_on_hand, 0) as qty, "
            f"       p.bin_location as bin_location, p.reorder_point as reorder_point, {set_aside_sql('p')} as set_aside, 0 as before_qty "
            f"from products p where true{extra}"
        )
    elif is_main:
        base = (
            f"select p.id, p.sku_display as sku, p.title, coalesce(p.is_active, true) as is_active, {main_qty} as qty, "
            f"       p.bin_location as bin_location, p.reorder_point as reorder_point, {set_aside_sql('p')} as set_aside, 0 as before_qty "
            f"from products p left join {_OTHERS} o on o.product_id = p.id "
            f"where ({main_qty} > 0 or not exists (select 1 from warehouse_stock x where x.product_id = p.id)){extra}"
        )
    else:
        base = (
            f"select p.id, p.sku_display as sku, p.title, coalesce(p.is_active, true) as is_active, s.quantity as qty, "
            f"       p.bin_location as bin_location, s.reorder_point as reorder_point, {set_aside_sql('p')} as set_aside, "
            f"       {main_qty} + coalesce((select sum(x.quantity) from warehouse_stock x where x.product_id = p.id and x.warehouse_id < :w), 0) as before_qty "
            f"from warehouse_stock s join products p on p.id = s.product_id left join {_OTHERS} o on o.product_id = p.id "
            f"where s.warehouse_id = :w{extra}"
        )
    return (
        f"with base as ({base}), "
        "calc as (select b.*, greatest(0, least(b.qty, b.set_aside - b.before_qty)) as reserved from base b), "
        "wr as (select c.*, c.qty - c.reserved as available from calc c) "
    )


_LOW = "(coalesce(wr.reorder_point, 0) > 0 and wr.available > 0 and wr.available <= wr.reorder_point)"
_OUT = "(wr.available <= 0)"


@router.get("/changes")
@needs(*ANY_INVENTORY)
def stock_changes(
    warehouse_id: Optional[int] = Query(None),
    action: Optional[str] = Query(None, pattern="^(added|count|removed|restock)$"),
    q: Optional[str] = Query(None, max_length=100),
    date_from: str = Query("", max_length=10),
    date_to: str = Query("", max_length=10),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Manual stock changes made in the warehouses, newest first."""
    params: dict = {"limit": limit, "offset": offset, "wid": warehouse_id, "action": action}
    for key, raw in (("df", date_from), ("dt", date_to)):
        params[key] = None
        if raw.strip():
            try:
                d = date.fromisoformat(raw.strip())
            except ValueError:
                raise HTTPException(422, "Dates must look like 2026-11-07.")
            params[key] = d if key == "df" else d + timedelta(days=1)  # "to" includes the whole last day
    conds = [
        "(cast(:wid as bigint) is null or l.warehouse_id = cast(:wid as bigint))",
        "(cast(:action as text) is null or l.action = cast(:action as text))",
        "(cast(:df as date) is null or l.at >= cast(:df as date))",
        "(cast(:dt as date) is null or l.at < cast(:dt as date))",
    ]
    for i, word in enumerate([x for x in (q or "").split() if x][:6]):
        params[f"q{i}"] = f"%{word}%"
        conds.append(f"(l.sku ilike cast(:q{i} as text) or l.title ilike cast(:q{i} as text) or l.user_email ilike cast(:q{i} as text))")
    where = " and ".join(conds)
    total = db.execute(text(f"select count(*) from warehouse_change_log l where {where}"), params).scalar()
    rows = db.execute(
        text(
            "select l.id, l.at, l.user_email, l.warehouse_id, coalesce(w.name, l.warehouse_name) as warehouse_name, "
            "       l.product_id, l.sku, l.title, l.action, l.old_value, l.new_value "
            f"from warehouse_change_log l left join warehouses w on w.id = l.warehouse_id where {where} "
            "order by l.at desc, l.id desc limit :limit offset :offset"
        ),
        params,
    ).all()
    return {"total": int(total or 0), "items": [dict(r._mapping) for r in rows]}


def _stock_response(db: Session, kind: str, warehouse_id: int, q, view: str, status: str, limit: int, offset: int) -> dict:
    params: dict = {"w": warehouse_id, "limit": limit, "offset": offset}
    words = [x for x in (q or "").split() if x][:6]
    for i, word in enumerate(words):
        params[f"q{i}"] = f"%{word}%"
    cond = {"low": _LOW, "out": _OUT}.get(view, "true")

    counts = db.execute(
        text(
            _stock_cte(kind, status, 0)
            + f"select count(*) filter (where {_LOW}) as low_count, count(*) filter (where {_OUT}) as out_count from wr"
        ),
        {"w": warehouse_id},
    ).first()
    cte = _stock_cte(kind, status, len(words))
    total = db.execute(text(cte + f"select count(*) from wr where {cond}"), params).scalar()
    order = "(wr.reorder_point - wr.available) desc, wr.title, wr.id" if view == "low" else "wr.title, wr.id"
    rows = db.execute(
        text(
            cte
            + "select wr.id, wr.sku, wr.title, wr.is_active, wr.qty as quantity, wr.bin_location, wr.reorder_point, wr.reserved, wr.available, "
            "       coalesce(p.on_order, false) as on_order, p.expected_date, "
            "       exists (select 1 from po_lines l join po_headers ph on ph.id = l.po_id "
            "         where l.product_id = p.id and l.received_at is null and ph.status <> 'cancelled') as has_open_po, "
            f"       {_OVERSOLD_SQL} as oversold, {available_sql('p')} as product_available "
            f"from wr join products p on p.id = wr.id where {cond} order by {order} limit :limit offset :offset"
        ),
        params,
    ).all()
    return {
        "total": int(total or 0),
        "low_count": int(counts.low_count) if counts else 0,
        "out_count": int(counts.out_count) if counts else 0,
        "is_main": kind == "main",
        "items": [dict(r._mapping) for r in rows],
    }


@router.get("/all/stock")
@needs(*ANY_INVENTORY)
def all_warehouses_stock(
    q: Optional[str] = Query(None, max_length=100),
    view: str = Query("all", pattern="^(all|low|out)$"),
    status: str = Query("active", pattern="^(all|active|inactive)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Every part with its total across all warehouses (read-only; counts are changed inside a warehouse)."""
    _ensure_warehouses(db)
    return _stock_response(db, "all", 0, q, view, status, limit, offset)


@router.get("/{warehouse_id}/stock")
@needs(*ANY_INVENTORY)
def warehouse_stock_list(
    warehouse_id: int,
    q: Optional[str] = Query(None, max_length=100),
    view: str = Query("all", pattern="^(all|low|out)$"),
    status: str = Query("active", pattern="^(all|active|inactive)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    _ensure_warehouses(db)
    w = _warehouse(db, warehouse_id)
    return _stock_response(db, "main" if w.is_default else "other", warehouse_id, q, view, status, limit, offset)


class QtyIn(BaseModel):
    quantity: int = Field(..., ge=0, le=MAX_QTY)


@router.put("/{warehouse_id}/stock/{product_id}")
@needs("inventory.adjust")
def set_warehouse_stock(
    warehouse_id: int, product_id: int, body: QtyIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)
):
    """Set (or add a part to) this warehouse's count. The total on hand moves by the same amount. 0 keeps the part listed."""
    _ensure_warehouses(db)
    w = _warehouse(db, warehouse_id)
    try:
        prod = db.execute(
            text("select id, coalesce(quantity_on_hand, 0) as total from products where id = :p for update"), {"p": product_id}
        ).first()
        if not prod:
            db.rollback()
            raise HTTPException(404, "No such product.")
        others = int(db.execute(text("select coalesce(sum(quantity), 0) from warehouse_stock where product_id = :p"), {"p": product_id}).scalar() or 0)
        existed = True
        if w.is_default:
            old = max(0, prod.total - others)
        else:
            cur = db.execute(
                text("select quantity from warehouse_stock where warehouse_id = :w and product_id = :p"),
                {"w": warehouse_id, "p": product_id},
            ).first()
            existed = cur is not None
            old = int(cur.quantity) if cur else 0
        delta = body.quantity - old
        if delta == 0 and existed:
            db.rollback()
            return {"ok": True, "quantity": body.quantity, "total": prod.total}
        new_total = prod.total + delta
        if new_total > MAX_QTY:
            db.rollback()
            raise HTTPException(400, f"Stock can't be more than {MAX_QTY}.")
        if not w.is_default:
            db.execute(
                text(
                    "insert into warehouse_stock (warehouse_id, product_id, quantity, updated_by) values (:w, :p, :q, :u) "
                    "on conflict (warehouse_id, product_id) do update set quantity = :q, updated_at = now(), updated_by = :u"
                ),
                {"w": warehouse_id, "p": product_id, "q": body.quantity, "u": user.email},
            )
        if delta != 0:
            db.execute(
                text("update products set quantity_on_hand = :t, updated_at = now() where id = :p"), {"t": new_total, "p": product_id}
            )
            db.execute(
                text(
                    "insert into inventory_transactions (product_id, transaction_type, quantity_change, reference, legacy_created_by) "
                    "values (:p, 'Adjustment', :d, :ref, :by)"
                ),
                {"p": product_id, "d": delta, "ref": f"{w.name} count set to {body.quantity}"[:200], "by": user.email},
            )
            queue_stock_push(db, [product_id])  # the total changed, so every eBay listing of the part is updated
        if not existed:
            log_change(db, user.email, w, product_id, "added", None, body.quantity)
        else:
            log_change(db, user.email, w, product_id, "count", old, body.quantity)
        db.commit()
        return {"ok": True, "quantity": body.quantity, "total": new_total}
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        log.exception("warehouse stock change failed")
        raise HTTPException(500, f"That did not work and nothing was changed. ({type(exc).__name__})")


@router.delete("/{warehouse_id}/stock/{product_id}")
@needs("inventory.adjust")
def remove_warehouse_part(warehouse_id: int, product_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    """Take a part out of a warehouse. Whatever was counted there leaves the total on hand too."""
    _ensure_warehouses(db)
    w = _warehouse(db, warehouse_id)
    if w.is_default:
        raise HTTPException(400, "The main warehouse holds every part; set its count to 0 instead.")
    try:
        prod = db.execute(
            text("select id, coalesce(quantity_on_hand, 0) as total from products where id = :p for update"), {"p": product_id}
        ).first()
        cur = db.execute(
            text("select quantity from warehouse_stock where warehouse_id = :w and product_id = :p"),
            {"w": warehouse_id, "p": product_id},
        ).first()
        if not prod or not cur:
            db.rollback()
            raise HTTPException(404, "That part is not in this warehouse.")
        qty = int(cur.quantity)
        db.execute(text("delete from warehouse_stock where warehouse_id = :w and product_id = :p"), {"w": warehouse_id, "p": product_id})
        if qty > 0:
            db.execute(
                text("update products set quantity_on_hand = :t, updated_at = now() where id = :p"),
                {"t": max(0, prod.total - qty), "p": product_id},
            )
            db.execute(
                text(
                    "insert into inventory_transactions (product_id, transaction_type, quantity_change, reference, legacy_created_by) "
                    "values (:p, 'Adjustment', :d, :ref, :by)"
                ),
                {"p": product_id, "d": -qty, "ref": f"Removed from {w.name}"[:200], "by": user.email},
            )
            queue_stock_push(db, [product_id])
        log_change(db, user.email, w, product_id, "removed", qty, None)
        db.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        log.exception("warehouse part removal failed")
        raise HTTPException(500, f"That did not work and nothing was changed. ({type(exc).__name__})")


class LevelIn(BaseModel):
    reorder_point: Optional[int] = Field(None, ge=0, le=MAX_QTY)  # empty / 0 = no restock level


def _lock_part(db: Session, w, product_id: int):
    """The part's current restock level in this warehouse (None if it isn't listed there)."""
    if w.is_default:
        return db.execute(text("select reorder_point from products where id = :p for update"), {"p": product_id}).first()
    return db.execute(
        text("select reorder_point from warehouse_stock where warehouse_id = :w and product_id = :p for update"),
        {"w": w.id, "p": product_id},
    ).first()


@router.put("/{warehouse_id}/stock/{product_id}/restock-level")
@needs("inventory.adjust")
def set_part_restock_level(
    warehouse_id: int, product_id: int, body: LevelIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)
):
    _ensure_warehouses(db)
    w = _warehouse(db, warehouse_id)
    value = body.reorder_point or None
    cur = _lock_part(db, w, product_id)
    if not cur:
        db.rollback()
        raise HTTPException(404, "That part is not in this warehouse.")
    if (cur.reorder_point or None) != value:
        if w.is_default:
            db.execute(text("update products set reorder_point = :v, updated_at = now() where id = :p"), {"v": value, "p": product_id})
        else:
            db.execute(
                text("update warehouse_stock set reorder_point = :v, updated_at = now(), updated_by = :u where warehouse_id = :w and product_id = :p"),
                {"v": value, "u": user.email, "w": warehouse_id, "p": product_id},
            )
        log_change(db, user.email, w, product_id, "restock", cur.reorder_point, value)
    db.commit()
    return {"reorder_point": value}
