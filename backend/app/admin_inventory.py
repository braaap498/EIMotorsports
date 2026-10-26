"""
Inventory control: barcodes, receiving (scan in) and shipping (scan out).
Mounted at /admin/inventory/* (admins, plus employees holding the matching permission).

HOW STOCK IS COUNTED - read this before changing anything (rules live in app/stock.py)
  * products.quantity_on_hand is what is PHYSICALLY ON THE SHELF.
  * A website order does not touch it when paid. It SETS PARTS ASIDE (stock_reservations), so the
    number customers can buy ("available") drops at once while the shelf count stays true.
  * COMMITTING A PICK (scan-out on the Ship tab) is what takes the parts off the shelf: on hand
    and the reservation both go down in one database transaction, and a ledger row is written.
    When every line is picked the order moves to the shipping stage (picked_at is set).
  * Mark shipped (with a tracking number) completes the order.
  * Cancelling an unshipped order releases its reservation; parts already picked go back on the
    shelf. Refunding is a separate, admin-only action through Stripe.
  * Orders paid BEFORE reservations existed (orders.reserved_stock = false) already had their stock
    taken at payment, so picking them takes nothing a second time.
  * eBay / other sales are decreased by whatever records them (outside this file); scanning those
    out only records who shipped what.
  * Receiving scans ADD stock and write an inventory_transactions ledger row. Parts that came on a purchase
    order are received AGAINST that PO, line by line (see "receiving a PO" below): the count is checked
    against what was ordered, a short or over count must be acknowledged (who + when is kept), and the PO
    becomes "received" once every line is closed. The plain "scan in without a PO" receive only adds stock;
    it never closes PO lines.
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
  POST   /inventory/receive                 commit a receiving batch with no PO (adds stock)
  GET    /inventory/receiving/pos           sent POs that still have lines to receive (searchable)
  GET    /inventory/receiving/pos/{id}      one PO with its lines, ready to scan against
  POST   /inventory/receiving/pos/{id}/lines/{line_id}/receive   receive the counted quantity for one PO line (closes it, or keeps it open if the rest is coming later)
  POST   /inventory/receiving/pos/{id}/extras   receive parts that were not on the PO (or more than ordered)
  GET    /inventory/orders/to-ship          paid website orders: to pick, and picked / ready to ship
  GET    /inventory/orders/{order_number}   one order with what is still to scan
  POST   /inventory/ship                    commit a scan-out batch (website order pick, or eBay / other)
  POST   /inventory/orders/{n}/short        flag a line that could not be picked (missing / damaged)
  POST   /inventory/orders/{n}/shipped      picked order -> shipped, with its tracking number
  POST   /inventory/orders/{n}/cancel       cancel an unshipped paid order (releases / restores stock)
  POST   /inventory/orders/{n}/refund       refund through Stripe (owner / admin only)
  PUT    /inventory/products/{id}/on-order  tick / untick Backordered (only while the part is on an open purchase order)
  PUT    /inventory/products/{id}/expected-date   the date the backordered part is expected (shown to customers)
  GET    /inventory/history                 recent batches (audit trail)
  GET    /inventory/stock                   every product with on-hand + restock level (search, low, out)
  POST   /inventory/products/{id}/adjust    manual +/- or set-the-count (writes an 'Adjustment' ledger row)
  PUT    /inventory/products/{id}/reorder-point   set or clear the restock level
"""
import logging
import re
import uuid
from datetime import date
from typing import Literal, Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user, needs, require_route_access
from app.database import get_db
from app.images import thumb_url
from app.stock import available_sql, lock_products, reserved_paid_sql, set_aside_sql

# No prefix for "/admin": main.py mounts this inside admin_router. require_route_access makes
# every route admin-only unless it carries a @needs(...) marker.
log = logging.getLogger(__name__)
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


def _clear_backorder_if_done(db: Session, product_id: int) -> None:
    """A part stops being Backordered (and loses its expected date) once no open, uncancelled PO line wants it."""
    db.execute(
        text(
            "update products p set on_order = false, expected_date = null, updated_at = now() "
            "where p.id = :p and not exists (select 1 from po_lines l join po_headers h on h.id = l.po_id "
            "  where l.product_id = p.id and l.received_at is null and h.status <> 'cancelled')"
        ),
        {"p": product_id},
    )


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
    q: str = Query("", max_length=100),
    limit: int = Query(25, le=50),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    # An empty q lists every product A to Z (the Barcodes & labels picker shows that before anything is typed).
    if len(q.strip()) == 1:
        raise HTTPException(400, "Type at least 2 characters to search")
    words = [w for w in re.split(r"\s+", q.strip()) if w][:6]
    params: dict = {"limit": limit, "offset": offset}
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
            f"from products p where {' and '.join(conds) or 'true'} order by p.title, p.id limit :limit offset :offset"
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
                    "update products set quantity_on_hand = coalesce(quantity_on_hand, 0) + :q, "
                    "updated_at = now() where id = :id returning quantity_on_hand"
                ),
                {"q": m["qty"], "id": pid},
            ).scalar()
            db.execute(
                text(
                    "insert into inventory_transactions "
                    "(product_id, transaction_type, quantity_change, reference, legacy_created_by) "
                    "values (:p, 'Receive', :q, :ref, :by)"
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
# receiving a PO (adds stock, line by line, and keeps the shortage / overage record)
# --------------------------------------------------------------------------
def _po_search_conds(q: Optional[str], params: dict) -> list[str]:
    conds = []
    for i, w in enumerate([w for w in re.split(r"\s+", (q or "").strip()) if w][:6]):
        params[f"w{i}"] = f"%{w}%"
        conds.append(
            f"(cast(h.po_number as text) ilike cast(:w{i} as text) or h.vendor_name ilike cast(:w{i} as text) "
            f"or exists (select 1 from po_lines l left join products p on p.id = l.product_id "
            f"  where l.po_id = h.id and (l.item ilike cast(:w{i} as text) or l.description ilike cast(:w{i} as text) "
            f"  or p.sku ilike cast(:w{i} as text))))"
        )
    return conds


@router.get("/receiving/pos")
@needs("inventory.receive")
def receiving_pos(q: Optional[str] = Query(None, max_length=100), db: Session = Depends(get_db)):
    """Purchase orders that were sent to the distributor and still have parts to receive. Oldest first."""
    params: dict = {}
    conds = ["h.status = 'open'", "h.sent_at is not null"] + _po_search_conds(q, params)
    rows = db.execute(
        text(
            f"""
            select h.id, h.po_number, h.vendor_name, h.sent_at, h.created_at,
                   count(l.id) as line_count,
                   count(l.id) filter (where l.received_at is null) as open_lines,
                   coalesce(sum(l.quantity - coalesce(l.received_qty, 0)) filter (where l.received_at is null), 0) as open_units
            from po_headers h join po_lines l on l.po_id = h.id
            where {" and ".join(conds)}
            group by h.id
            having count(l.id) filter (where l.received_at is null) > 0
            order by h.po_number
            limit 200
            """
        ),
        params,
    ).all()
    return {"items": [dict(r._mapping) for r in rows]}


def _receiving_detail(db: Session, po_id: int) -> dict:
    from app.admin_purchase_orders import _who

    h = db.execute(
        text("select id, po_number, vendor_name, status, sent_at, received_at from po_headers where id = :id"),
        {"id": po_id},
    ).first()
    if not h:
        raise HTTPException(404, "That purchase order doesn't exist.")
    rows = db.execute(
        text(
            "select l.id, l.product_id, l.item, l.description, l.quantity, l.received_at, l.received_qty, "
            "       p.id as pid, p.sku, p.title, p.quantity_on_hand, p.bin_location "
            "from po_lines l left join products p on p.id = l.product_id where l.po_id = :id order by l.id"
        ),
        {"id": po_id},
    ).all()
    lines = []
    for r in rows:
        product = (
            {
                "id": r.pid,
                "sku": r.sku,
                "title": r.title,
                "quantity_on_hand": r.quantity_on_hand or 0,
                "bin_location": r.bin_location,
                "thumb": _thumb(db, r.pid),
            }
            if r.pid is not None
            else None
        )
        lines.append(
            {
                "id": r.id,
                "product_id": r.product_id,
                "item": r.item,
                "description": r.description,
                "quantity": r.quantity,
                "received_at": r.received_at,
                "received_qty": r.received_qty,
                "product": product,
            }
        )
    issues = db.execute(
        text(
            "select id, kind, sku, description, expected_qty, received_qty, acknowledged_by, acknowledged_at "
            "from po_receiving_issues where po_id = :id order by id"
        ),
        {"id": po_id},
    ).all()
    return {
        **dict(h._mapping),
        "lines": lines,
        "issues": [
            {**dict(i._mapping), "acknowledged_by_name": _who(i.acknowledged_by)} for i in issues
        ],
    }


@router.get("/receiving/pos/{po_id}")
@needs("inventory.receive")
def receiving_po(po_id: int, db: Session = Depends(get_db)):
    return _receiving_detail(db, po_id)


class ReceiveLineIn(BaseModel):
    batch_id: uuid.UUID
    quantity: int = Field(..., ge=0, le=MAX_QTY)   # counted NOW, against what is still outstanding on the line
    acknowledged: bool = False                     # required when the count is not exactly what is outstanding
    # When the count is short: "error" = they won't send the rest, close the line and report the shortage;
    # "later" = the rest is expected on a later delivery, keep the line (and the PO) open.
    short_choice: Optional[Literal["error", "later"]] = None
    barcode_id: Optional[int] = None
    note: Optional[str] = Field(None, max_length=300)


def _record_issue(db: Session, po_id: int, line_id, product_id, kind: str, item, description, expected: int, got: int,
                  user: AuthUser, note) -> None:
    db.execute(
        text(
            "insert into po_receiving_issues (po_id, po_line_id, product_id, kind, sku, item, description, "
            "  expected_qty, received_qty, note, acknowledged_by, acknowledged_at) "
            "values (:po, :line, :p, :kind, (select sku from products where id = :p), :item, :desc, :e, :g, :note, :by, now())"
        ),
        {
            "po": po_id, "line": line_id, "p": product_id, "kind": kind, "item": item,
            "desc": (description or "")[:250], "e": expected, "g": got,
            "note": (note or "").strip() or None, "by": user.email,
        },
    )


@router.post("/receiving/pos/{po_id}/lines/{line_id}/receive")
@needs("inventory.receive")
def receive_po_line(
    po_id: int, line_id: int, body: ReceiveLineIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)
):
    """Receive the quantity counted for ONE line of a PO. It is added to on hand (available follows, because
    available is on hand minus what is set aside). The count is compared with what is still outstanding on the line:
      * exactly right      -> the line is closed.
      * over               -> acknowledge it; the line is closed and an overage is reported.
      * short, "error"     -> acknowledge it; the line is closed and a shortage is reported (the distributor couldn't
                              or won't send the rest).
      * short, "later"     -> acknowledge it; what arrived is added to stock, the line STAYS OPEN for the rest and
                              nothing goes on the shortage report. A later delivery is received against the same line.
    Every acknowledgement keeps who and when (po_line_receipts, and po_receiving_issues for a reported problem).
    When no open line is left the PO becomes 'received'."""
    try:
        h = db.execute(
            text("select id, po_number, status, sent_at from po_headers where id = :id for update"), {"id": po_id}
        ).first()
        if not h:
            db.rollback()
            raise HTTPException(404, "That purchase order doesn't exist.")
        if h.status == "cancelled":
            db.rollback()
            raise HTTPException(409, f"Purchase order {h.po_number} was cancelled. Nothing was received.")
        if h.sent_at is None:
            db.rollback()
            raise HTTPException(409, f"Purchase order {h.po_number} is not marked as sent yet. Nothing was received.")
        line = db.execute(
            text(
                "select id, product_id, item, description, quantity, received_at, coalesce(received_qty, 0) as got "
                "from po_lines where id = :l and po_id = :po for update"
            ),
            {"l": line_id, "po": po_id},
        ).first()
        if not line:
            db.rollback()
            raise HTTPException(404, "That line is no longer on this purchase order. Reload it.")
        if line.received_at is not None:
            db.rollback()
            raise HTTPException(409, "This line was already received (maybe on another screen). Reload the purchase order.")
        if line.product_id is None and body.quantity > 0:
            db.rollback()
            raise HTTPException(400, "This line's part no longer exists, so nothing can be added to stock for it.")

        outstanding = max(line.quantity - line.got, 0)
        diff = body.quantity - outstanding
        later = False
        if diff != 0:
            if not body.acknowledged:
                db.rollback()
                word = "short" if diff < 0 else "over"
                raise HTTPException(
                    400, f"The count is {abs(diff)} {word}. Confirm and acknowledge it before this line can be received."
                )
            if diff < 0:
                if body.short_choice not in ("error", "later"):
                    db.rollback()
                    raise HTTPException(400, "Say whether the shortage is an error or the rest is coming on a later delivery.")
                later = body.short_choice == "later"
                if later and body.quantity == 0:
                    db.rollback()
                    raise HTTPException(400, "Nothing was counted, so there is nothing to receive now. Leave the line open instead.")

        new_qty = None
        if line.product_id is not None:
            locked = db.execute(
                text("select id from products where id = :p for update"), {"p": line.product_id}
            ).first()
            if not locked:
                db.rollback()
                raise HTTPException(404, "That product no longer exists. Nothing was received.")

        _open_batch(db, body.batch_id, "receive", user, f"PO {h.po_number}", body.note, None)

        if body.quantity > 0:
            new_qty = db.execute(
                text(
                    "update products set quantity_on_hand = coalesce(quantity_on_hand, 0) + :q, "
                    "updated_at = now() where id = :id returning quantity_on_hand"
                ),
                {"q": body.quantity, "id": line.product_id},
            ).scalar()
            db.execute(
                text(
                    "insert into inventory_transactions "
                    "(product_id, transaction_type, quantity_change, reference, legacy_created_by) "
                    "values (:p, 'Receive', :q, :ref, :by)"
                ),
                {"p": line.product_id, "q": body.quantity,
                 "ref": f"Receive {str(body.batch_id)[:8]} - PO {h.po_number}", "by": user.email},
            )
            _insert_line(db, body.batch_id, line.product_id, body.barcode_id, body.quantity)

        total_got = line.got + body.quantity
        if later:
            db.execute(
                text("update po_lines set received_qty = :t, received_by = :by where id = :id"),
                {"t": total_got, "by": user.email, "id": line.id},
            )
        else:
            db.execute(
                text("update po_lines set received_at = now(), received_qty = :t, received_by = :by where id = :id"),
                {"t": total_got, "by": user.email, "id": line.id},
            )
            if diff != 0:
                _record_issue(db, po_id, line.id, line.product_id, "short" if diff < 0 else "over",
                              line.item, line.description, line.quantity, total_got, user, body.note)
            if line.product_id is not None:
                _clear_backorder_if_done(db, line.product_id)

        # An audit row for every receipt, with the acknowledgement when there was one.
        db.execute(
            text(
                "insert into po_line_receipts (po_id, po_line_id, product_id, quantity, outcome, note, "
                "  acknowledged_by, acknowledged_at, received_by) "
                "values (:po, :l, :p, :q, :out, :note, :ack_by, case when :ack then now() end, :by)"
            ),
            {
                "po": po_id, "l": line.id, "p": line.product_id, "q": body.quantity,
                "out": "later" if later else ("short" if diff < 0 else "over" if diff > 0 else "exact"),
                "note": (body.note or "").strip() or None,
                "ack_by": user.email if diff != 0 else None, "ack": diff != 0, "by": user.email,
            },
        )

        left = db.execute(
            text("select count(*) from po_lines where po_id = :id and received_at is null"), {"id": po_id}
        ).scalar()
        if left == 0:
            db.execute(
                text("update po_headers set status = 'received', received_at = now(), updated_at = now() where id = :id"),
                {"id": po_id},
            )
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        log.exception("Receiving a PO line failed")
        raise HTTPException(500, f"Receiving failed and was rolled back; no stock changed. ({type(exc).__name__})")
    return {
        "line_id": line_id,
        "received": body.quantity,
        "difference": diff,
        "line_closed": not later,
        "still_expected": (line.quantity - total_got) if later else 0,
        "quantity_on_hand": new_qty,
        "po_status": "received" if left == 0 else "open",
        "lines_left": left,
    }


class ExtraLineIn(BaseModel):
    product_id: int
    quantity: int = Field(..., ge=1, le=MAX_QTY)
    barcode_id: Optional[int] = None


class ReceiveExtrasIn(BaseModel):
    batch_id: uuid.UUID
    lines: list[ExtraLineIn] = Field(..., min_length=1, max_length=200)
    acknowledged: bool = False
    note: Optional[str] = Field(None, max_length=300)


@router.post("/receiving/pos/{po_id}/extras")
@needs("inventory.receive")
def receive_po_extras(
    po_id: int, body: ReceiveExtrasIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)
):
    """Parts that arrived with this PO but were not on it (or more of a part than its line asked for, after that
    line was closed). They are added to stock and written to the shortage / overage report as overages, with the
    name of the person who acknowledged them and the time."""
    if not body.acknowledged:
        raise HTTPException(400, "These parts are not on the purchase order. Acknowledge that to receive them.")
    merged = _merge_lines(body.lines)
    ids = sorted(merged)
    try:
        h = db.execute(
            text("select id, po_number, status from po_headers where id = :id for update"), {"id": po_id}
        ).first()
        if not h:
            db.rollback()
            raise HTTPException(404, "That purchase order doesn't exist.")
        if h.status == "cancelled":
            db.rollback()
            raise HTTPException(409, f"Purchase order {h.po_number} was cancelled. Nothing was received.")
        prods = {
            r.id: r
            for r in db.execute(
                text("select id, sku, title from products where id = any(cast(:ids as bigint[])) order by id for update"),
                {"ids": ids},
            ).all()
        }
        if len(prods) != len(ids):
            db.rollback()
            raise HTTPException(404, "One of these products no longer exists. Nothing was received - scan again.")
        on_po = {
            r.product_id: r
            for r in db.execute(
                text("select id, product_id, item, quantity from po_lines where po_id = :id and product_id = any(cast(:ids as bigint[]))"),
                {"id": po_id, "ids": ids},
            ).all()
        }

        _open_batch(db, body.batch_id, "receive", user, f"PO {h.po_number} (extra)", body.note, None)
        out = []
        for pid in ids:
            m = merged[pid]
            new_qty = db.execute(
                text(
                    "update products set quantity_on_hand = coalesce(quantity_on_hand, 0) + :q, "
                    "updated_at = now() where id = :id returning quantity_on_hand"
                ),
                {"q": m["qty"], "id": pid},
            ).scalar()
            db.execute(
                text(
                    "insert into inventory_transactions "
                    "(product_id, transaction_type, quantity_change, reference, legacy_created_by) "
                    "values (:p, 'Receive', :q, :ref, :by)"
                ),
                {"p": pid, "q": m["qty"], "ref": f"Receive {str(body.batch_id)[:8]} - PO {h.po_number} (not on PO)", "by": user.email},
            )
            _insert_line(db, body.batch_id, pid, m["barcode_id"], m["qty"])
            ln = on_po.get(pid)
            _record_issue(
                db, po_id, ln.id if ln else None, pid, "over" if ln else "not_on_po",
                ln.item if ln else None, prods[pid].title, ln.quantity if ln else 0, m["qty"], user, body.note,
            )
            out.append({"product_id": pid, "received": m["qty"], "quantity_on_hand": new_qty})
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        log.exception("Receiving extra parts failed")
        raise HTTPException(500, f"Receiving failed and was rolled back; no stock changed. ({type(exc).__name__})")
    return {"lines": out}


# --------------------------------------------------------------------------
# stock list, manual adjustments, restock levels
# --------------------------------------------------------------------------
# "Low" = a restock level is set (above 0) and what can still be SOLD (available) is AT or BELOW it, but
# not zero (zero is "Out", so a part never shows in both lists).
# "Out" = nothing left to sell. Parts promised to paid orders are not counted as sellable.
_AVAIL_SQL = available_sql("p")
_LOW_SQL = f"(coalesce(p.reorder_point, 0) > 0 and {_AVAIL_SQL} > 0 and {_AVAIL_SQL} <= p.reorder_point)"
_OUT_SQL = f"({_AVAIL_SQL} <= 0)"
# Oversold = more promised to paid orders than the shelf count says is there. Staff-only warning.
_OVERSOLD_SQL = f"({reserved_paid_sql('p')} > coalesce(p.quantity_on_hand, 0))"


@router.get("/stock")
@needs(*ANY_INVENTORY)
def stock_list(
    q: Optional[str] = Query(None, max_length=100),
    view: str = Query("all", pattern="^(all|low|out)$"),
    include_inactive: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    params: dict = {"limit": limit, "offset": offset}
    scope = [] if include_inactive else ["coalesce(p.is_active, true)"]

    conds = list(scope)
    words = [w for w in re.split(r"\s+", (q or "").strip()) if w][:6]
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
    if view == "low":
        conds.append(_LOW_SQL)
    elif view == "out":
        conds.append(_OUT_SQL)
    where = ("where " + " and ".join(conds)) if conds else ""
    order = (
        f"(p.reorder_point - {_AVAIL_SQL}) desc, p.title, p.id"
        if view == "low"
        else "p.title, p.id"
    )

    total = db.execute(text(f"select count(*) from products p {where}"), params).scalar()
    rows = db.execute(
        text(
            "select p.id, p.sku, p.title, coalesce(p.quantity_on_hand, 0) as quantity_on_hand, "
            "       p.reorder_point, p.bin_location, coalesce(p.is_active, true) as is_active, "
            f"       {set_aside_sql('p')} as reserved, {_AVAIL_SQL} as available, "
            f"       coalesce(p.on_order, false) as on_order, p.expected_date, "
            f"       exists (select 1 from po_lines l join po_headers ph on ph.id = l.po_id "
            f"         where l.product_id = p.id and l.received_at is null and ph.status <> 'cancelled') as has_open_po, "
            f"       {_OVERSOLD_SQL} as oversold "
            f"from products p {where} order by {order} limit :limit offset :offset"
        ),
        params,
    ).all()
    scope_where = ("where " + " and ".join(scope)) if scope else ""
    counts = db.execute(
        text(
            f"select count(*) filter (where {_LOW_SQL}) as low_count, "
            f"       count(*) filter (where {_OUT_SQL}) as out_count, "
            f"       count(*) filter (where {_OVERSOLD_SQL}) as oversold_count "
            f"from products p {scope_where}"
        )
    ).first()
    return {
        "total": total,
        "low_count": counts.low_count if counts else 0,
        "out_count": counts.out_count if counts else 0,
        "oversold_count": counts.oversold_count if counts else 0,
        "items": [dict(r._mapping) for r in rows],
    }


class AdjustIn(BaseModel):
    delta: Optional[int] = Field(None, ge=-MAX_QTY, le=MAX_QTY)  # +/- this many
    set_to: Optional[int] = Field(None, ge=0, le=MAX_QTY)  # or: the real count is this
    note: Optional[str] = Field(None, max_length=200)


@router.post("/products/{product_id}/adjust")
@needs("inventory.adjust")
def adjust_stock(
    product_id: int,
    body: AdjustIn,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(current_user),
):
    """Manual stock change. Locks the product row, never goes below zero, and logs who did it."""
    if (body.delta is None) == (body.set_to is None):
        raise HTTPException(400, "Send either a change (+/-) or a new count, not both")
    try:
        row = db.execute(
            text("select id, quantity_on_hand from products where id = :id for update"),
            {"id": product_id},
        ).first()
        if not row:
            db.rollback()
            raise HTTPException(404, "That product no longer exists")
        before = row.quantity_on_hand or 0
        new = body.set_to if body.set_to is not None else before + body.delta
        if new < 0:
            db.rollback()
            raise HTTPException(400, f"That would take stock below zero (on hand is {before}).")
        if new > MAX_QTY:
            db.rollback()
            raise HTTPException(400, f"Stock can't be more than {MAX_QTY}.")
        change = new - before
        if change == 0:
            db.rollback()
            return {"product_id": product_id, "quantity_on_hand": before, "change": 0}
        db.execute(
            text("update products set quantity_on_hand = :q, updated_at = now() where id = :id"),
            {"q": new, "id": product_id},
        )
        ref = "Manual adjustment" + (f" - {body.note.strip()}" if (body.note or "").strip() else "")
        db.execute(
            text(
                "insert into inventory_transactions "
                "(product_id, transaction_type, quantity_change, reference, legacy_created_by) "
                "values (:p, 'Adjustment', :d, :ref, :by)"
            ),
            {"p": product_id, "d": change, "ref": ref[:200], "by": user.email},
        )
        db.commit()
        return {"product_id": product_id, "quantity_on_hand": new, "change": change}
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, f"Adjustment failed and was rolled back; no stock changed. ({type(exc).__name__})")


class ReorderIn(BaseModel):
    reorder_point: Optional[int] = Field(None, ge=0, le=MAX_QTY)  # empty / 0 = no restock level


@router.put("/products/{product_id}/reorder-point")
@needs("inventory.adjust")
def set_reorder_point(product_id: int, body: ReorderIn, db: Session = Depends(get_db)):
    value = body.reorder_point or None
    got = db.execute(
        text("update products set reorder_point = :v, updated_at = now() where id = :id returning reorder_point"),
        {"v": value, "id": product_id},
    ).first()
    if not got:
        db.rollback()
        raise HTTPException(404, "That product no longer exists")
    db.commit()
    return {"product_id": product_id, "reorder_point": got.reorder_point}


class OnOrderIn(BaseModel):
    on_order: bool


def _has_open_po(db: Session, product_id: int) -> bool:
    return bool(
        db.execute(
            text(
                "select 1 from po_lines l join po_headers h on h.id = l.po_id "
                "where l.product_id = :p and l.received_at is null and h.status <> 'cancelled' limit 1"
            ),
            {"p": product_id},
        ).first()
    )


@router.put("/products/{product_id}/on-order")
@needs("inventory.adjust")
def set_on_order(product_id: int, body: OnOrderIn, db: Session = Depends(get_db)):
    """Backordered on / off. A part can only be Backordered while it is on a purchase order that has not
    been received. Creating the PO ticks this by itself; here it can be un-ticked (or ticked again).
    Turning it off also clears the expected date."""
    if body.on_order and not _has_open_po(db, product_id):
        db.rollback()
        raise HTTPException(400, "This part isn't on a purchase order yet. Create a purchase order for it first.")
    got = db.execute(
        text(
            "update products set on_order = :v, "
            + ("" if body.on_order else "expected_date = null, ")
            + "updated_at = now() where id = :id returning on_order, expected_date"
        ),
        {"v": body.on_order, "id": product_id},
    ).first()
    if not got:
        db.rollback()
        raise HTTPException(404, "That product no longer exists")
    db.commit()
    return {"product_id": product_id, "on_order": got.on_order, "expected_date": got.expected_date}


class ExpectedDateIn(BaseModel):
    expected_date: Optional[date] = None  # empty clears it


@router.put("/products/{product_id}/expected-date")
@needs("inventory.adjust")
def set_expected_date(product_id: int, body: ExpectedDateIn, db: Session = Depends(get_db)):
    """When a backordered part is expected. Customers see it on the product page and on their order."""
    got = db.execute(
        text("select on_order from products where id = :id for update"), {"id": product_id}
    ).first()
    if not got:
        db.rollback()
        raise HTTPException(404, "That product no longer exists")
    if body.expected_date and not got.on_order:
        db.rollback()
        raise HTTPException(400, "Only a backordered part has an expected date. Tick Backordered first.")
    db.execute(
        text("update products set expected_date = :d, updated_at = now() where id = :id"),
        {"d": body.expected_date, "id": product_id},
    )
    db.commit()
    return {"product_id": product_id, "expected_date": body.expected_date}


# --------------------------------------------------------------------------
# website orders: pick (scan out) -> ship (tracking number) -> done.
# Also: short picks, cancelling, refunding. See the top of this file for the stock rules.
# --------------------------------------------------------------------------
def _order_by_number(db: Session, order_number: str):
    return db.execute(
        text(
            "select id, order_number, status, customer_name, shipping_address, notes, paid_at, shipped_at, "
            "       picked_at, reserved_stock, tracking_number, carrier, cancelled_at, cancel_reason, "
            "       total_cents, refunded_cents, payment_provider "
            "from orders where lower(order_number) = lower(:n)"
        ),
        {"n": order_number.strip()},
    ).first()


def _ordered_and_shipped(db: Session, order_id: int) -> tuple[dict, dict]:
    """(units ordered, units scanned out so far) per product. Scanned-out = picked."""
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


def _attention(db: Session, order_ids: list[int]) -> dict[int, list[str]]:
    """Red-flag reasons per order: a line that came up short, or not enough on the shelf to cover
    every paid order for a part. Staff only; customers never see any of this."""
    out: dict[int, list[str]] = {i: [] for i in order_ids}
    if not order_ids:
        return out
    rows = db.execute(
        text(
            """
            select oi.order_id, p.sku, 'short' as kind
            from order_items oi join products p on p.id = oi.product_id
            where oi.order_id = any(cast(:ids as bigint[])) and oi.short_qty > 0
            union
            select r.order_id, p.sku, 'oversold' as kind
            from stock_reservations r join products p on p.id = r.product_id
            where r.order_id = any(cast(:ids as bigint[])) and r.status = 'reserved'
              and coalesce(p.quantity_on_hand, 0) <
                  (select coalesce(sum(r2.quantity), 0) from stock_reservations r2
                    where r2.product_id = r.product_id and r2.status = 'reserved')
            """
        ),
        {"ids": order_ids},
    ).all()
    for r in rows:
        out[r.order_id].append(
            f"SHORT PICK: {r.sku} could not be found or was damaged"
            if r.kind == "short"
            else f"OVERSOLD: not enough {r.sku} on the shelf to cover every paid order"
        )
    return out


@router.get("/orders/to-ship")
@needs("inventory.ship")
def orders_to_ship(db: Session = Depends(get_db)):
    """Paid website orders that have not shipped. stage 'pick' = still to scan, 'ship' = every part
    is picked and it is waiting for a tracking number. Orders needing attention come first."""
    rows = db.execute(
        text(
            "select o.id, o.order_number, o.customer_name, o.paid_at, o.picked_at, "
            "       coalesce((select sum(oi.quantity) from order_items oi where oi.order_id = o.id), 0) as units "
            "from orders o where o.status = 'paid' and o.shipped_at is null "
            "order by o.paid_at nulls last, o.id limit 200"
        )
    ).all()
    flags = _attention(db, [r.id for r in rows])
    items = [
        {
            "order_number": r.order_number,
            "customer_name": r.customer_name,
            "paid_at": r.paid_at,
            "units": int(r.units),
            "stage": "ship" if r.picked_at else "pick",
            "attention": flags.get(r.id, []),
        }
        for r in rows
    ]
    items.sort(key=lambda i: 0 if i["attention"] else 1)  # stable: flagged first, then oldest first
    return {"items": items, "attention_count": sum(1 for i in items if i["attention"])}


@router.get("/orders/{order_number}")
@needs("inventory.ship")
def get_order(order_number: str, db: Session = Depends(get_db)):
    o = _order_by_number(db, order_number)
    if not o:
        raise HTTPException(404, "No order with that number")
    items = db.execute(
        text(
            "select oi.id as item_id, oi.product_id, oi.title, oi.quantity, oi.short_qty, "
            "       p.sku, p.bin_location, p.quantity_on_hand "
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
        "picked_at": o.picked_at,
        "shipped_at": o.shipped_at,
        "tracking_number": o.tracking_number,
        "carrier": o.carrier,
        "cancelled_at": o.cancelled_at,
        "cancel_reason": o.cancel_reason,
        "total_cents": o.total_cents,
        "refunded_cents": o.refunded_cents or 0,
        "pays_by_stripe": o.payment_provider == "stripe",
        "attention": _attention(db, [o.id]).get(o.id, []),
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
                "short_qty": i.short_qty or 0,
                "on_hand": i.quantity_on_hand or 0,
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
    partial: bool = False                                       # commit what's here, finish the order later
    note: Optional[str] = Field(None, max_length=500)


def _take_stock_for_pick(db: Session, order, merged: dict[int, dict], user: AuthUser) -> None:
    """The parts in this pick leave the shelf: on hand goes down and so does the order's reservation.

    Orders paid before reservations existed (reserved_stock = false) already had their stock taken
    at payment, so nothing is taken again. The shelf count never goes below zero; if the shelf
    count was lower than what was physically picked, the ledger records the real change.
    """
    if not order.reserved_stock:
        return
    lock_products(db, merged)
    for pid in sorted(merged):
        res = db.execute(
            text(
                "select id, quantity from stock_reservations "
                "where order_id = :o and product_id = :p and status = 'reserved' for update"
            ),
            {"o": order.id, "p": pid},
        ).first()
        take = min(merged[pid]["qty"], res.quantity) if res else 0
        if take <= 0:
            continue
        before = db.execute(
            text("select coalesce(quantity_on_hand, 0) from products where id = :id"), {"id": pid}
        ).scalar()
        after = db.execute(
            text(
                "update products set quantity_on_hand = greatest(0, coalesce(quantity_on_hand, 0) - :t), "
                "updated_at = now() where id = :id returning quantity_on_hand"
            ),
            {"t": take, "id": pid},
        ).scalar()
        if after != before:
            db.execute(
                text(
                    "insert into inventory_transactions "
                    "(product_id, transaction_type, quantity_change, reference, legacy_created_by) "
                    "values (:p, 'Adjustment', :d, :ref, :by)"
                ),
                {"p": pid, "d": after - before, "ref": f"Picked for order {order.order_number}", "by": user.email},
            )
        if res.quantity - take > 0:
            db.execute(
                text("update stock_reservations set quantity = quantity - :t where id = :id"),
                {"t": take, "id": res.id},
            )
        else:
            db.execute(text("delete from stock_reservations where id = :id"), {"id": res.id})


@router.post("/ship")
@needs("inventory.ship")
def ship(body: ShipIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    """Commit a scan-out batch.

    Website order: the scanned parts come off the shelf (see _take_stock_for_pick) and the order
    advances to the shipping stage once every part is picked. It is NOT shipped yet; that needs a
    tracking number (see mark_shipped). Partial commits are fine: the order stays open for the rest.
    eBay / other sale: only records who shipped what; whatever recorded the sale handles the stock.
    """
    merged = _merge_lines(body.lines)
    try:
        order = None
        fully_picked = False
        remaining_after: dict[int, int] = {}
        if body.order_number:
            order = db.execute(
                text(
                    "select id, order_number, status, shipped_at, picked_at, reserved_stock from orders "
                    "where lower(order_number) = lower(:n) for update"
                ),
                {"n": body.order_number.strip()},
            ).first()
            if not order:
                db.rollback()
                raise HTTPException(404, "No order with that number")
            if order.status != "paid":
                db.rollback()
                raise HTTPException(409, f"Order {order.order_number} is '{order.status}', not paid - it can't be picked.")
            if order.shipped_at:
                db.rollback()
                raise HTTPException(409, f"Order {order.order_number} was already marked shipped.")
            if order.picked_at:
                db.rollback()
                raise HTTPException(
                    409, f"Order {order.order_number} is already fully picked. Enter its tracking number to ship it."
                )
            ordered, shipped = _ordered_and_shipped(db, order.id)
            for pid, m in merged.items():
                if pid not in ordered:
                    db.rollback()
                    raise HTTPException(400, "A scanned part is not on this order.")
                left = ordered[pid] - shipped.get(pid, 0)
                if m["qty"] > left:
                    db.rollback()
                    raise HTTPException(400, f"Too many scanned for a part on this order (only {left} still to pick).")
            remaining_after = {
                pid: ordered[pid] - shipped.get(pid, 0) - merged.get(pid, {"qty": 0})["qty"] for pid in ordered
            }
            fully_picked = all(v == 0 for v in remaining_after.values())
            if not fully_picked and not body.partial:
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
        if order:
            _take_stock_for_pick(db, order, merged, user)
            # A line that was flagged short is only still short for what has not been picked.
            for pid in merged:
                db.execute(
                    text(
                        "update order_items set short_qty = least(short_qty, :r) "
                        "where order_id = :o and product_id = :p and short_qty > 0"
                    ),
                    {"r": remaining_after.get(pid, 0), "o": order.id, "p": pid},
                )
            if fully_picked:
                db.execute(
                    text("update orders set picked_at = now(), picked_by = :by where id = :id"),
                    {"by": user.email, "id": order.id},
                )
        db.commit()
        return {
            "batch_id": str(body.batch_id),
            "fully_picked": fully_picked,
            "remaining": {str(k): v for k, v in remaining_after.items() if v > 0},
        }
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, f"Pick failed and was rolled back; nothing changed. ({type(exc).__name__})")


class ShortIn(BaseModel):
    product_id: int
    quantity: int = Field(..., ge=1, le=MAX_QTY)               # units that could not be found / were damaged
    actual_on_hand: Optional[int] = Field(None, ge=0, le=MAX_QTY)  # the real shelf count, if they counted
    note: Optional[str] = Field(None, max_length=200)


@router.post("/orders/{order_number}/short")
@needs("inventory.ship")
def short_pick(
    order_number: str,
    body: ShortIn,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(current_user),
):
    """The part is not on the shelf (or is damaged). Flags the order red so it gets looked at, and
    optionally fixes the shelf count. Nothing is cancelled; that is a separate decision."""
    try:
        order = db.execute(
            text(
                "select id, order_number, status, shipped_at, picked_at from orders "
                "where lower(order_number) = lower(:n) for update"
            ),
            {"n": order_number.strip()},
        ).first()
        if not order:
            db.rollback()
            raise HTTPException(404, "No order with that number")
        if order.status != "paid" or order.shipped_at or order.picked_at:
            db.rollback()
            raise HTTPException(409, "Only an order that is paid and still being picked can be flagged short.")
        ordered, shipped = _ordered_and_shipped(db, order.id)
        if body.product_id not in ordered:
            db.rollback()
            raise HTTPException(400, "That part is not on this order.")
        left = ordered[body.product_id] - shipped.get(body.product_id, 0)
        if body.quantity > left:
            db.rollback()
            raise HTTPException(400, f"Only {left} of that part are still to pick on this order.")
        db.execute(
            text("update order_items set short_qty = :q where order_id = :o and product_id = :p"),
            {"q": body.quantity, "o": order.id, "p": body.product_id},
        )
        if body.actual_on_hand is not None:
            lock_products(db, [body.product_id])
            before = db.execute(
                text("select coalesce(quantity_on_hand, 0) from products where id = :id"), {"id": body.product_id}
            ).scalar()
            if before != body.actual_on_hand:
                db.execute(
                    text("update products set quantity_on_hand = :q, updated_at = now() where id = :id"),
                    {"q": body.actual_on_hand, "id": body.product_id},
                )
                ref = f"Short pick, order {order.order_number}: shelf counted at {body.actual_on_hand}"
                if (body.note or "").strip():
                    ref += f" - {body.note.strip()}"
                db.execute(
                    text(
                        "insert into inventory_transactions "
                        "(product_id, transaction_type, quantity_change, reference, legacy_created_by) "
                        "values (:p, 'Adjustment', :d, :ref, :by)"
                    ),
                    {"p": body.product_id, "d": body.actual_on_hand - before, "ref": ref[:200], "by": user.email},
                )
        db.commit()
        return {"ok": True, "attention": _attention(db, [order.id]).get(order.id, [])}
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, f"Could not flag the short pick; nothing changed. ({type(exc).__name__})")


class ShippedIn(BaseModel):
    tracking_number: str = Field(..., min_length=3, max_length=100)
    carrier: Optional[str] = Field(None, max_length=40)


@router.post("/orders/{order_number}/shipped")
@needs("inventory.ship")
def mark_shipped(
    order_number: str,
    body: ShippedIn,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(current_user),
):
    """The order is in the carrier's hands. Needs every part picked first, and the tracking number."""
    order = db.execute(
        text(
            "select id, order_number, status, shipped_at, picked_at from orders "
            "where lower(order_number) = lower(:n) for update"
        ),
        {"n": order_number.strip()},
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
    if not order.picked_at:
        db.rollback()
        raise HTTPException(409, "Scan every part of the order first. It can only ship once it is fully picked.")
    db.execute(
        text(
            "update orders set status = 'shipped', shipped_at = now(), shipped_by = :by, "
            "tracking_number = :t, carrier = :c where id = :id"
        ),
        {
            "by": user.email,
            "t": body.tracking_number.strip(),
            "c": (body.carrier or "").strip() or None,
            "id": order.id,
        },
    )
    db.commit()
    return {"ok": True, "order_number": order.order_number}


class CancelIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=200)


def _add_back(db: Session, pid: int, qty: int, ref: str, by: Optional[str]) -> None:
    db.execute(
        text(
            "update products set quantity_on_hand = coalesce(quantity_on_hand, 0) + :q, updated_at = now() "
            "where id = :id"
        ),
        {"q": qty, "id": pid},
    )
    db.execute(
        text(
            "insert into inventory_transactions "
            "(product_id, transaction_type, quantity_change, reference, legacy_created_by) "
            "values (:p, 'Adjustment', :q, :ref, :by)"
        ),
        {"p": pid, "q": qty, "ref": ref[:200], "by": by},
    )


@router.post("/orders/{order_number}/cancel")
@needs("inventory.ship")
def cancel_order(
    order_number: str,
    body: CancelIn,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(current_user),
):
    """Cancel a paid order that has not shipped. The money is NOT refunded here (see refund_order).

      * Parts still waiting to be picked: the reservation is released, so they can be sold again.
        The shelf count was never reduced, so it does not change.
      * Parts already picked: they go back on the shelf, so on hand goes back up.
      * Orders paid before reservations existed: their stock was taken at payment, so everything
        not already scanned out is put back.
    """
    try:
        order = db.execute(
            text(
                "select id, order_number, status, shipped_at, reserved_stock, total_cents, refunded_cents "
                "from orders where lower(order_number) = lower(:n) for update"
            ),
            {"n": order_number.strip()},
        ).first()
        if not order:
            db.rollback()
            raise HTTPException(404, "No order with that number")
        if order.shipped_at or order.status == "shipped":
            db.rollback()
            raise HTTPException(409, "This order has already shipped, so it can't be cancelled. It would be a return.")
        if order.status != "paid":
            db.rollback()
            raise HTTPException(409, f"Only paid orders can be cancelled here. This one is '{order.status}'.")

        ordered, picked = _ordered_and_shipped(db, order.id)
        lock_products(db, ordered)
        if order.reserved_stock:
            back = {pid: q for pid, q in picked.items() if pid in ordered}
        else:
            back = {pid: ordered[pid] - picked.get(pid, 0) for pid in ordered}
        restored = {}
        for pid, q in sorted(back.items()):
            if q > 0:
                _add_back(db, pid, q, f"Order {order.order_number} cancelled: parts back in stock", user.email)
                restored[str(pid)] = q
        db.execute(text("delete from stock_reservations where order_id = :o"), {"o": order.id})
        db.execute(
            text(
                "update orders set status = 'cancelled', cancelled_at = now(), cancelled_by = :by, "
                "cancel_reason = :why where id = :id"
            ),
            {"by": user.email, "why": (body.reason or "").strip() or None, "id": order.id},
        )
        db.commit()
        return {
            "ok": True,
            "put_back": sum(restored.values()),
            "refundable_cents": max(0, (order.total_cents or 0) - (order.refunded_cents or 0)),
        }
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, f"Cancelling failed and was rolled back; nothing changed. ({type(exc).__name__})")


class RefundIn(BaseModel):
    amount_cents: Optional[int] = Field(None, ge=1, le=10_000_000)  # leave empty to refund everything left


@router.post("/orders/{order_number}/refund")
def refund_order(
    order_number: str,
    body: RefundIn,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(current_user),
):
    """Send money back to the customer's card through Stripe. Owner / admin only (no @needs marker).

    Separate from cancelling on purpose: you can refund after a cancellation, or on its own (a
    return, a short pick on one line). The amount is capped at what is still unrefunded, and the
    Stripe idempotency key makes a double click or a retry refund only once.
    """
    from app.store_checkout import _open_stripe  # same Stripe key handling as checkout

    _open_stripe()
    order = db.execute(
        text(
            "select id, order_number, payment_provider, payment_reference, paid_at, total_cents, refunded_cents "
            "from orders where lower(order_number) = lower(:n) for update"
        ),
        {"n": order_number.strip()},
    ).first()
    if not order:
        db.rollback()
        raise HTTPException(404, "No order with that number")
    if not order.paid_at or order.payment_provider != "stripe" or not order.payment_reference:
        db.rollback()
        raise HTTPException(409, "This order wasn't paid through Stripe on the website, so it can't be refunded here.")
    already = order.refunded_cents or 0
    left = (order.total_cents or 0) - already
    if left <= 0:
        db.rollback()
        raise HTTPException(409, "This order has already been fully refunded.")
    amount = body.amount_cents or left
    if amount > left:
        db.rollback()
        raise HTTPException(400, f"That is more than is left to refund (${left / 100:,.2f}).")
    try:
        session = stripe.checkout.Session.retrieve(order.payment_reference)
        intent = session.get("payment_intent")
        if not intent:
            raise ValueError("Stripe has no payment on file for this order")
        stripe.Refund.create(
            payment_intent=intent,
            amount=amount,
            metadata={"order_number": order.order_number, "refunded_by": user.email or ""},
            idempotency_key=f"refund-{order.id}-{already}-{amount}",
        )
    except Exception as exc:
        db.rollback()
        log.exception("Stripe refund failed for order %s", order.order_number)
        detail = getattr(exc, "user_message", None) or str(exc) or type(exc).__name__
        raise HTTPException(502, f"Stripe did not issue the refund, so nothing was recorded. ({detail})")
    db.execute(
        text("update orders set refunded_cents = coalesce(refunded_cents, 0) + :a, refunded_at = now() where id = :id"),
        {"a": amount, "id": order.id},
    )
    db.commit()
    return {"ok": True, "refunded_cents": already + amount, "left_cents": left - amount}


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
