"""
Public storefront extras: categories, product search, and the customer garage.
Mounted at /store/*.

  GET    /store/categories      public
  GET    /store/search          public (text, category, machine type, and/or a chosen machine)
  POST   /store/cart/price      public (current price and stock for the items in a cart)
  GET    /store/garage          signed-in customer
  POST   /store/garage          signed-in customer
  DELETE /store/garage/{id}     signed-in customer
  GET    /store/addresses       signed-in customer (shipping and billing addresses)
  POST   /store/addresses       signed-in customer
  POST   /store/addresses/{id}  signed-in customer (edit)
  POST   /store/addresses/{id}/default   signed-in customer
  DELETE /store/addresses/{id}  signed-in customer

Garage routes use the Supabase login (any signed-in user, not just staff) and
only ever touch rows belonging to the caller's own user id.
"""
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user
from app.database import get_db
from app.images import card_image
from app.stock import available_sql

router = APIRouter(prefix="/store", tags=["store"])

PUBLISHED_FILTER = "p.is_published = true"
MAX_GARAGE = 12
MAX_ADDRESSES = 10


# --------------------------------------------------------------------------
# categories
# --------------------------------------------------------------------------
@router.get("/categories")
def categories(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select c.id, c.parent_id, c.name, c.slug, c.sort_order, "
            "(select count(*) from products p "
            f" where p.category_id = c.id and {PUBLISHED_FILTER}) as product_count "
            "from categories c order by c.sort_order, c.name"
        )
    ).all()
    out = [dict(r._mapping) for r in rows]
    # The menu hides empty categories using product_count. If nothing is
    # published yet, leave the counts out so the whole menu is not hidden.
    if not any(r["product_count"] for r in out):
        for r in out:
            del r["product_count"]
    return out


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------
@router.get("/search")
def search(
    q: Optional[str] = None,
    category: Optional[str] = None,
    year: Optional[int] = None,
    model_id: Optional[int] = None,
    type: Optional[str] = None,  # machine type slug: atv, utv, snowmobile, dirt-bike, motorcycle, pwc
    limit: int = Query(24, le=100),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    # Every word must match the title, SKU or a part number (any order).
    words = re.sub(r"[%_\\]", " ", (q or "")[:100]).split()[:5]
    # An unknown type is ignored (all types), so a bad link never gives an empty store.
    vtype = type if type in {"atv", "utv", "snowmobile", "dirt-bike", "motorcycle", "pwc"} else None
    params: dict = {"cat": category or None, "year": year, "model_id": model_id, "vtype": vtype}
    word_sql = ""
    for i, w in enumerate(words):
        params[f"w{i}"] = f"%{w}%"
        word_sql += (
            f" and (p.title ilike :w{i} or p.sku ilike :w{i}"
            f" or p.manufacturer_part_number ilike :w{i}"
            f" or p.internal_part_number ilike :w{i})"
        )

    cte = (
        "with recursive tree as ("
        " select id from categories where slug = cast(:cat as text)"
        " union all"
        " select c.id from categories c join tree t on c.parent_id = t.id)"
    )
    where = f"""
        where {PUBLISHED_FILTER}
        {word_sql}
        and (cast(:cat as text) is null or p.category_id in (select id from tree))
        and (
          cast(:vtype as text) is null
          or p.is_universal
          or exists (
            select 1 from product_fitment pf
            join vehicles v on v.id = pf.vehicle_id
            join vehicle_models vm on vm.id = v.model_id
            join vehicle_types vt on vt.id = vm.vehicle_type_id
            where pf.product_id = p.id and vt.slug = cast(:vtype as text)
          )
        )
        and (
          cast(:model_id as bigint) is null or cast(:year as int) is null
          or p.is_universal
          or exists (
            select 1 from product_fitment pf
            join vehicles v on v.id = pf.vehicle_id
            where pf.product_id = p.id
              and v.model_id = cast(:model_id as bigint)
              and v.year = cast(:year as int)
          )
        )
    """
    total = db.execute(text(f"{cte} select count(*) from products p {where}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            {cte}
            select p.id, p.title, p.slug, p.list_price, p.is_universal,
              (select ph.source_url from product_photos ph
                where ph.product_id = p.id
                order by ph.is_primary desc, ph.sort_order, ph.id limit 1) as photo_source_url,
              (select ph.image_key from product_photos ph
                where ph.product_id = p.id
                order by ph.is_primary desc, ph.sort_order, ph.id limit 1) as photo_image_key
            from products p {where}
            order by p.title limit :limit offset :offset
            """
        ),
        {**params, "limit": limit, "offset": offset},
    ).all()
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "slug": r.slug,
                "price": f"{r.list_price:.2f}" if r.list_price is not None else None,
                "universal": r.is_universal,
                **card_image(r.photo_image_key, r.photo_source_url),
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------
# cart
# The cart itself lives in the customer's browser. This only answers "what do
# these items cost right now, and how many can be bought?" so the screen never
# relies on a price the browser remembered.
# --------------------------------------------------------------------------
class CartItemIn(BaseModel):
    product_id: int
    qty: int = Field(..., ge=1, le=999)


class CartPriceIn(BaseModel):
    items: list[CartItemIn] = Field(..., max_length=50)


@router.post("/cart/price")
def cart_price(body: CartPriceIn, db: Session = Depends(get_db)):
    ids = list({i.product_id for i in body.items})
    if not ids:
        return {"items": []}
    rows = db.execute(
        text(
            f"""
            select p.id, p.slug, p.sku, p.title, p.list_price, p.on_order, p.expected_date,
              (p.expected_date - current_date) as days_until,
              {available_sql("p")} as available_qty,
              (select ph.source_url from product_photos ph
                where ph.product_id = p.id
                order by ph.is_primary desc, ph.sort_order, ph.id limit 1) as photo_source_url,
              (select ph.image_key from product_photos ph
                where ph.product_id = p.id
                order by ph.is_primary desc, ph.sort_order, ph.id limit 1) as photo_image_key
            from products p
            where p.id in :ids and {PUBLISHED_FILTER}
            """
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    by_id = {r.id: r for r in rows}
    out = []
    for it in body.items:
        r = by_id.get(it.product_id)
        if not r:
            out.append({"product_id": it.product_id, "problem": "unavailable"})
            continue
        # What can still be bought: the shelf count minus parts promised to paid orders and to
        # other customers' open checkouts. Never negative.
        available = max(0, int(r.available_qty or 0))
        qty = it.qty
        problem = None
        if r.list_price is None:
            problem = "no_price"
        elif available <= 0:
            problem = "sold_out"
        elif qty > available:
            qty = available
            problem = "reduced"
        out.append(
            {
                "product_id": r.id,
                "slug": r.slug,
                "sku": r.sku,
                "title": r.title,
                "price": f"{r.list_price:.2f}" if r.list_price is not None else None,
                "available": available,
                "backordered": bool(r.on_order) and available <= 0,
                "expected_date": r.expected_date.isoformat() if r.on_order and available <= 0 and r.expected_date else None,
                "days_until": int(r.days_until) if r.on_order and available <= 0 and r.days_until is not None else None,
                "qty": qty,
                "problem": problem,
                **card_image(r.photo_image_key, r.photo_source_url),
            }
        )
    return {"items": out}


# --------------------------------------------------------------------------
# garage (needs table public.customer_garage - see customer_garage.sql)
# --------------------------------------------------------------------------
GARAGE_SELECT = """
    select g.id, g.year, g.model_id, vm.make_id, mk.name as make,
           vm.name as model, vt.slug as type
    from customer_garage g
    join vehicle_models vm on vm.id = g.model_id
    join vehicle_makes mk on mk.id = vm.make_id
    left join vehicle_types vt on vt.id = vm.vehicle_type_id
"""


class GarageAdd(BaseModel):
    year: int
    model_id: int


@router.get("/garage")
def garage_list(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        text(f"{GARAGE_SELECT} where g.user_id = cast(:uid as uuid) order by g.created_at desc"),
        {"uid": user.user_id},
    ).all()
    return [dict(r._mapping) for r in rows]


@router.post("/garage", status_code=201)
def garage_add(body: GarageAdd, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    exists = db.execute(
        text("select 1 from vehicles where model_id = :m and year = :y limit 1"),
        {"m": body.model_id, "y": body.year},
    ).first()
    if not exists:
        raise HTTPException(404, "That year and model combination does not exist")

    count = db.execute(
        text("select count(*) from customer_garage where user_id = cast(:uid as uuid)"),
        {"uid": user.user_id},
    ).scalar()
    already = db.execute(
        text(
            "select id from customer_garage where user_id = cast(:uid as uuid) "
            "and model_id = :m and year = :y"
        ),
        {"uid": user.user_id, "m": body.model_id, "y": body.year},
    ).first()
    if not already and count >= MAX_GARAGE:
        raise HTTPException(400, f"Your garage is full ({MAX_GARAGE} machines). Remove one first.")

    if already:
        gid = already.id
    else:
        gid = db.execute(
            text(
                "insert into customer_garage (user_id, model_id, year) "
                "values (cast(:uid as uuid), :m, :y) returning id"
            ),
            {"uid": user.user_id, "m": body.model_id, "y": body.year},
        ).scalar()
        db.commit()

    row = db.execute(text(f"{GARAGE_SELECT} where g.id = :id"), {"id": gid}).first()
    return dict(row._mapping)


@router.delete("/garage/{garage_id}", status_code=204)
def garage_remove(garage_id: int, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(
        text("delete from customer_garage where id = :id and user_id = cast(:uid as uuid)"),
        {"id": garage_id, "uid": user.user_id},
    )
    db.commit()


# --------------------------------------------------------------------------
# addresses (needs table public.customer_addresses - see customer_addresses.sql)
# Card numbers are never stored here: payment details belong to the payment provider.
# --------------------------------------------------------------------------
ADDRESS_COLS = (
    "id, kind, full_name, line1, line2, city, region, postal_code, country, phone, is_default"
)


class AddressIn(BaseModel):
    kind: Literal["shipping", "billing"]
    full_name: str = Field(..., max_length=100)
    line1: str = Field(..., max_length=120)
    line2: Optional[str] = Field(None, max_length=120)
    city: str = Field(..., max_length=80)
    region: str = Field(..., max_length=50)
    postal_code: str = Field(..., max_length=12)
    country: str = Field("US", min_length=2, max_length=2)
    phone: Optional[str] = Field(None, max_length=25)
    is_default: bool = False


def _clean_address(body: AddressIn) -> dict:
    data = {
        "full_name": body.full_name.strip(),
        "line1": body.line1.strip(),
        "line2": (body.line2 or "").strip() or None,
        "city": body.city.strip(),
        "region": body.region.strip(),
        "postal_code": body.postal_code.strip(),
        "country": body.country.strip().upper(),
        "phone": (body.phone or "").strip() or None,
    }
    labels = {
        "full_name": "Full name",
        "line1": "Address",
        "city": "City",
        "region": "State / Province",
        "postal_code": "ZIP / Postal code",
    }
    for key, label in labels.items():
        if not data[key]:
            raise HTTPException(400, f"{label} is required")
    if len(data["postal_code"]) < 3:
        raise HTTPException(400, "Enter a valid ZIP / Postal code")
    return data


def _address_row(db: Session, address_id: int, uid: str) -> dict:
    row = db.execute(
        text(f"select {ADDRESS_COLS} from customer_addresses where id = :id and user_id = cast(:uid as uuid)"),
        {"id": address_id, "uid": uid},
    ).first()
    if not row:
        raise HTTPException(404, "Address not found")
    return dict(row._mapping)


@router.get("/addresses")
def addresses_list(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            f"select {ADDRESS_COLS} from customer_addresses where user_id = cast(:uid as uuid) "
            "order by kind, is_default desc, id"
        ),
        {"uid": user.user_id},
    ).all()
    return [dict(r._mapping) for r in rows]


@router.post("/addresses", status_code=201)
def address_add(body: AddressIn, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    data = _clean_address(body)
    count = db.execute(
        text("select count(*) from customer_addresses where user_id = cast(:uid as uuid)"),
        {"uid": user.user_id},
    ).scalar()
    if count >= MAX_ADDRESSES:
        raise HTTPException(400, f"You can save up to {MAX_ADDRESSES} addresses. Remove one first.")
    has_default = db.execute(
        text(
            "select 1 from customer_addresses where user_id = cast(:uid as uuid) and kind = :k and is_default limit 1"
        ),
        {"uid": user.user_id, "k": body.kind},
    ).first()
    make_default = body.is_default or not has_default  # the first address of a kind is the default
    if make_default:
        db.execute(
            text("update customer_addresses set is_default = false where user_id = cast(:uid as uuid) and kind = :k"),
            {"uid": user.user_id, "k": body.kind},
        )
    new_id = db.execute(
        text(
            "insert into customer_addresses (user_id, kind, full_name, line1, line2, city, region, postal_code, country, phone, is_default) "
            "values (cast(:uid as uuid), :k, :full_name, :line1, :line2, :city, :region, :postal_code, :country, :phone, :d) "
            "returning id"
        ),
        {"uid": user.user_id, "k": body.kind, "d": make_default, **data},
    ).scalar()
    db.commit()
    return _address_row(db, new_id, user.user_id)


@router.post("/addresses/{address_id}")
def address_edit(address_id: int, body: AddressIn, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    current = _address_row(db, address_id, user.user_id)
    data = _clean_address(body)
    if body.is_default:
        db.execute(
            text(
                "update customer_addresses set is_default = false "
                "where user_id = cast(:uid as uuid) and kind = :k and id <> :id"
            ),
            {"uid": user.user_id, "k": current["kind"], "id": address_id},
        )
    db.execute(
        text(
            "update customer_addresses set full_name = :full_name, line1 = :line1, line2 = :line2, city = :city, "
            "region = :region, postal_code = :postal_code, country = :country, phone = :phone, "
            "is_default = (is_default or :d) where id = :id and user_id = cast(:uid as uuid)"
        ),
        {"id": address_id, "uid": user.user_id, "d": body.is_default, **data},
    )
    db.commit()
    return _address_row(db, address_id, user.user_id)


@router.post("/addresses/{address_id}/default")
def address_make_default(address_id: int, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    current = _address_row(db, address_id, user.user_id)
    db.execute(
        text("update customer_addresses set is_default = (id = :id) where user_id = cast(:uid as uuid) and kind = :k"),
        {"id": address_id, "uid": user.user_id, "k": current["kind"]},
    )
    db.commit()
    return _address_row(db, address_id, user.user_id)


@router.delete("/addresses/{address_id}", status_code=204)
def address_remove(address_id: int, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    current = _address_row(db, address_id, user.user_id)
    db.execute(
        text("delete from customer_addresses where id = :id and user_id = cast(:uid as uuid)"),
        {"id": address_id, "uid": user.user_id},
    )
    # If the default was removed, the newest remaining address of that kind takes over.
    db.execute(
        text(
            "update customer_addresses set is_default = true where id = ("
            " select id from customer_addresses where user_id = cast(:uid as uuid) and kind = :k order by id desc limit 1"
            ") and not exists (select 1 from customer_addresses where user_id = cast(:uid as uuid) and kind = :k and is_default)"
        ),
        {"uid": user.user_id, "k": current["kind"]},
    )
    db.commit()
