"""
Inventory control: barcodes, receiving (scan in) and shipping (scan out).
Mounted at /admin/inventory/* (admins, plus employees holding the matching permission).

HOW STOCK IS COUNTED - read this before changing anything
  * products.quantity_on_hand is what the store can SELL. A website order takes its stock the
    moment Stripe confirms payment (store_checkout._mark_paid), and eBay sales take theirs
    elsewhere. So SCANNING A PART OUT NEVER SUBTRACTS STOCK A SECOND TIME. The ship scan
    checks the pick (right part, right quantity, nothing missing), records who shipped what,
    and marks the website order shipped.
  * Receiving scans ADD stock, and write an inventory_transactions ledger row.
  * Every commit is ONE database transaction, tagged with a batch id made by the browser, so a
    double click or a retry after a dropped connection can never be applied twice.

Routes
  GET    /inventory/lookup?code=            what product does this scanned code mean?
  GET    /inventory/products/search?q=      find a product to link a barcode to
  GET    /inventory/products/{id}/barcodes  a product and every barcode linked to it
  POST   /inventory/products/{id}/barcodes  link a barcode (box label, UPC, part label...)
  DELETE /inventory/barcodes/{id}           unlink a barcode
  POST   /inventory/products/{id}/shelf-barcode   make (or fetch) the shelf label code
  PUT    /inventory/products/{id}/bin       set the shelf / bin location text
  POST   /inventory/receive                 commit a receiving batch (adds stock)
  GET    /inventory/orders/to-ship          paid website orders not yet shipped
  GET    /inventory/orders/{order_number}   one order with what is still to scan
  POST   /inventory/ship                    commit a shipping batch
  GET    /inventory/history                 recent batches (audit trail)
"""
import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user, needs, require_route_access
from app.database import get_db
from app.images import thumb_url

# No prefix for "/admin": main.py mounts this inside admin_router. require_route_access makes
# every route admin-only unless it carries a @needs(...) marker.
router = APIRouter(prefix="/inventory", tags=["admin-inventory"], dependencies=[Depends(require_route_access)])

ANY_INVENTORY = ("inventory.receive", "inventory.ship", "inventory.barcodes", "inventory.adjust")
KINDS = {"shelf", "box", "part", "upc", "other"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_QTY = 100000


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _clean_code(raw: str) -> str:
    code = (raw or "").strip()
    if not code or len(code) > 128 or _CONTROL.search(code):
        raise HTTPException(400, "That barcode is empty, too long, or contains hidden control characters")
    return code


def _product_row(db: Session, product_id: int):
    row = db.execute(
        text("select id, sku, title, quantity_on_hand, bin_location from products where id = :id"),
        {"id": product_id},
    ).first()
    if not row:
        raise HTTPException(404, "Product not found")
    return row


def _thumb(db: Session, product_id: int) -> Optional[str]:
    r = db.execute(
        text(
            "select image_key, source_url from product_photos where product_id = :id "
            "order by is_primary desc, sort_order, id limit 1"
        ),
        {"id": product_id},
    ).first()
    if not r:
        return None
    return thumb_url(r.image_key) or r.source_url


def _prod(db: Session, row) -> dict:
    return {
        "id": row.id,
        "sku": row.sku,
        "title": row.title,
        "quantity_on_hand": row.quantity_on_hand or 0,
        "bin_location": row.bin_location,
        "thumb": _thumb(db, row.id),
    }


def _barcode_owner(db: Session, code: str):
    return db.execute(
        text(
            "select b.id, b.code, b.kind, b.label, b.product_id, p.sku, p.title "
            "from product_barcodes b join products p on p.id = b.product_id where b.code = :c"
        ),
        {"c": code},
    ).first()


def _merge_lines(lines: list) -> dict[int, dict]:
    """Same product scanned on several lines becomes one line."""
    merged: dict[int, dict] = {}
    for l in lines:
        m = merged.setdefault(l.product_id, {"qty": 0, "barcode_id": l.barcode_id})
        m["qty"] += l.quantity
        if m["barcode_id"] is None:
            m["barcode_id"] = l.barcode_id
    for pid, m in merged.items():
        if m["qty"] > MAX_QTY:
            raise HTTPException(400, f"Quantity for product {pid} is too large")
    return merged


def _open_batch(db: Session, batch_id: uuid.UUID, kind: str, user: AuthUser, reference, note, order_id) -> None:
    """Creates the batch row. The batch id is the primary key, so a repeat is refused here."""
    try:
        db.execute(
            text(
                "insert into inventory_batches (id, kind, created_by_user_id, created_by_email, reference, note, order_id) "
                "values (cast(:id as uuid), :kind, cast(:uid as uuid), :email, :ref, :note, cast(:oid as bigint))"
            ),
            {
                "id": str(batch_id),
                "kind": kind,
                "uid": user.user_id,
                "email": user.email,
                "ref": (reference or "").strip() or None,
                "note": (note or "").strip() or None,
                "oid": order_id,
            },
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Already saved - this batch went through the first time, nothing was added twice.")


def _insert_line(db: Session, batch_id: uuid.UUID, product_id: int, barcode_id: Optional[int], qty: int) -> None:
    # The barcode is recorded only if it really belongs to that product.
    db.execute(
        text(
            "insert into inventory_batch_lines (batch_id, product_id, barcode_id, quantity) "
            "select cast(:b as uuid), :p, "
            "       (select id from product_barcodes where id = cast(:bid as bigint) and product_id = :p), :q"
        ),
        {"b": str(batch_id), "p": product_id, "bid": barcode_id, "q": qty},
    )


# --------------------------------------------------------------------------
# lookup + search
# --------------------------------------------------------------------------
@router.get("/lookup")
@needs(*ANY_INVENTORY)
def lookup(code: str = Query(..., min_length=1, max_length=200), db: Session = Depends(get_db)):
    c = code.strip()
    hit = db.execute(
        text(
            "select b.id as barcode_id, b.code, b.kind, b.product_id "
            "from product_barcodes b where b.code = :c"
        ),
        {"c": c},
    ).first()
    if hit:
        row = _product_row(db, hit.product_id)
        return {
            "found": True,
            "matched_by": "barcode",
            "product": _prod(db, row),
            "barcode": {"id": hit.barcode_id, "code": hit.code, "kind": hit.kind},
        }
    # A label that just says the SKU also works, as long as it is unambiguous.
    by_sku = db.execute(text("select id from products where lower(sku) = lower(:c) limit 2"), {"c": c}).all()
    if len(by_sku) == 1:
        row = _product_row(db, by_sku[0].id)
        return {"found": True, "matched_by": "sku", "product": _prod(db, row), "barcode": None}
    return {"found": False}


@router.get("/products/search")
@needs(*ANY_INVENTORY)
def search_products(
    q: str = Query(..., min_length=2, max_length=100),
    limit: int = Query(25, le=50),
    db: Session = Depends(get_db),
):
    words = [w for w in re.split(r"\s+", q.strip()) if w][:6]
    if not words:
        return {"items": []}
    params: dict = {"limit": limit}
    conds = []
    for i, w in enumerate(words):
        params[f"w{i}"] = f"%{w}%"
        conds.append(
            f"(p.title ilike cast(:w{i} as text) or p.sku ilike cast(:w{i} as text) "
            f"or p.manufacturer_part_number ilike cast(:w{i} as text) "
            f"or p.internal_part_number ilike cast(:w{i} as text) "
            f"or p.bin_location ilike cast(:w{i} as text) "
            f"or exists (select 1 from product_barcodes b where b.product_id = p.id "
            f"           and b.code ilike cast(:w{i} as text)))"
        )
    rows = db.execute(
        text(
            "select p.id, p.sku, p.title, p.quantity_on_hand, p.bin_location, "
            "       (select count(*) from product_barcodes b where b.product_id = p.id) as barcode_count "
            f"from products p where {' and '.join(conds)} order by p.title limit :limit"
        ),
        params,
    ).all()
    return {"items": [dict(r._mapping) for r in rows]}


# --------------------------------------------------------------------------
# barcodes
# --------------------------------------------------------------------------
@router.get("/products/{product_id}/barcodes")
@needs(*ANY_INVENTORY)
def list_barcodes(product_id: int, db: Session = Depends(get_db)):
    p = _product_row(db, product_id)
    rows = db.execute(
        text(
            "select id, code, kind, label, is_auto, created_by_email, created_at "
            "from product_barcodes where product_id = :id order by id"
        ),
        {"id": product_id},
    ).all()
    return {"product": _prod(db, p), "barcodes": [dict(r._mapping) for r in rows]}


class BarcodeIn(BaseModel):
    code: str = Field(..., max_length=200)
    kind: str = "other"
    label: Optional[str] = Field(None, max_length=80)


@router.post("/products/{product_id}/barcodes")
@needs("inventory.barcodes")
def add_barcode(
    product_id: int,
    body: BarcodeIn,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(current_user),
):
    code = _clean_code(body.code)
    if body.kind not in KINDS:
        raise HTTPException(400, "Unknown barcode type")
    _product_row(db, product_id)
    owner = _barcode_owner(db, code)
    if owner:
        if owner.product_id == product_id:
            return {"id": owner.id, "code": owner.code, "kind": owner.kind, "already": True}
        raise HTTPException(409, f"That barcode is already linked to {owner.sku} - {owner.title}")
    try:
        row = db.execute(
            text(
                "insert into product_barcodes (product_id, code, kind, label, created_by_email) "
                "values (:p, :c, :k, :l, :by) returning id, code, kind"
            ),
            {"p": product_id, "c": code, "k": body.kind, "l": (body.label or "").strip() or None, "by": user.email},
        ).first()
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That barcode was just linked to a product by someone else")
    return {"id": row.id, "code": row.code, "kind": row.kind, "already": False}


@router.delete("/barcodes/{barcode_id}")
@needs("inventory.barcodes")
def delete_barcode(barcode_id: int, db: Session = Depends(get_db)):
    gone = db.execute(text("delete from product_barcodes where id = :id returning id"), {"id": barcode_id}).first()
    if not gone:
        raise HTTPException(404, "Barcode not found")
    db.commit()
    return {"ok": True}


@router.post("/products/{product_id}/shelf-barcode")
@needs("inventory.barcodes")
def ensure_shelf_barcode(
    product_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(current_user),
):
    """The code printed on the shelf label. Made once per product, then reused forever."""
    p = _product_row(db, product_id)
    row = db.execute(
        text(
            "select code from product_barcodes where product_id = :id and kind = 'shelf' and is_auto "
            "order by id limit 1"
        ),
        {"id": product_id},
    ).first()
    created = False
    if row:
        code = row.code
    else:
        code = f"EI-{product_id:06d}"
        owner = _barcode_owner(db, code)
        if owner and owner.product_id != product_id:
            code = f"EI-{product_id:06d}-{uuid.uuid4().hex[:4].upper()}"  # never expected; stays unique anyway
            owner = None
        if not owner:
            try:
                db.execute(
                    text(
                        "insert into product_barcodes (product_id, code, kind, label, is_auto, created_by_email) "
                        "values (:p, :c, 'shelf', 'Shelf label', true, :by)"
                    ),
                    {"p": product_id, "c": code, "by": user.email},
                )
                db.commit()
                created = True
            except IntegrityError:
                db.rollback()
                raise HTTPException(409, "Could not make the shelf code. Try again.")
    return {"code": code, "created": created, "product": _prod(db, p)}


class BinIn(BaseModel):
    bin_location: Optional[str] = Field(None, max_length=40)


@router.put("/products/{product_id}/bin")
@needs("inventory.barcodes", "inventory.adjust")
def set_bin(product_id: int, body: BinIn, db: Session = Depends(get_db)):
    _product_row(db, product_id)
    value = (body.bin_location or "").strip() or None
    db.execute(
        text("update products set bin_location = :b, updated_at = now() where id = :id"),
        {"b": value, "id": product_id},
    )
    db.commit()
    return {"bin_location": value}


# --------------------------------------------------------------------------
# receiving (adds stock)
# --------------------------------------------------------------------------
class LineIn(BaseModel):
    product_id: int
    quantity: int = Field(..., ge=1, le=MAX_QTY)
    barcode_id: Optional[int] = None


class ReceiveIn(BaseModel):
    batch_id: uuid.UUID
    lines: list[LineIn] = Field(..., min_length=1, max_length=500)
    reference: Optional[str] = Field(None, max_length=100)  # packing slip / PO number
    note: Optional[str] = Field(None, max_length=500)


@router.post("/receive")
@needs("inventory.receive")
def receive(body: ReceiveIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    merged = _merge_lines(body.lines)
    ids = sorted(merged)
    try:
        # Lock the products so two people receiving the same part can't overwrite each other.
        locked = db.execute(
            text("select id from products where id = any(cast(:ids as bigint[])) order by id for update"),
            {"ids": ids},
        ).all()
        if len(locked) != len(ids):
            db.rollback()
            raise HTTPException(404, "One of these products no longer exists. Nothing was received - scan again.")

        _open_batch(db, body.batch_id, "receive", user, body.reference, body.note, None)

        ref = f"Receive {str(body.batch_id)[:8]}"
        if (body.reference or "").strip():
            ref += f" - {body.reference.strip()}"
        result = []
        for pid in ids:
            m = merged[pid]
            new_qty = db.execute(
                text(
                    "update products set quantity_on_hand = coalesce(quantity_on_hand, 0) + :q, updated_at = now() "
                    "where id = :id returning quantity_on_hand"
                ),
                {"q": m["qty"], "id": pid},
            ).scalar()
            db.execute(
                text(
                    "insert into inventory_transactions "
                    "(product_id, transaction_type, quantity_change, reference, legacy_created_by) "
                    "values (:p, 'Receiving', :q, :ref, :by)"
                ),
                {"p": pid, "q": m["qty"], "ref": ref[:200], "by": user.email},
            )
            _insert_line(db, body.batch_id, pid, m["barcode_id"], m["qty"])
            result.append({"product_id": pid, "received": m["qty"], "quantity_on_hand": new_qty})
        db.commit()
        return {"batch_id": str(body.batch_id), "lines": result}
    except HTTPException:
        raise
    except Exception as exc:  # any surprise = nothing changed
        db.rollback()
        raise HTTPException(500, f"Receiving failed and was rolled back; no stock changed. ({type(exc).__name__})")


# --------------------------------------------------------------------------
# shipping (verifies the pick; does NOT subtract stock again - see top of file)
# --------------------------------------------------------------------------
def _order_by_number(db: Session, order_number: str):
    return db.execute(
        text(
            "select id, order_number, status, customer_name, shipping_address, notes, paid_at, shipped_at "
            "from orders where lower(order_number) = lower(:n)"
        ),
        {"n": order_number.strip()},
    ).first()


def _ordered_and_shipped(db: Session, order_id: int) -> tuple[dict, dict]:
    ordered = {
        r.product_id: int(r.q)
        for r in db.execute(
            text("select product_id, sum(quantity) as q from order_items where order_id = :o group by product_id"),
            {"o": order_id},
        ).all()
        if r.product_id is not None
    }
    shipped = {
        r.product_id: int(r.q)
        for r in db.execute(
            text(
                "select l.product_id, sum(l.quantity) as q from inventory_batch_lines l "
                "join inventory_batches b on b.id = l.batch_id "
                "where b.order_id = :o and b.kind = 'ship' group by l.product_id"
            ),
            {"o": order_id},
        ).all()
    }
    return ordered, shipped


@router.get("/orders/to-ship")
@needs("inventory.ship")
def orders_to_ship(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select o.order_number, o.customer_name, o.paid_at, "
            "       coalesce((select sum(oi.quantity) from order_items oi where oi.order_id = o.id), 0) as units "
            "from orders o where o.status = 'paid' and o.shipped_at is null "
            "order by o.paid_at nulls last, o.id limit 100"
        )
    ).all()
    return {"items": [dict(r._mapping) for r in rows]}


@router.get("/orders/{order_number}")
@needs("inventory.ship")
def get_order(order_number: str, db: Session = Depends(get_db)):
    o = _order_by_number(db, order_number)
    if not o:
        raise HTTPException(404, "No order with that number")
    items = db.execute(
        text(
            "select oi.id as item_id, oi.product_id, oi.title, oi.quantity, p.sku, p.bin_location "
            "from order_items oi left join products p on p.id = oi.product_id "
            "where oi.order_id = :o order by oi.id"
        ),
        {"o": o.id},
    ).all()
    _, shipped = _ordered_and_shipped(db, o.id)
    addr = o.shipping_address or {}
    return {
        "order_number": o.order_number,
        "status": o.status,
        "customer_name": o.customer_name,
        "paid_at": o.paid_at,
        "shipped_at": o.shipped_at,
        "shipping_method": (o.notes or "").removeprefix("Shipping: ").split(" | ")[0] or None,
        "ship_to": {
            "name": addr.get("full_name"),
            "line1": addr.get("line1"),
            "line2": addr.get("line2"),
            "city": addr.get("city"),
            "region": addr.get("region"),
            "postal_code": addr.get("postal_code"),
        },
        "items": [
            {
                "item_id": i.item_id,
                "product_id": i.product_id,
                "sku": i.sku,
                "title": i.title,
                "bin_location": i.bin_location,
                "ordered": i.quantity,
                "shipped_before": shipped.get(i.product_id, 0) if i.product_id is not None else 0,
                "thumb": _thumb(db, i.product_id) if i.product_id is not None else None,
            }
            for i in items
        ],
    }


class ShipIn(BaseModel):
    batch_id: uuid.UUID
    order_number: Optional[str] = Field(None, max_length=40)   # website order, or leave empty...
    reference: Optional[str] = Field(None, max_length=100)     # ...and give the eBay / other order number
    lines: list[LineIn] = Field(..., min_length=1, max_length=500)
    partial: bool = False                                       # ship what's here, finish the order later
    note: Optional[str] = Field(None, max_length=500)


@router.post("/ship")
@needs("inventory.ship")
def ship(body: ShipIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    merged = _merge_lines(body.lines)
    try:
        order = None
        complete = False
        remaining_after: dict[int, int] = {}
        if body.order_number:
            order = db.execute(
                text(
                    "select id, order_number, status, shipped_at from orders "
                    "where lower(order_number) = lower(:n) for update"
                ),
                {"n": body.order_number.strip()},
            ).first()
            if not order:
                db.rollback()
                raise HTTPException(404, "No order with that number")
            if order.status != "paid":
                db.rollback()
                raise HTTPException(409, f"Order {order.order_number} is '{order.status}', not paid - it can't be shipped.")
            if order.shipped_at:
                db.rollback()
                raise HTTPException(409, f"Order {order.order_number} was already marked shipped.")
            ordered, shipped = _ordered_and_shipped(db, order.id)
            for pid, m in merged.items():
                if pid not in ordered:
                    db.rollback()
                    raise HTTPException(400, "A scanned part is not on this order.")
                left = ordered[pid] - shipped.get(pid, 0)
                if m["qty"] > left:
                    db.rollback()
                    raise HTTPException(400, f"Too many scanned for a part on this order (only {left} still to ship).")
            remaining_after = {
                pid: ordered[pid] - shipped.get(pid, 0) - merged.get(pid, {"qty": 0})["qty"] for pid in ordered
            }
            complete = all(v == 0 for v in remaining_after.values())
            if not complete and not body.partial:
                db.rollback()
                raise HTTPException(409, "Not every item on the order has been scanned yet.")
        elif not (body.reference or "").strip():
            raise HTTPException(400, "Enter the eBay (or other) order number so this shipment can be traced.")

        _open_batch(
            db, body.batch_id, "ship", user,
            body.reference or (order.order_number if order else None), body.note,
            order.id if order else None,
        )
        for pid, m in merged.items():
            _insert_line(db, body.batch_id, pid, m["barcode_id"], m["qty"])
        if order and complete:
            db.execute(
                text("update orders set shipped_at = now(), shipped_by = :by where id = :id"),
                {"by": user.email, "id": order.id},
            )
        db.commit()
        return {
            "batch_id": str(body.batch_id),
            "order_complete": complete,
            "remaining": {str(k): v for k, v in remaining_after.items() if v > 0},
        }
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, f"Shipping record failed and was rolled back; nothing changed. ({type(exc).__name__})")


# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------
@router.get("/history")
@needs(*ANY_INVENTORY)
def history(
    kind: Optional[str] = Query(None, pattern="^(receive|ship)$"),
    limit: int = Query(40, le=200),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text(
            """
            select b.id, b.kind, b.reference, b.note, b.created_by_email, b.created_at, o.order_number,
                   coalesce(jsonb_agg(jsonb_build_object(
                       'product_id', l.product_id, 'sku', p.sku, 'title', p.title, 'quantity', l.quantity)
                       order by l.id) filter (where l.id is not null), '[]'::jsonb) as lines
            from inventory_batches b
            left join orders o on o.id = b.order_id
            left join inventory_batch_lines l on l.batch_id = b.id
            left join products p on p.id = l.product_id
            where (cast(:kind as text) is null or b.kind = cast(:kind as text))
            group by b.id, o.order_number
            order by b.created_at desc
            limit :limit
            """
        ),
        {"kind": kind, "limit": limit},
    ).all()
    return {"items": [{**dict(r._mapping), "id": str(r.id)} for r in rows]}
