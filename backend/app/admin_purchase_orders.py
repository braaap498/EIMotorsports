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
  GET    /purchase-orders/defaults         the default Ship To address and the next unused PO number
  GET    /purchase-orders/{id}             one PO with its lines
  PUT    /purchase-orders/{id}             edit a PO (vendor, ship-to, note, open lines, and its PO number)
  PUT    /purchase-orders/{id}/sent        mark it sent to the distributor (or not)
  POST   /purchase-orders/{id}/cancel      cancel a PO (it is kept, with its number; parts that are on no other open PO stop being Backordered)
  GET    /purchase-orders/issues           POs with receiving problems (short, over, or not on the PO), searchable
  GET    /purchase-orders/{id}/export?format=pdf|xlsx     download it again, any time

A PO has a type: "stock" (buying inventory, received into stock) or "drop_ship" (ships straight to a customer). The type
is chosen when it is made. Rows made before the type existed count as stock.

The "Cost" on a PO line is what you pay the distributor. It starts from the product's cost (admin only, never shown
to customers) and can be changed per PO line.

A PO is never deleted. It is "open" until every line has been received (then "received"), or "cancelled".
PO numbers count up from the highest one used (cancelled POs keep theirs), so the first PO ever made is 1.
Receiving against a PO happens on the Inventory > Receive tab (see admin_inventory.py); anything short, over or
not on the PO is written to po_receiving_issues, which the "Shortages / overages" tab here reads.

Creating a PO also marks each part on it Backordered (on_order = true), because a part can only be
Backordered once it is on a PO. That is ticked by default and can be un-ticked on the Stock tab.
The PO's "Item" column is the product's manufacturer part number (the field on the product page, or the
"Manufacturer Part Number" item specific when that field is empty), copied onto the PO when it is made.
"""
import logging
import time
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user, needs, require_route_access
from app.database import get_db
from app.kits import expand_rows
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


# Who made a PO: their full name, not their email. Names live on the staff accounts in Supabase, so they are
# looked up (and kept for a few minutes). If that lookup isn't possible, the email is shown instead.
_NAMES: dict = {"at": 0.0, "map": {}}


def _staff_names() -> dict[str, str]:
    now = time.time()
    if now - _NAMES["at"] < 300:
        return _NAMES["map"]
    found: dict[str, str] = {}
    try:
        from app.admin_users import _call

        for page in range(1, 21):
            users = (_call("GET", f"/users?page={page}&per_page=200").get("users")) or []
            for u in users:
                um = u.get("user_metadata") or {}
                first = (um.get("display_name") or um.get("full_name") or "").strip()
                last = (um.get("last_name") or "").strip()
                # "Eric" + "Smith" -> "Eric Smith" (but don't repeat it if the display name already has it)
                name = first if (not last or first.lower().endswith(last.lower())) else f"{first} {last}".strip()
                if u.get("email") and name:
                    found[u["email"].strip().lower()] = name
            if len(users) < 200:
                break
        _NAMES.update(at=now, map=found)
    except Exception:
        log.warning("Could not look up staff names for purchase orders; showing emails instead")
        _NAMES["at"] = now - 240  # try again in about a minute
    return _NAMES["map"]


def _who(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    return _staff_names().get(email.strip().lower()) or email


# Open = not received yet AND on a PO that is not cancelled. Use this wherever "is this part still on order?" matters.
OPEN_LINE = "l.received_at is null and exists (select 1 from po_headers hh where hh.id = l.po_id and hh.status <> 'cancelled')"


def _free_number(db: Session) -> int:
    """The next PO number: one more than the highest used (1 when there are none). POs are never deleted,
    so a number is never reused or skipped. The lock keeps two people creating a PO at once from getting the same number."""
    db.execute(text("select pg_advisory_xact_lock(7001)"))
    return int(db.execute(text("select coalesce(max(po_number), 0) + 1 from po_headers")).scalar())


def _po_cols(db: Session) -> set[str]:
    """Which optional columns po_headers has yet (po_type, supplier_order_number: sql/po_supplier_fields.sql), so the screen
    keeps working before that script has been run."""
    rows = db.execute(
        text("select column_name from information_schema.columns where table_schema = current_schema() and table_name = 'po_headers'")
    ).all()
    return {r[0] for r in rows}


def _release_parts(db: Session, product_ids) -> None:
    """Parts that are no longer on any open purchase order stop being Backordered (and lose their date)."""
    ids = sorted({int(i) for i in product_ids})
    if not ids:
        return
    db.execute(
        text(
            "update products p set on_order = false, expected_date = null, updated_at = now() "
            "where p.id = any(cast(:ids as bigint[])) "
            f"and not exists (select 1 from po_lines l where l.product_id = p.id and {OPEN_LINE})"
        ),
        {"ids": ids},
    )


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
            f"(p.title ilike cast(:w{i} as text) or p.sku ilike cast(:w{i} as text) or p.ebay_item_id ilike cast(:w{i} as text) "
            f"or p.manufacturer_part_number ilike cast(:w{i} as text) "
            f"or {_MPN} ilike cast(:w{i} as text))"
        )
    where = ("where " + " and ".join(conds)) if conds else ""
    total = db.execute(text(f"select count(*) from products p {where}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            select p.id, p.sku_display as sku, p.title, {_MPN} as part_number, p.cost,
                   coalesce(p.quantity_on_hand, 0) as on_hand, {set_aside_sql('p')} as reserved,
                   {avail} as available, p.reorder_point, coalesce(p.on_order, false) as on_order,
                   (select array_agg(h.po_number order by h.po_number)
                      from po_lines l join po_headers h on h.id = l.po_id
                     where l.product_id = p.id and l.received_at is null and h.status <> 'cancelled') as open_pos
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
    ship_to: Optional[str] = Field(None, max_length=400)   # empty text = no ship-to; missing = the default address
    note: Optional[str] = Field(None, max_length=300)
    expected_date: Optional[date] = None   # when the parts are expected; sets the backorder date on each part
    po_type: str = Field("stock", pattern="^(stock|drop_ship)$")   # stock = buying inventory; drop_ship = ships straight to a customer
    source: Optional[str] = Field(None, max_length=60)             # e.g. "web:1042" when made from a website order
    lines: list[PoLineIn] = Field(..., min_length=1, max_length=200)


def _po_detail(db: Session, po_id: int) -> dict:
    cols = _po_cols(db)
    extra = (", coalesce(po_type, 'stock') as po_type" if "po_type" in cols else ", 'stock' as po_type") + (
        ", supplier_order_number" if "supplier_order_number" in cols else ", null as supplier_order_number"
    ) + (", source" if "source" in cols else ", null as source")
    h = db.execute(
        text(
            "select id, po_number, distributor_id, vendor_name, vendor_address, ship_to, note, total_cents, "
            "       created_by, created_at, sent_at, updated_at, status, received_at, cancelled_at, cancelled_by" + extra +
            " from po_headers where id = :id"
        ),
        {"id": po_id},
    ).first()
    if not h:
        raise HTTPException(404, "That purchase order doesn't exist")
    lines = db.execute(
        text(
            "select l.id, l.product_id, p.sku_display as sku, l.item, l.description, l.quantity, l.rate, l.received_at, l.received_qty, l.kit_product_id, l.per_kit "
            "from po_lines l left join products p on p.id = l.product_id where l.po_id = :id order by l.id"
        ),
        {"id": po_id},
    ).all()
    out_lines = [
        {**dict(l._mapping), "rate": float(l.rate), "amount": float(Decimal(str(l.rate)) * l.quantity)} for l in lines
    ]
    return {
        **dict(h._mapping),
        "created_by_name": _who(h.created_by),
        "cancelled_by_name": _who(h.cancelled_by),
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
        rows, kit_notes = expand_rows(db, rows, dist.id)  # a kit becomes one line per piece
        total = po_total(rows)
        cols = _po_cols(db)
        has_type = "po_type" in cols
        has_source = "source" in cols and bool(body.source)
        header = db.execute(
            text(
                "insert into po_headers (po_number, distributor_id, vendor_name, vendor_address, ship_to, note, total_cents, created_by"
                + (", po_type" if has_type else "")
                + (", source" if has_source else "")
                + ") values (:pn, :d, :vn, :va, :st, :note, :tc, :by"
                + (", :pt" if has_type else "")
                + (", :src" if has_source else "")
                + ") returning id, po_number"
            ),
            {
                **({"pt": body.po_type} if has_type else {}),
                **({"src": body.source} if has_source else {}),
                "pn": _free_number(db),
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
                    "insert into po_lines (po_id, product_id, item, description, quantity, rate, kit_product_id, kit_qty, per_kit) "
                    "values (:po, :p, :item, :desc, :q, :rate, :kp, :kq, :pk)"
                ),
                {"po": header.id, "p": r["product_id"], "item": r["item"], "desc": r["description"],
                 "q": r["quantity"], "rate": r["rate"],
                 "kp": r.get("kit_product_id"), "kq": r.get("kit_qty"), "pk": r.get("per_kit")},
            )
        # Backordered is ticked by default for every part on a new STOCK PO (it can be un-ticked on the Stock tab).
        # A drop ship PO never marks parts Backordered: nothing is coming to our shelf, so the website must not say so.
        if body.po_type != "drop_ship":
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
    po["kit_notes"] = kit_notes
    return po


class FromWebOrderIn(BaseModel):
    order_number: str = Field(..., min_length=1, max_length=40)
    all_lines: bool = False   # false = only the lines the shelf cannot cover; true = every line on the order
    product_ids: Optional[list[int]] = Field(None, max_length=200)   # the lines staff chose (overrides the two above)


def _po_ship_to(addr: dict) -> str:
    """A website order's address as the PO's ship-to text (the Automatic Distributors screen reads it back into fields)."""
    parts = [
        (addr.get("full_name") or "").strip(),
        (addr.get("line1") or "").strip(),
        (addr.get("line2") or "").strip(),
        f"{(addr.get('city') or '').strip()}, {(addr.get('region') or '').strip()} {(addr.get('postal_code') or '').strip()}".strip(", "),
    ]
    if (addr.get("phone") or "").strip():
        parts.append("Phone: " + addr["phone"].strip())
    return "\n".join(p for p in parts if p)


@router.post("/from-web-order", status_code=201)
@needs("inventory.purchase", "inventory.ship")
def po_from_web_order(body: FromWebOrderIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    """Make a DROP SHIP purchase order to Automatic Distributors for the parts a website order is short of.
    It ships straight to the customer, so the PO's ship-to is the customer's address."""
    cols = _po_cols(db)
    if not {"po_type", "source"} <= cols:
        raise HTTPException(503, "Run sql/po_supplier_fields.sql in Supabase first (it adds the PO type and source).")
    o = db.execute(
        text("select id, order_number, status, shipping_address from orders where order_number = :n"), {"n": body.order_number.strip()}
    ).first()
    if not o:
        raise HTTPException(404, "No website order with that number.")
    if o.status in ("cancelled", "shipped"):
        raise HTTPException(409, f"Order {o.order_number} is {o.status}.")
    src = f"web:{o.order_number}"
    dup = db.execute(
        text("select po_number from po_headers where source = :s and status <> 'cancelled' order by id desc limit 1"), {"s": src}
    ).scalar()
    if dup:
        raise HTTPException(409, f"PO {dup} was already made for order {o.order_number}. Cancel it first if you need a new one.")
    dists = db.execute(
        text("select id from po_distributors where name ilike '%automatic dist%' or name ilike '%autodist%' order by id")
    ).all()
    if len(dists) != 1:
        raise HTTPException(
            409,
            "Add one distributor named Automatic Distributors on the Distributors page first." if not dists
            else "More than one distributor looks like Automatic Distributors. Keep one, or rename the others.",
        )
    items = db.execute(
        text("select product_id, title, quantity from order_items where order_id = :o and product_id is not null order by id"), {"o": o.id}
    ).all()
    from app.stock import order_shortfalls

    short = {r.product_id for r in order_shortfalls(db, [o.id]) if r.short and r.short > 0}
    if body.product_ids:
        chosen = set(body.product_ids)
        picked = [i for i in items if i.product_id in chosen]
        if not picked:
            raise HTTPException(409, "None of the chosen parts are on that order.")
    else:
        picked = [i for i in items if body.all_lines or i.product_id in short]
    if not picked:
        raise HTTPException(409, "Nothing on this order is short of stock. (Only paid orders waiting to be picked are checked.) Choose the parts to drop ship.")
    addr = o.shipping_address or {}
    if not all((addr.get(k) or "").strip() for k in ("full_name", "line1", "city", "region", "postal_code")):
        raise HTTPException(409, "That order's shipping address is incomplete.")
    po = create_po(
        PoIn(
            distributor_id=dists[0].id,
            ship_to=_po_ship_to(addr),
            note=f"Drop ship for website order {o.order_number}",
            po_type="drop_ship",
            source=src,
            lines=[PoLineIn(product_id=i.product_id, quantity=i.quantity) for i in picked],
        ),
        db=db,
        user=user,
    )
    return po


# --------------------------------------------------------------------------
# DROP SHIP FROM AN EBAY ORDER
# The eBay import never stores buyer names or street addresses (only state and ZIP). So the full ship-to is fetched
# from eBay on demand, for ONE order, when staff press the button, and is written onto the PO only.
# --------------------------------------------------------------------------
class EbayDropIn(BaseModel):
    order: str = Field(..., min_length=3, max_length=40)           # the eBay order number
    product_ids: Optional[list[int]] = Field(None, max_length=200)  # the parts staff chose; empty = every matched line


def _ebay_text(el, path: str) -> str:
    from app.ebay_edit import NS

    return (el.findtext("/".join(NS + p for p in path.split("/"))) or "").strip()


def _ebay_buyer_address(db: Session, store: str, order_id: str) -> dict:
    """Ask eBay for ONE order's ship-to. Returns the fields plus 'gsp' (Global Shipping Program) and 'country'."""
    from app.ebay_edit import NS, _failed, _first_message, _trading

    root = _trading(
        db, store, "GetOrders",
        f"<OrderIDArray><OrderID>{order_id}</OrderID></OrderIDArray><OrderRole>Seller</OrderRole>",
    )
    if _failed(root):
        raise HTTPException(502, _first_message(root))
    found = None
    for o in root.findall(f"{NS}OrderArray/{NS}Order"):
        if _ebay_text(o, "OrderID") == order_id:
            found = o
            break
    if found is None:
        raise HTTPException(404, "eBay did not return that order.")
    service = _ebay_text(found, "ShippingServiceSelected/ShippingService")
    # Global Shipping Program: eBay marks the order multi-leg (seller ships to an eBay hub, which forwards it abroad).
    gsp = (
        _ebay_text(found, "IsMultilegShipping").lower() == "true"
        or found.find(f"{NS}MultiLegShippingDetails") is not None
        or "global" in service.lower()
    )
    phone = _ebay_text(found, "ShippingAddress/Phone")
    if phone.lower().startswith("invalid") or not any(c.isdigit() for c in phone):
        phone = ""
    return {
        "gsp": gsp,
        "service": service,
        "full_name": _ebay_text(found, "ShippingAddress/Name"),
        "line1": _ebay_text(found, "ShippingAddress/Street1"),
        "line2": _ebay_text(found, "ShippingAddress/Street2"),
        "city": _ebay_text(found, "ShippingAddress/CityName"),
        "region": _ebay_text(found, "ShippingAddress/StateOrProvince"),
        "postal_code": _ebay_text(found, "ShippingAddress/PostalCode"),
        "country": _ebay_text(found, "ShippingAddress/Country"),
        "phone": phone,
    }


def _ebay_order_and_lines(db: Session, order: str):
    o = db.execute(
        text(
            "select id, store, ebay_order_id, ebay_status, cancelled, shipped_at_ebay from ebay_orders "
            "where lower(ebay_order_id) = lower(:o) order by id desc limit 1"
        ),
        {"o": order.strip()},
    ).first()
    if not o:
        raise HTTPException(404, "That eBay order isn't in the system yet. Run a check on the eBay Sales page, then try again.")
    lines = db.execute(
        text(
            f"""
            select l.id, l.product_id, coalesce(l.title, p.title) as title, l.quantity, l.state,
                   p.sku_display as sku, coalesce(p.quantity_on_hand, 0) as on_hand, {set_aside_sql('p')} as reserved
            from ebay_order_lines l left join products p on p.id = l.product_id
            where l.order_pk = :o order by l.id
            """
        ),
        {"o": o.id},
    ).all()
    return o, lines


@router.get("/ebay-order")
@needs("inventory.purchase", "inventory.ship")
def ebay_order_lookup(order: str = Query(..., min_length=3, max_length=40), db: Session = Depends(get_db)):
    """What staff see before making a drop ship PO from an eBay order: its lines, shelf counts and any warnings.
    The buyer's address is NOT fetched here (nothing is stored or sent until the PO is made)."""
    o, lines = _ebay_order_and_lines(db, order)
    existing = db.execute(
        text("select po_number from po_headers where source = :s and status <> 'cancelled' order by id desc limit 1"),
        {"s": f"ebay:{o.ebay_order_id}"},
    ).scalar() if "source" in _po_cols(db) else None
    warnings = []
    if o.cancelled:
        warnings.append("This eBay order is cancelled.")
    elif o.shipped_at_ebay:
        warnings.append("eBay shows this order as already shipped.")
    return {
        "order": o.ebay_order_id,
        "store": o.store,
        "status": o.ebay_status,
        "blocked": bool(o.cancelled or o.shipped_at_ebay),
        "existing_po": existing,
        "warnings": warnings,
        "lines": [
            {
                "product_id": l.product_id, "sku": l.sku, "title": l.title, "quantity": l.quantity,
                "on_hand": l.on_hand, "available": max(0, int(l.on_hand or 0) - int(l.reserved or 0)),
                "short": l.product_id is not None and int(l.on_hand or 0) < int(l.quantity or 0),
                "matched": l.product_id is not None,
            }
            for l in lines
        ],
    }


@router.post("/from-ebay-order", status_code=201)
@needs("inventory.purchase", "inventory.ship")
def po_from_ebay_order(body: EbayDropIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    """Make a DROP SHIP purchase order to Automatic Distributors from an eBay order, shipping to the eBay buyer.
    The buyer's address is fetched from eBay now and written onto the PO only. Global Shipping Program orders are refused."""
    cols = _po_cols(db)
    if not {"po_type", "source"} <= cols:
        raise HTTPException(503, "Run sql/po_supplier_fields.sql in Supabase first (it adds the PO type and source).")
    o, lines = _ebay_order_and_lines(db, body.order)
    if o.cancelled:
        raise HTTPException(409, f"eBay order {o.ebay_order_id} is cancelled.")
    if o.shipped_at_ebay:
        raise HTTPException(409, f"eBay shows order {o.ebay_order_id} as already shipped.")
    src = f"ebay:{o.ebay_order_id}"
    dup = db.execute(
        text("select po_number from po_headers where source = :s and status <> 'cancelled' order by id desc limit 1"), {"s": src}
    ).scalar()
    if dup:
        raise HTTPException(409, f"PO {dup} was already made for eBay order {o.ebay_order_id}. Cancel it first if you need a new one.")
    dists = db.execute(
        text("select id from po_distributors where name ilike '%automatic dist%' or name ilike '%autodist%' order by id")
    ).all()
    if len(dists) != 1:
        raise HTTPException(
            409,
            "Add one distributor named Automatic Distributors on the Distributors page first." if not dists
            else "More than one distributor looks like Automatic Distributors. Keep one, or rename the others.",
        )
    chosen = set(body.product_ids or [])
    picked = [l for l in lines if l.product_id is not None and (not chosen or l.product_id in chosen)]
    if not picked:
        raise HTTPException(409, "None of the chosen lines are matched to a product. Link the listing to a product first.")
    addr = _ebay_buyer_address(db, o.store, o.ebay_order_id)
    if addr["gsp"]:
        raise HTTPException(
            409,
            "This is a Global Shipping Program order. eBay sends it through its own hub, so the address eBay gives is the "
            "hub's, not the buyer's. Do not drop ship it: ship it from stock, or handle it by hand.",
        )
    if not all((addr.get(k) or "").strip() for k in ("full_name", "line1", "city", "region", "postal_code")):
        raise HTTPException(409, "eBay did not return a complete ship-to address for that order.")
    merged: dict[int, int] = {}
    for l in picked:
        merged[l.product_id] = merged.get(l.product_id, 0) + int(l.quantity or 1)
    return create_po(
        PoIn(
            distributor_id=dists[0].id,
            ship_to=_po_ship_to(addr),
            note=f"Drop ship for eBay order {o.ebay_order_id}",
            po_type="drop_ship",
            source=src,
            lines=[PoLineIn(product_id=pid, quantity=q) for pid, q in merged.items()],
        ),
        db=db,
        user=user,
    )


@router.get("")
@needs("inventory.purchase")
def list_pos(
    q: Optional[str] = Query(None, max_length=100),
    status: Optional[str] = Query(None, pattern="^(open|received|cancelled)$"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    import re

    params: dict = {"limit": limit, "offset": offset}
    conds = []
    if status:
        params["status"] = status
        conds.append("h.status = :status")
    for i, w in enumerate([w for w in re.split(r"\s+", (q or "").strip()) if w][:6]):
        params[f"w{i}"] = f"%{w}%"
        conds.append(
            f"(cast(h.po_number as text) ilike cast(:w{i} as text) or h.vendor_name ilike cast(:w{i} as text) "
            f"or exists (select 1 from po_lines l left join products p on p.id = l.product_id "
            f"  where l.po_id = h.id and (l.item ilike cast(:w{i} as text) or l.description ilike cast(:w{i} as text) "
            f"  or p.sku ilike cast(:w{i} as text) or p.ebay_item_id ilike cast(:w{i} as text))))"
        )
    where = ("where " + " and ".join(conds)) if conds else ""
    total = db.execute(text(f"select count(*) from po_headers h {where}"), params).scalar()
    type_sel = "coalesce(h.po_type, 'stock')" if "po_type" in _po_cols(db) else "'stock'"
    rows = db.execute(
        text(
            f"""
            select h.id, h.po_number, h.vendor_name, h.total_cents, h.created_by, h.created_at, h.sent_at, h.status, {type_sel} as po_type,
                   (select count(*) from po_receiving_issues i where i.po_id = h.id) as issue_count,
                   (select count(*) from po_lines l where l.po_id = h.id) as line_count,
                   (select count(*) from po_lines l where l.po_id = h.id and l.received_at is null) as open_lines
            from po_headers h {where} order by h.po_number desc limit :limit offset :offset
            """
        ),
        params,
    ).all()
    items = []
    for r in rows:
        d = dict(r._mapping)
        d["created_by_name"] = _who(d["created_by"])
        items.append(d)
    return {"total": total, "items": items}


@router.get("/defaults")
@needs("inventory.purchase")
def defaults(db: Session = Depends(get_db)):
    """What the Create / Edit screens fill in: the usual Ship To address, and a PO number nobody uses yet."""
    nxt = db.execute(text("select coalesce(max(po_number), 0) + 1 from po_headers")).scalar()
    return {"ship_to": DEFAULT_SHIP_TO, "next_po_number": int(nxt)}


@router.get("/issues")
@needs("inventory.purchase")
def po_issues(
    q: Optional[str] = Query(None, max_length=100),
    kind: Optional[str] = Query(None, pattern="^(short|over|not_on_po)$"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """The shortage / overage report: every PO that had a problem when it was received, with each problem.
    short = fewer arrived than ordered and the rest isn't coming; over = more than ordered, or a part that wasn't on
    the PO at all (not_on_po marks that second kind, so it can be filtered on its own).
    A short line whose rest is expected on a later delivery is NOT here: that PO simply stays open.
    Each one records who acknowledged it and when. Search by PO number, distributor, part number, SKU or description."""
    import re

    params: dict = {"limit": limit, "offset": offset}
    conds = []
    if kind:
        params["kind"] = kind
        # "over" means every overage, including parts that were not on the PO at all; "not_on_po" is just those.
        conds.append(
            "exists (select 1 from po_receiving_issues k where k.po_id = h.id and "
            + ("k.kind in ('over', 'not_on_po')" if kind == "over" else "k.kind = :kind")
            + ")"
        )
    for i, w in enumerate([w for w in re.split(r"\s+", (q or "").strip()) if w][:6]):
        params[f"w{i}"] = f"%{w}%"
        conds.append(
            f"(cast(h.po_number as text) ilike cast(:w{i} as text) or h.vendor_name ilike cast(:w{i} as text) "
            f"or exists (select 1 from po_receiving_issues k where k.po_id = h.id and "
            f"(k.sku ilike cast(:w{i} as text) or k.item ilike cast(:w{i} as text) or k.description ilike cast(:w{i} as text))))"
        )
    where = " and ".join(["exists (select 1 from po_receiving_issues x where x.po_id = h.id)"] + conds)
    total = db.execute(text(f"select count(*) from po_headers h where {where}"), params).scalar()
    heads = db.execute(
        text(
            f"select h.id, h.po_number, h.vendor_name, h.status, h.created_at, h.sent_at, h.received_at "
            f"from po_headers h where {where} order by h.po_number desc limit :limit offset :offset"
        ),
        params,
    ).all()
    ids = [h.id for h in heads]
    by_po: dict[int, list] = {i: [] for i in ids}
    if ids:
        rows = db.execute(
            text(
                "select id, po_id, kind, sku, item, description, expected_qty, received_qty, note, "
                "       acknowledged_by, acknowledged_at "
                "from po_receiving_issues where po_id = any(cast(:ids as bigint[])) order by id"
            ),
            {"ids": ids},
        ).all()
        for r in rows:
            d = dict(r._mapping)
            d["difference"] = d["received_qty"] - d["expected_qty"]
            d["acknowledged_by_name"] = _who(d["acknowledged_by"])
            by_po[r.po_id].append(d)
    items = []
    for h in heads:
        d = dict(h._mapping)
        d["issues"] = by_po.get(h.id, [])
        d["issue_count"] = len(d["issues"])
        items.append(d)
    return {"total": total, "items": items}


@router.get("/{po_id}")
@needs("inventory.purchase")
def get_po(po_id: int, db: Session = Depends(get_db)):
    return _po_detail(db, po_id)


class PoEditLine(BaseModel):
    line_id: Optional[int] = None          # an existing open line; missing = a part added now
    product_id: int
    quantity: int = Field(..., ge=1, le=MAX_QTY)
    rate: Optional[float] = Field(None, ge=0, le=1_000_000)   # the cost; empty = keep (or the product's cost for a new line)
    description: Optional[str] = Field(None, max_length=250)


class PoEdit(BaseModel):
    distributor_id: Optional[int] = None
    po_number: Optional[int] = Field(None, ge=1, le=99_999_999)   # a NEW number; it must not be used by another PO
    ship_to: Optional[str] = Field(None, max_length=400)          # missing = unchanged, empty = none
    note: Optional[str] = Field(None, max_length=300)
    expected_date: Optional[date] = None
    lines: list[PoEditLine] = Field(..., max_length=200)          # the lines that are still open; received lines are locked


@router.put("/{po_id}")
@needs("inventory.purchase")
def edit_po(po_id: int, body: PoEdit, db: Session = Depends(get_db)):
    try:
        h = db.execute(
            text("select id, po_number, distributor_id, sent_at, status from po_headers where id = :id for update"),
            {"id": po_id},
        ).first()
        if not h:
            db.rollback()
            raise HTTPException(404, "That purchase order doesn't exist.")
        if h.status == "cancelled":
            db.rollback()
            raise HTTPException(409, "This purchase order was cancelled, so it can't be edited.")

        renumbered = body.po_number is not None and body.po_number != h.po_number
        if renumbered and db.execute(
            text("select 1 from po_headers where po_number = :n and id <> :id"), {"n": body.po_number, "id": po_id}
        ).first():
            db.rollback()
            raise HTTPException(409, f"PO number {body.po_number} is already used by another purchase order. Pick an unused one.")

        sets: dict = {}
        if body.distributor_id is not None and body.distributor_id != h.distributor_id:
            dist = db.execute(
                text("select id, name, address from po_distributors where id = :id"), {"id": body.distributor_id}
            ).first()
            if not dist:
                db.rollback()
                raise HTTPException(404, "That distributor no longer exists.")
            sets.update(distributor_id=dist.id, vendor_name=dist.name, vendor_address=dist.address)
        if body.ship_to is not None:
            sets["ship_to"] = body.ship_to.strip() or None
        if body.note is not None:
            sets["note"] = body.note.strip() or None
        if renumbered:
            sets["po_number"] = body.po_number
            sets["sent_at"] = None   # a new number is a new document to send

        existing = db.execute(
            text("select id, product_id, quantity, rate, received_at, coalesce(received_qty, 0) as got from po_lines where po_id = :id order by id for update"),
            {"id": po_id},
        ).all()
        open_by_id = {l.id: l for l in existing if l.received_at is None}
        received_count = sum(1 for l in existing if l.received_at is not None)

        wanted_products: set[int] = set()
        for l in body.lines:
            if l.line_id is not None and l.line_id not in open_by_id:
                db.rollback()
                raise HTTPException(409, "A line on this PO was received or changed meanwhile. Reload the PO and try again.")
            if l.product_id in wanted_products:
                db.rollback()
                raise HTTPException(400, "The same part is on this PO twice. Combine them into one line.")
            wanted_products.add(l.product_id)
        if not body.lines and received_count == 0:
            db.rollback()
            raise HTTPException(400, "A purchase order needs at least one part. To get rid of it, cancel the PO instead.")

        prods = {
            r.id: r
            for r in db.execute(
                text(f"select p.id, p.title, p.cost, {_MPN} as part_number from products p "
                     "where p.id = any(cast(:ids as bigint[])) order by p.id for update of p"),
                {"ids": sorted(wanted_products) or [0]},
            ).all()
        }
        if len(prods) != len(wanted_products):
            db.rollback()
            raise HTTPException(404, "One of those parts no longer exists. Nothing was changed.")

        kept_ids = set()
        added: list[int] = []
        for l in body.lines:
            if l.line_id is not None:
                kept_ids.add(l.line_id)
                old = open_by_id[l.line_id]
                if l.quantity < old.got:
                    db.rollback()
                    raise HTTPException(400, f"{old.got} of that part were already received on this PO, so the quantity can't go below {old.got}.")
                if old.product_id != l.product_id:
                    db.rollback()
                    raise HTTPException(400, "A line's part can't be swapped. Remove the line and add the other part.")
                db.execute(
                    text("update po_lines set quantity = :q, rate = coalesce(cast(:r as numeric), rate), "
                         "description = coalesce(nullif(btrim(cast(:d as text)), ''), description) where id = :id"),
                    {"q": l.quantity, "r": l.rate, "d": l.description, "id": l.line_id},
                )
            else:
                p = prods[l.product_id]
                rate = l.rate if l.rate is not None else (float(p.cost) if p.cost is not None else 0.0)
                db.execute(
                    text("insert into po_lines (po_id, product_id, item, description, quantity, rate) "
                         "values (:po, :p, :item, :desc, :q, :rate)"),
                    {"po": po_id, "p": l.product_id, "item": p.part_number,
                     "desc": ((l.description or "").strip() or p.title or "")[:250], "q": l.quantity, "rate": rate},
                )
                added.append(l.product_id)

        if any(open_by_id[i].got > 0 for i in open_by_id if i not in kept_ids):
            db.rollback()
            raise HTTPException(400, "A line that is already partly received can't be removed from the PO.")
        removed = [open_by_id[i].product_id for i in open_by_id if i not in kept_ids]
        for i in open_by_id:
            if i not in kept_ids:
                db.execute(text("delete from po_lines where id = :id"), {"id": i})

        totals = db.execute(
            text("select quantity, rate from po_lines where po_id = :id"), {"id": po_id}
        ).all()
        sets["total_cents"] = int(po_total([{"quantity": t.quantity, "rate": t.rate} for t in totals]) * 100)
        cols = ", ".join(f"{k} = :{k}" for k in sets)  # keys are set above in this function, never from the client
        db.execute(text(f"update po_headers set {cols}, updated_at = now() where id = :id"), {**sets, "id": po_id})

        if added:
            db.execute(
                text("update products set on_order = true, updated_at = now() where id = any(cast(:ids as bigint[]))"),
                {"ids": added},
            )
        if body.expected_date and wanted_products:
            db.execute(
                text("update products set expected_date = :d, updated_at = now() "
                     "where id = any(cast(:ids as bigint[])) and on_order"),
                {"d": body.expected_date, "ids": sorted(wanted_products)},
            )
        _release_parts(db, removed)
        db.commit()
    except HTTPException:
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That PO number is already used by another purchase order. Pick an unused one.")
    except Exception as exc:
        db.rollback()
        log.exception("Editing a purchase order failed")
        raise HTTPException(500, f"The purchase order was not changed. ({type(exc).__name__})")

    po = _po_detail(db, po_id)
    po["renumbered"] = bool(renumbered)
    return po


class SentIn(BaseModel):
    sent: bool


@router.put("/{po_id}/sent")
@needs("inventory.purchase")
def mark_sent(po_id: int, body: SentIn, db: Session = Depends(get_db)):
    got = db.execute(
        text(
            "update po_headers set sent_at = case when :s then now() else null end "
            "where id = :id and status <> 'cancelled' returning sent_at"
        ),
        {"s": body.sent, "id": po_id},
    ).first()
    if not got:
        db.rollback()
        raise HTTPException(404, "That purchase order doesn't exist, or it was cancelled.")
    db.commit()
    return {"id": po_id, "sent_at": got.sent_at}


class CancelIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=300)


@router.post("/{po_id}/cancel")
@needs("inventory.purchase")
def cancel_po(po_id: int, body: CancelIn, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    """Cancel a PO. It is never deleted: it keeps its number and stays in the history, marked cancelled.
    Stock counts are not touched. Parts that were only on this PO stop being Backordered. A PO that has
    been received in full can't be cancelled; one that is part-received keeps what was already received."""
    try:
        h = db.execute(
            text("select id, po_number, status from po_headers where id = :id for update"), {"id": po_id}
        ).first()
        if not h:
            db.rollback()
            raise HTTPException(404, "That purchase order doesn't exist.")
        if h.status == "cancelled":
            db.rollback()
            raise HTTPException(409, f"Purchase order {h.po_number} is already cancelled.")
        if h.status == "received":
            db.rollback()
            raise HTTPException(409, f"Purchase order {h.po_number} was already received in full, so it can't be cancelled.")
        lines = db.execute(text("select product_id, received_at from po_lines where po_id = :id"), {"id": po_id}).all()
        open_products = [l.product_id for l in lines if l.received_at is None and l.product_id is not None]
        db.execute(
            text(
                "update po_headers set status = 'cancelled', cancelled_at = now(), cancelled_by = :by, "
                "cancel_reason = :why, updated_at = now() where id = :id"
            ),
            {"id": po_id, "by": user.email, "why": (body.reason or "").strip() or None},
        )
        _release_parts(db, open_products)
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        log.exception("Cancelling a purchase order failed")
        raise HTTPException(500, f"The purchase order was not cancelled. ({type(exc).__name__})")
    return {"ok": True, "po_number": h.po_number, "received_lines": sum(1 for l in lines if l.received_at is not None)}


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
