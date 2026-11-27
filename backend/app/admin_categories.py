"""
Manage the product categories themselves (the menu tree on the storefront): add, rename, move, reorder, delete.
Part types have their own screen (part_types.py); this one is for the categories those types are listed under.

Owner / admin only (no @needs marker, so require_route_access keeps employees out).

  GET    /category-manage        every category with its direct product count and number of sub-categories
  POST   /category-manage        add a category (name, optional parent_id, optional sort_order)
  PATCH  /category-manage/{id}   rename, move under another parent, change sort order (slug stays the same unless sent)
  DELETE /category-manage/{id}   only when it has no products and no sub-categories

Renaming keeps the slug so storefront links and the sitemap do not break. Products point at the category by id, so a
rename or a move changes nothing on the products themselves.
"""
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_route_access
from app.database import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/category-manage", tags=["admin-categories"], dependencies=[Depends(require_route_access)])


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _unique_slug(db: Session, base: str, exclude_id: Optional[int] = None) -> str:
    base = _slugify(base) or "category"
    slug, n = base, 2
    while db.execute(
        text("select 1 from categories where slug = :s and (cast(:x as bigint) is null or id <> cast(:x as bigint))"),
        {"s": slug, "x": exclude_id},
    ).first():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


def _check_parent(db: Session, parent_id: Optional[int], self_id: Optional[int] = None) -> None:
    if parent_id is None:
        return
    if not db.execute(text("select 1 from categories where id = :p"), {"p": parent_id}).first():
        raise HTTPException(400, "That parent category does not exist.")
    if self_id is not None:
        # walking up from the new parent must never reach the category being moved
        up = db.execute(
            text(
                "with recursive a as (select id, parent_id from categories where id = :p "
                "union select c.id, c.parent_id from categories c join a on c.id = a.parent_id) select id from a"
            ),
            {"p": parent_id},
        ).all()
        if self_id in {r.id for r in up}:
            raise HTTPException(400, "A category cannot be moved under itself or one of its own sub-categories.")


def _name_taken(db: Session, name: str, parent_id: Optional[int], exclude_id: Optional[int] = None) -> bool:
    return bool(
        db.execute(
            text(
                "select 1 from categories where lower(btrim(name)) = lower(:n) "
                "and parent_id is not distinct from cast(:p as bigint) "
                "and (cast(:x as bigint) is null or id <> cast(:x as bigint))"
            ),
            {"n": name, "p": parent_id, "x": exclude_id},
        ).first()
    )


@router.get("")
def list_manage(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select c.id, c.parent_id, c.name, c.slug, coalesce(c.sort_order, 0) as sort_order, "
            "(select count(*) from products p where p.category_id = c.id) as products, "
            "(select count(*) from categories k where k.parent_id = c.id) as children "
            "from categories c order by coalesce(c.sort_order, 0), lower(c.name)"
        )
    ).all()
    return {"items": [dict(r._mapping) for r in rows]}


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    parent_id: Optional[int] = None
    sort_order: Optional[int] = None


@router.post("")
def add_category(body: CategoryIn, db: Session = Depends(get_db)):
    name = _clean(body.name)
    if not name:
        raise HTTPException(400, "Type a name.")
    _check_parent(db, body.parent_id)
    if _name_taken(db, name, body.parent_id):
        raise HTTPException(409, f'"{name}" already exists in that spot.')
    so = body.sort_order
    if so is None:
        so = (
            db.execute(
                text("select coalesce(max(coalesce(sort_order, 0)), 0) + 1 from categories where parent_id is not distinct from cast(:p as bigint)"),
                {"p": body.parent_id},
            ).scalar()
            or 1
        )
    cid = db.execute(
        text("insert into categories (name, slug, parent_id, sort_order) values (:n, :s, :p, :o) returning id"),
        {"n": name, "s": _unique_slug(db, name), "p": body.parent_id, "o": so},
    ).scalar()
    db.commit()
    return {"ok": True, "id": cid}


class CategoryPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    parent_id: Optional[int] = None
    move: bool = False  # parent_id = null is a real choice (top level), so a move has to say so
    sort_order: Optional[int] = None
    slug: Optional[str] = Field(default=None, max_length=80)


@router.patch("/{category_id}")
def edit_category(category_id: int, body: CategoryPatch, db: Session = Depends(get_db)):
    cur = db.execute(text("select id, name, parent_id, slug, sort_order from categories where id = :i for update"), {"i": category_id}).first()
    if not cur:
        raise HTTPException(404, "Category not found")
    name = _clean(body.name) if body.name is not None else cur.name
    if not name:
        raise HTTPException(400, "Type a name.")
    parent = body.parent_id if body.move else cur.parent_id
    if body.move:
        _check_parent(db, parent, category_id)
    if (name != cur.name or parent != cur.parent_id) and _name_taken(db, name, parent, category_id):
        raise HTTPException(409, f'"{name}" already exists in that spot.')
    slug = cur.slug
    if body.slug is not None and _slugify(body.slug) and _slugify(body.slug) != cur.slug:
        slug = _unique_slug(db, body.slug, category_id)
    so = body.sort_order if body.sort_order is not None else cur.sort_order
    db.execute(
        text("update categories set name = :n, parent_id = :p, slug = :s, sort_order = :o where id = :i"),
        {"n": name, "p": parent, "s": slug, "o": so, "i": category_id},
    )
    db.commit()
    return {"ok": True}


@router.delete("/{category_id}")
def delete_category(category_id: int, db: Session = Depends(get_db)):
    cur = db.execute(text("select id, name from categories where id = :i for update"), {"i": category_id}).first()
    if not cur:
        raise HTTPException(404, "Category not found")
    n_prod = db.execute(text("select count(*) from products where category_id = :i"), {"i": category_id}).scalar() or 0
    n_kids = db.execute(text("select count(*) from categories where parent_id = :i"), {"i": category_id}).scalar() or 0
    if n_prod or n_kids:
        raise HTTPException(
            409,
            f'"{cur.name}" still has {n_prod} product(s) and {n_kids} sub-categor{"y" if n_kids == 1 else "ies"}. Move them first, then delete it.',
        )
    db.execute(text("delete from part_type_categories where category_id = :i"), {"i": category_id})
    db.execute(text("delete from categories where id = :i"), {"i": category_id})
    db.commit()
    return {"ok": True}
