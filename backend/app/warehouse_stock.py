"""More warehouses: contact details, adding a warehouse, and the stock kept in each one (Inventory > Warehouses / Stock).

  POST /inventory/warehouses                       add a warehouse (name, address, manager, phone, email, notes)
  PUT  /inventory/warehouses/{id}/details          change a warehouse's name and details
  GET  /inventory/warehouses/{id}/stock            the parts a warehouse holds (any warehouse, the main one too)
  PUT  /inventory/warehouses/{id}/stock/{product}  set how many of a part a warehouse holds (0 removes it)

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
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.admin_inventory import WAREHOUSE_NAME_MAX, _CONTROL, MAX_QTY, _ensure_warehouses
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
                        delete from warehouse_stock where product_id = new.id and quantity <= 0;
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


@router.get("/{warehouse_id}/stock")
@needs("inventory.receive", "inventory.ship", "inventory.barcodes", "inventory.adjust")
def warehouse_stock_list(
    warehouse_id: int,
    q: Optional[str] = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    _ensure_warehouses(db)
    w = _warehouse(db, warehouse_id)
    params: dict = {"w": warehouse_id, "limit": limit, "offset": offset}
    conds = []
    for i, word in enumerate([x for x in (q or "").split() if x][:6]):
        params[f"q{i}"] = f"%{word}%"
        conds.append(f"(p.title ilike cast(:q{i} as text) or p.sku ilike cast(:q{i} as text) or p.bin_location ilike cast(:q{i} as text))")
    extra = (" and " + " and ".join(conds)) if conds else ""
    if w.is_default:
        # the main warehouse holds whatever the other warehouses do not
        frm = f"from products p left join {_OTHERS} o on o.product_id = p.id"
        where = f"coalesce(p.is_active, true) and coalesce(p.quantity_on_hand, 0) - coalesce(o.s, 0) > 0{extra}"
        qty, upd, by = "coalesce(p.quantity_on_hand, 0) - coalesce(o.s, 0)", "p.updated_at", "null::text"
    else:
        frm = "from warehouse_stock s join products p on p.id = s.product_id"
        where = f"s.warehouse_id = :w and s.quantity > 0{extra}"
        qty, upd, by = "s.quantity", "s.updated_at", "s.updated_by"
    total = db.execute(text(f"select count(*) {frm} where {where}"), params).scalar()
    rows = db.execute(
        text(
            f"select p.id, p.sku_display as sku, p.title, {qty} as quantity, p.bin_location, {upd} as updated_at, {by} as updated_by "
            f"{frm} where {where} order by p.title, p.id limit :limit offset :offset"
        ),
        params,
    ).all()
    return {"total": int(total or 0), "is_main": bool(w.is_default), "items": [dict(r._mapping) for r in rows]}


class QtyIn(BaseModel):
    quantity: int = Field(..., ge=0, le=MAX_QTY)


@router.put("/{warehouse_id}/stock/{product_id}")
@needs("inventory.adjust")
def set_warehouse_stock(
    warehouse_id: int, product_id: int, body: QtyIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)
):
    """Set (or add a part to) this warehouse's count. The total on hand moves by the same amount. 0 removes the part."""
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
        if w.is_default:
            old = prod.total - others
        else:
            old = int(
                db.execute(
                    text("select coalesce(quantity, 0) from warehouse_stock where warehouse_id = :w and product_id = :p"),
                    {"w": warehouse_id, "p": product_id},
                ).scalar()
                or 0
            )
        delta = body.quantity - old
        if delta == 0:
            db.rollback()
            return {"ok": True, "quantity": body.quantity, "total": prod.total}
        new_total = prod.total + delta
        if new_total > MAX_QTY:
            db.rollback()
            raise HTTPException(400, f"Stock can't be more than {MAX_QTY}.")
        if not w.is_default:
            if body.quantity == 0:
                db.execute(text("delete from warehouse_stock where warehouse_id = :w and product_id = :p"), {"w": warehouse_id, "p": product_id})
            else:
                db.execute(
                    text(
                        "insert into warehouse_stock (warehouse_id, product_id, quantity, updated_by) values (:w, :p, :q, :u) "
                        "on conflict (warehouse_id, product_id) do update set quantity = :q, updated_at = now(), updated_by = :u"
                    ),
                    {"w": warehouse_id, "p": product_id, "q": body.quantity, "u": user.email},
                )
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
        db.commit()
        return {"ok": True, "quantity": body.quantity, "total": new_total}
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        log.exception("warehouse stock change failed")
        raise HTTPException(500, f"That did not work and nothing was changed. ({type(exc).__name__})")
