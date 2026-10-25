"""
Purchase orders: what to reorder, who from, and a permanent searchable record of every PO.
Mounted at /admin/purchase-orders/* (owner / admin, plus staff holding "inventory.purchase").

  GET    /purchase-orders/parts            low and out-of-stock parts (or all parts) to pick from
  GET    /purchase-orders/distributors     the distributors you order from
  POST   /purchase-orders/distributors     add one
  PUT    /purchase-orders/distributors/{id}   edit one
  DELETE /purchase-orders/distributors/{id}   delete one (old POs keep their own copy of the vendor)
  POST   /purchase-orders                  create a PO from the ticked parts
  GET    /purchase-orders                  history, searchable (PO number, vendor, part number, SKU, title)
  GET    /purchase-orders/{id}             one PO with its lines
  GET    /purchase-orders/{id}/export?format=pdf|xlsx     download it again, any time

Creating a PO also marks each part on it Backordered (on_order = true), because a part can only be
Backordered once it is on a PO. That is ticked by default and can be un-ticked on the Stock tab.
The PO's "Item" column is the product's manufacturer part number (the field on the product page, or the
"Manufacturer Part Number" item specific when that field is empty), copied onto the PO when it is made.
"""
import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user, needs, require_route_access
from app.database import get_db
from app.po_export import DEFAULT_SHIP_TO, build_pdf, build_xlsx, po_total
from app.stock import available_sql, set_aside_sql

log = logging.getLogger(__name__)
router = APIRouter(prefix="/purchase-orders", tags=["admin-purchase-orders"], dependencies=[Depends(require_route_access)])

MAX_QTY = 100000

# The manufacturer part number: the product's own field, or the item specific of the same name.
_MPN = (
    "coalesce(nullif(btrim(p.manufacturer_part_number), ''), "
    "(select s.value from product_item_specifics s where s.product_id = p.id "
    "  and s.name = 'Manufacturer Part Number' order by s.sort_order, s.id limit 1))"
)


def _local_date(ts) -> date:
    """The calendar date in Michigan (so a PO made at 9pm doesn't print tomorrow's date)."""
    try:
        from zoneinfo import ZoneInfo

        return ts.astimezone(ZoneInfo("America/Detroit")).date()
    except Exception:
        return ts.date()


# --------------------------------------------------------------------------
# parts to reorder
# --------------------------------------------------------------------------
@router.get("/parts")
@needs("inventory.purchase")
def parts(
    q: Optional[str] = Query(None, max_length=100),
    show_all: bool = False,
    include_inactive: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Low and out-of-stock parts by default (parts with a restock level come first, biggest shortfall
    first). show_all lists every part so anything can be added to a PO."""
    avail = available_sql("p")
    params: dict = {"limit": limit, "offset": offset}
    conds = [] if include_inactive else ["coalesce(p.is_active, true)"]
    if not show_all:
        conds.append(f"({avail} <= 0 or (coalesce(p.reorder_point, 0) > 0 and {avail} <= p.reorder_point))")
    import re

    for i, w in enumerate([w for w in re.split(r"\s+", (q or "").strip()) if w][:6]):
        params[f"w{i}"] = f"%{w}%"
        conds.append(
            f"(p.title ilike cast(:w{i} as text) or p.sku ilike cast(:w{i} as text) "
            f"or p.manufacturer_part_number ilike cast(:w{i} as text) "
            f"or p.internal_part_number ilike cast(:w{i} as text) "
            f"or {_MPN} ilike cast(:w{i} as text))"
        )
    where = ("where " + " and ".join(conds)) if conds else ""
    total = db.execute(text(f"select count(*) from products p {where}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            select p.id, p.sku, p.title, {_MPN} as part_number, p.cost,
                   coalesce(p.quantity_on_hand, 0) as on_hand, {set_aside_sql('p')} as reserved,
                   {avail} as available, p.reorder_point, coalesce(p.on_order, false) as on_order,
                   (select array_agg(h.po_number order by h.po_number)
                      from po_lines l join po_headers h on h.id = l.po_id
                     where l.product_id = p.id and l.received_at is null) as open_pos
            from products p {where}
            order by (coalesce(p.reorder_point, 0) > 0) desc,
                     greatest(coalesce(p.reorder_point, 0) - {avail}, 0) desc, p.title, p.id
            limit :limit offset :offset
            """
        ),
        params,
    ).all()
    items = []
    for r in rows:
        d = dict(r._mapping)
        d["cost"] = float(d["cost"]) if d["cost"] is not None else None
        d["open_pos"] = d["open_pos"] or []
        rp = d["reorder_point"] or 0
        d["suggested_qty"] = max(1, rp - d["available"]) if rp > 0 else 1
        items.append(d)
    return {"total": total, "items": items}


# --------------------------------------------------------------------------
# distributors
# --------------------------------------------------------------------------
class DistributorIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    address: Optional[str] = Field(None, max_length=400)   # several lines: prints in the Vendor box
    email: Optional[str] = Field(None, max_length=200)
    phone: Optional[str] = Field(None, max_length=40)
    notes: Optional[str] = Field(None, max_length=500)


def _clean(body: DistributorIn) -> dict:
    return {
        "name": body.name.strip(),
        "address": (body.address or "").strip() or None,
        "email": (body.email or "").strip() or None,
        "phone": (body.phone or "").strip() or None,
        "notes": (body.notes or "").strip() or None,
    }


@router.get("/distributors")
@needs("inventory.purchase")
def list_distributors(db: Session = Depends(get_db)):
    rows = db.execute(
        text("select id, name, address, email, phone, notes from po_distributors order by lower(name), id")
    ).all()
    return {"items": [dict(r._mapping) for r in rows]}


@router.post("/distributors", status_code=201)
@needs("inventory.purchase")
def add_distributor(body: DistributorIn, db: Session = Depends(get_db)):
    row = db.execute(
        text(
            "insert into po_distributors (name, address, email, phone, notes) "
            "values (:name, :address, :email, :phone, :notes) returning id, name, address, email, phone, notes"
        ),
        _clean(body),
    ).first()
    db.commit()
    return dict(row._mapping)


@router.put("/distributors/{dist_id}")
@needs("inventory.purchase")
def edit_distributor(dist_id: int, body: DistributorIn, db: Session = Depends(get_db)):
    row = db.execute(
        text(
            "update po_distributors set name = :name, address = :address, email = :email, phone = :phone, "
            "notes = :notes where id = :id returning id, name, address, email, phone, notes"
        ),
        {**_clean(body), "id": dist_id},
    ).first()
    if not row:
        db.rollback()
        raise HTTPException(404, "That distributor no longer exists")
    db.commit()
    return dict(row._mapping)


@router.delete("/distributors/{dist_id}")
@needs("inventory.purchase")
def delete_distributor(dist_id: int, db: Session = Depends(get_db)):
    gone = db.execute(text("delete from po_distributors where id = :id returning id"), {"id": dist_id}).first()
    if not gone:
        db.rollback()
        raise HTTPException(404, "That distributor no longer exists")
    db.commit()  # purchase orders already made keep their own copy of the vendor name and address
    return {"ok": True}


# --------------------------------------------------------------------------
# create / list / read / export
# --------------------------------------------------------------------------
class PoLineIn(BaseModel):
    product_id: int
    quantity: int = Field(..., ge=1, le=MAX_QTY)
    rate: Optional[float] = Field(None, ge=0, le=1_000_000)   # empty = the product's cost
    description: Optional[str] = Field(None, max_length=250)  # empty = the product's title


class PoIn(BaseModel):
    distributor_id: int
    ship_to: Optional[str] = Field(None, max_length=400)
    note: Optional[str] = Field(None, max_length=300)
    expected_date: Optional[date] = None   # when the parts are expected; sets the backorder date on each part
    lines: list[PoLineIn] = Field(..., min_length=1, max_length=200)


def _po_detail(db: Session, po_id: int) -> dict:
    h = db.execute(
        text(
            "select id, po_number, distributor_id, vendor_name, vendor_address, ship_to, note, total_cents, "
            "       created_by, created_at from po_headers where id = :id"
        ),
        {"id": po_id},
    ).first()
    if not h:
        raise HTTPException(404, "That purchase order doesn't exist")
    lines = db.execute(
        text(
            "select l.id, l.product_id, p.sku, l.item, l.description, l.quantity, l.rate, l.received_at "
            "from po_lines l left join products p on p.id = l.product_id where l.po_id = :id order by l.id"
        ),
        {"id": po_id},
    ).all()
    out_lines = [
        {**dict(l._mapping), "rate": float(l.rate), "amount": float(Decimal(str(l.rate)) * l.quantity)} for l in lines
    ]
    return {
        **dict(h._mapping),
        "date": _local_date(h.created_at).isoformat(),
        "total": h.total_cents / 100,
        "lines": out_lines,
    }


@router.post("", status_code=201)
@needs("inventory.purchase")
def create_po(body: PoIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    dist = db.execute(
        text("select id, name, address from po_distributors where id = :id"), {"id": body.distributor_id}
    ).first()
    if not dist:
        raise HTTPException(404, "Pick a distributor first (that one no longer exists).")

    merged: dict[int, PoLineIn] = {}
    for l in body.lines:
        if l.product_id in merged:
            merged[l.product_id].quantity += l.quantity
        else:
            merged[l.product_id] = l.model_copy()
    ids = sorted(merged)
    try:
        prods = {
            r.id: r
            for r in db.execute(
                text(f"select p.id, p.title, p.cost, {_MPN} as part_number from products p "
                     "where p.id = any(cast(:ids as bigint[])) order by p.id for update of p"),
                {"ids": ids},
            ).all()
        }
        if len(prods) != len(ids):
            db.rollback()
            raise HTTPException(404, "One of those parts no longer exists. Nothing was ordered.")

        rows = []
        for pid in ids:
            l, p = merged[pid], prods[pid]
            rate = l.rate if l.rate is not None else (float(p.cost) if p.cost is not None else 0.0)
            rows.append(
                {
                    "product_id": pid,
                    "item": p.part_number,
                    "description": ((l.description or "").strip() or p.title or "")[:250],
                    "quantity": l.quantity,
                    "rate": rate,
                }
            )
        total = po_total(rows)
        header = db.execute(
            text(
                "insert into po_headers (distributor_id, vendor_name, vendor_address, ship_to, note, total_cents, created_by) "
                "values (:d, :vn, :va, :st, :note, :tc, :by) returning id, po_number"
            ),
            {
                "d": dist.id,
                "vn": dist.name,
                "va": dist.address,
                "st": (body.ship_to if body.ship_to is not None else DEFAULT_SHIP_TO).strip() or None,
                "note": (body.note or "").strip() or None,
                "tc": int(total * 100),
                "by": user.email,
            },
        ).first()
        for r in rows:
            db.execute(
                text(
                    "insert into po_lines (po_id, product_id, item, description, quantity, rate) "
                    "values (:po, :p, :item, :desc, :q, :rate)"
                ),
                {"po": header.id, "p": r["product_id"], "item": r["item"], "desc": r["description"],
                 "q": r["quantity"], "rate": r["rate"]},
            )
        # Backordered is ticked by default for every part on the new PO (it can be un-ticked on the Stock tab).
        db.execute(
            text(
                "update products set on_order = true, updated_at = now()"
                + (", expected_date = :d" if body.expected_date else "")
                + " where id = any(cast(:ids as bigint[]))"
            ),
            {"ids": ids, **({"d": body.expected_date} if body.expected_date else {})},
        )
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        log.exception("Creating a purchase order failed")
        raise HTTPException(500, f"The purchase order was not created; nothing changed. ({type(exc).__name__})")

    po = _po_detail(db, header.id)
    po["missing_part_numbers"] = [r["description"] for r in rows if not r["item"]]
    return po


@router.get("")
@needs("inventory.purchase")
def list_pos(
    q: Optional[str] = Query(None, max_length=100),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    import re

    params: dict = {"limit": limit, "offset": offset}
    conds = []
    for i, w in enumerate([w for w in re.split(r"\s+", (q or "").strip()) if w][:6]):
        params[f"w{i}"] = f"%{w}%"
        conds.append(
            f"(cast(h.po_number as text) ilike cast(:w{i} as text) or h.vendor_name ilike cast(:w{i} as text) "
            f"or exists (select 1 from po_lines l left join products p on p.id = l.product_id "
            f"  where l.po_id = h.id and (l.item ilike cast(:w{i} as text) or l.description ilike cast(:w{i} as text) "
            f"  or p.sku ilike cast(:w{i} as text))))"
        )
    where = ("where " + " and ".join(conds)) if conds else ""
    total = db.execute(text(f"select count(*) from po_headers h {where}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            select h.id, h.po_number, h.vendor_name, h.total_cents, h.created_by, h.created_at,
                   (select count(*) from po_lines l where l.po_id = h.id) as line_count,
                   (select count(*) from po_lines l where l.po_id = h.id and l.received_at is null) as open_lines
            from po_headers h {where} order by h.po_number desc limit :limit offset :offset
            """
        ),
        params,
    ).all()
    return {"total": total, "items": [dict(r._mapping) for r in rows]}


@router.get("/{po_id}")
@needs("inventory.purchase")
def get_po(po_id: int, db: Session = Depends(get_db)):
    return _po_detail(db, po_id)


@router.get("/{po_id}/export")
@needs("inventory.purchase")
def export_po(po_id: int, format: str = Query("pdf", pattern="^(pdf|xlsx)$"), db: Session = Depends(get_db)):
    po = _po_detail(db, po_id)
    data = {
        "po_number": po["po_number"],
        "date": date.fromisoformat(po["date"]),
        "vendor_name": po["vendor_name"],
        "vendor_address": po["vendor_address"],
        "ship_to": po["ship_to"],
        "note": po["note"],
        "lines": [
            {"item": l["item"], "description": l["description"], "quantity": l["quantity"], "rate": l["rate"]}
            for l in po["lines"]
        ],
    }
    if format == "pdf":
        body, media = build_pdf(data), "application/pdf"
    else:
        body, media = build_xlsx(data), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="PO-{po["po_number"]}.{format}"'},
    )
