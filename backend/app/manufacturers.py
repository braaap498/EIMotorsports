"""
Manufacturers (the brand of a part): the list behind the product's "Manufacturer" field.

Mounted at /admin/manufacturers. A product points at one through products.part_manufacturer_id. The table is found from that
foreign key (the same way app/admin_cleanup.py finds it), so its name is never guessed. The Manufacturer is the source of the
eBay "Brand" item specific when a listing is made, and products made from the Autodist catalog get it from the catalog's brand.

  GET  /manufacturers          every manufacturer, by name
  POST /manufacturers {name}   the manufacturer with that name (any capitals), made when it does not exist yet
"""
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.admin_catalog import READ_LISTS
from app.auth import needs, require_route_access
from app.database import get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/manufacturers", tags=["admin-manufacturers"], dependencies=[Depends(require_route_access)])


def manufacturer_table(db: Session) -> Optional[str]:
    """The table products.part_manufacturer_id points at, or None when it cannot be found."""
    try:
        with db.begin_nested():
            ref = db.execute(
                text(
                    """
                    select c.confrelid::regclass::text
                    from pg_constraint c
                    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]
                    where c.contype = 'f' and c.conrelid = 'public.products'::regclass
                      and a.attname = 'part_manufacturer_id'
                    """
                )
            ).scalar()
    except Exception:
        return None
    if not ref or not re.fullmatch(r'[A-Za-z0-9_".]+', ref):
        return None
    return ref


LAST_ERROR: dict = {"msg": ""}  # why the last find_or_create gave None, so callers can show it instead of a bare "could not"


def _insert_manufacturer(db: Session, ref: str, n: str) -> int:
    """Insert `n`. A table with more required columns than `name` (no default, NOT NULL) gets them filled when we can guess
    them: slug-like columns get a slug of the name, id gets the next number when it has no default."""
    try:
        with db.begin_nested():
            return int(db.execute(text(f"insert into {ref} (name) values (:n) returning id"), {"n": n}).scalar())
    except Exception as first:
        LAST_ERROR["msg"] = str(first.orig if hasattr(first, "orig") else first)[:300]
        cols = db.execute(
            text(
                "select column_name, data_type, column_default from information_schema.columns "
                "where table_schema || '.' || table_name = replace(:r, '\"', '') and is_nullable = 'NO' and column_name <> 'name'"
            ),
            {"r": ref if "." in ref else f"public.{ref}"},
        ).all()
        slug = re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-") or "brand"
        vals: dict = {"name": n}
        exprs = {"name": ":name"}
        for c in cols:
            if c.column_name == "id" and c.column_default is None:
                exprs["id"] = f"(select coalesce(max(id), 0) + 1 from {ref})"
            elif c.column_default is not None:
                continue
            elif c.column_name in ("slug", "handle", "code", "key"):
                vals[c.column_name] = slug
                exprs[c.column_name] = f":{c.column_name}"
            elif c.data_type in ("boolean",):
                exprs[c.column_name] = "false"
            elif c.data_type in ("integer", "bigint", "smallint", "numeric"):
                exprs[c.column_name] = "0"
            elif c.data_type in ("timestamp with time zone", "timestamp without time zone"):
                exprs[c.column_name] = "now()"
            else:
                vals[c.column_name] = n
                exprs[c.column_name] = f":{c.column_name}"
        if len(exprs) == 1:
            raise
        with db.begin_nested():
            return int(
                db.execute(
                    text(f"insert into {ref} ({', '.join(exprs)}) values ({', '.join(exprs.values())}) returning id"), vals
                ).scalar()
            )


def find_or_create(db: Session, name: Optional[str], create: bool = True) -> Optional[int]:
    """The id of the manufacturer called `name` (capitals ignored). Makes it when `create` and it is missing. Commits nothing.
    Any problem (no table, the table needs other columns) just gives None, so callers carry on without a manufacturer;
    the reason is left in LAST_ERROR["msg"]."""
    LAST_ERROR["msg"] = ""
    n = " ".join((name or "").split())
    if not n or len(n) > 120:
        return None
    ref = manufacturer_table(db)
    if not ref:
        LAST_ERROR["msg"] = "the manufacturer table could not be found"
        return None
    try:
        with db.begin_nested():
            found = db.execute(text(f"select id from {ref} where lower(btrim(name)) = lower(:n) order by id limit 1"), {"n": n}).scalar()
        if found is not None:
            return int(found)
        if not create:
            return None
        return _insert_manufacturer(db, ref, n)
    except Exception as e:
        LAST_ERROR["msg"] = LAST_ERROR["msg"] or str(getattr(e, "orig", e))[:300]
        log.exception("could not find or make the manufacturer %r", n)
        return None


@router.get("")
@needs(*READ_LISTS)
def list_manufacturers(db: Session = Depends(get_db)):
    ref = manufacturer_table(db)
    if not ref:
        return {"items": [], "problem": "The manufacturer list could not be found in the database."}
    try:
        rows = db.execute(text(f"select id, name from {ref} where btrim(coalesce(name, '')) <> '' order by lower(name), id")).all()
    except Exception:
        db.rollback()
        return {"items": [], "problem": "The manufacturer list could not be read."}
    return {"items": [{"id": r.id, "name": r.name} for r in rows]}


class ManufacturerIn(BaseModel):
    name: str


@router.post("")
@needs("products.create", "products.edit_details")
def add_manufacturer(body: ManufacturerIn, db: Session = Depends(get_db)):
    name = " ".join(body.name.split())
    if not name or len(name) > 120:
        raise HTTPException(400, "Type the manufacturer's name (up to 120 characters).")
    mid = find_or_create(db, name)
    if mid is None:
        db.rollback()
        raise HTTPException(409, "The manufacturer could not be added (the list's table needs more than a name, or was not found).")
    db.commit()
    ref = manufacturer_table(db)
    shown = db.execute(text(f"select name from {ref} where id = :i"), {"i": mid}).scalar() if ref else name
    return {"id": mid, "name": shown or name}
