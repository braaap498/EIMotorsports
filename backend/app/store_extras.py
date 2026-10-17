"""
Public storefront extras: categories, product search, and the customer garage.
Mounted at /store/*.

  GET    /store/categories      public
  GET    /store/search          public (text, category, and/or a chosen machine)
  GET    /store/garage          signed-in customer
  POST   /store/garage          signed-in customer
  DELETE /store/garage/{id}     signed-in customer

Garage routes use the Supabase login (any signed-in user, not just staff) and
only ever touch rows belonging to the caller's own user id.
"""
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user
from app.database import get_db
from app.images import card_image

router = APIRouter(prefix="/store", tags=["store"])

PUBLISHED_FILTER = "p.is_published = true"
MAX_GARAGE = 12


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
    limit: int = Query(24, le=100),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    # Every word must match the title, SKU or a part number (any order).
    words = re.sub(r"[%_\\]", " ", (q or "")[:100]).split()[:5]
    params: dict = {"cat": category or None, "year": year, "model_id": model_id}
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
