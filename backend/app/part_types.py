"""
Part types: a managed list instead of free text. A part type says WHAT a product is (Piston Kit, Fork & Dust Seal Kit);
the category says WHERE it lives on the site. Each type is linked to one or more categories and applies to everything
under them, so the product screen offers the types that belong to the product's category (with "show all" for the rest).

Tables (created at start-up):
  part_types            id, name (unique ignoring case)
  part_type_categories  part_type_id, category_id   (a type with no link fits every category)
The product keeps storing the type's NAME in products.part_type, so anything that already reads that column keeps working.

  GET  /part-types?category_id=     every type, each marked fits (belongs to that category or one above it)
  POST /part-types                  add a type (a near-duplicate is offered back instead); links it to category_id if given
  POST /part-types/{id}/link        list an existing type under another category
  GET  /part-types/manage           every type with its product count and linked categories (the Part types screen)
  PATCH /part-types/{id}            rename a type; products using the old name are moved to the new one
  POST /part-types/standardize      run the one-time clean-up again by hand (owner / admin)
"""
import difflib
import logging
import re
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user, needs, require_route_access
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/part-types", tags=["admin-part-types"], dependencies=[Depends(require_route_access)])

READ = ("products.view", "products.edit_details", "products.create")
WRITE = ("products.edit_details", "products.create")

# The standard list and the categories each type belongs to (by category name).
STANDARD: list[tuple[str, list[str]]] = [
    ("Piston Kit", ["Pistons & Top-End Kits"]),
    ("Piston Kit & Top End Gasket Set", ["Pistons & Top-End Kits"]),
    ("Top End Rebuild Kit", ["Pistons & Top-End Kits"]),
    ("Complete Cylinder", ["Pistons & Top-End Kits"]),
    ("Wrist Pin Bearing", ["Pistons & Top-End Kits"]),
    ("Carburetor Rebuild Kit", ["Carburetor & Injection Parts"]),
    ("Fork & Dust Seal Kit", ["Fork Parts"]),
    ("Shift Lever", ["Shift Levers"]),
    ("Clutch & Hand Brake Lever Kit", ["Levers"]),
    ("Clutch Lever", ["Levers"]),
    ("Differential Seal", ["Drivetrain"]),
    ("Fuel Tank Cap", ["Gas Caps"]),
    ("Radiator Cap", ["Radiators & Cooling"]),
    ("Oil Filter", ["Oil Filters"]),
    ("Recoil Starter", ["Starters & Recoils"]),
    ("Steering Bearing & Seal Kit", ["Steering Stem Bearings"]),
    ("Transaxle Seal", ["Wheel Bearings & Seals"]),
    ("Transaxle Bearing & Seal", ["General Bearings & Seals"]),
    ("Wheel Bearing Kit", ["Wheel Bearings & Seals"]),
    ("Valve Shim Kit", ["Valve Train"]),
]

# Old wording (lower case) -> the standard type, for the one-time clean-up of existing products.
RENAMES: dict[str, str] = {
    "piston kit & gasket set": "Piston Kit & Top End Gasket Set",
    "piston kit and gaskets": "Piston Kit & Top End Gasket Set",
    "piston kit with gasket kit": "Piston Kit & Top End Gasket Set",
    "piston kit with top end gaskets": "Piston Kit & Top End Gasket Set",
    "top end engine rebuild kit": "Top End Rebuild Kit",
    "engine rebuild kit": "Top End Rebuild Kit",
    "top end kit": "Top End Rebuild Kit",
    "carb kit": "Carburetor Rebuild Kit",
    "fork and dust seal kit": "Fork & Dust Seal Kit",
    "shifter": "Shift Lever",
    "lever": "Clutch Lever",
    "filters": "Oil Filter",
    "transaxle bearing and seal": "Transaxle Bearing & Seal",
}


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------
def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "")).strip()


def _norm(name: str) -> str:
    """For telling whether two names are the same thing: case, '&' / 'and', punctuation and a plural 's' do not count."""
    words = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower().replace("&", " and ")).split()
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words)


def _similar(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if min(len(na), len(nb)) >= 5 and (na in nb or nb in na):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85


# --------------------------------------------------------------------------
# tables, the standard list, and the one-time clean-up
# --------------------------------------------------------------------------
def ensure_tables() -> None:
    db = _session_factory()()
    try:
        db.execute(text("create table if not exists part_types (id bigserial primary key, name text not null, created_at timestamptz not null default now())"))
        db.execute(text("create unique index if not exists part_types_name_key on part_types (lower(name))"))
        db.execute(
            text(
                "create table if not exists part_type_categories ("
                "part_type_id bigint not null references part_types(id) on delete cascade, category_id bigint not null, "
                "primary key (part_type_id, category_id))"
            )
        )
        db.execute(text("create table if not exists part_types_standardized (id int primary key default 1, done_at timestamptz not null default now(), summary text)"))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not create the part type tables; the Part type list will not work until they exist")
    finally:
        db.close()


def standardize(db: Session) -> dict:
    """Add the standard types and their category links, then rewrite existing products' part types to the standard wording.
    Safe to repeat. A value that is not in any rule is left exactly as it is. Commits."""
    res = {"types_added": 0, "links_added": 0, "products_changed": 0, "category_names_not_found": []}
    ids: dict[str, int] = {}
    for name, cats in STANDARD:
        row = db.execute(
            text("insert into part_types (name) values (:n) on conflict (lower(name)) do nothing returning id"), {"n": name}
        ).first()
        if row:
            res["types_added"] += 1
        tid = row.id if row else db.execute(text("select id from part_types where lower(name) = lower(:n)"), {"n": name}).scalar()
        ids[name] = tid
        for cname in cats:
            cids = [r.id for r in db.execute(text("select id from categories where lower(btrim(name)) = lower(:c)"), {"c": cname}).all()]
            if not cids:
                res["category_names_not_found"].append(cname)
            for cid in cids:
                res["links_added"] += db.execute(
                    text("insert into part_type_categories (part_type_id, category_id) values (:t, :c) on conflict do nothing"),
                    {"t": tid, "c": cid},
                ).rowcount
    db.commit()

    def move(sql: str, params: dict) -> None:
        res["products_changed"] += db.execute(text(sql), params).rowcount

    # the same name in a different case or spacing -> the standard spelling
    for name, _cats in STANDARD:
        move("update products set part_type = :new where lower(btrim(part_type)) = lower(:new) and part_type is distinct from :new", {"new": name})
    for old, new in RENAMES.items():
        move("update products set part_type = :new where lower(btrim(part_type)) = :old and part_type is distinct from :new", {"old": old, "new": new})
    # "Engine" says where a part goes, not what it is: decided by the product's category
    move(
        "update products p set part_type = 'Recoil Starter' from categories c where c.id = p.category_id "
        "and lower(btrim(p.part_type)) = 'engine' and lower(btrim(c.name)) = 'starters & recoils'",
        {},
    )
    move(
        "update products p set part_type = 'Top End Rebuild Kit' from categories c where c.id = p.category_id "
        "and lower(btrim(p.part_type)) = 'engine' and lower(btrim(c.name)) = 'pistons & top-end kits'",
        {},
    )
    db.commit()
    return res


def start_standardize_once() -> None:
    """Start-up: run the clean-up once (a little after start, in the background), then remember it was done."""

    def run() -> None:
        time.sleep(30)
        db = _session_factory()()
        try:
            if db.execute(text("select 1 from part_types_standardized where id = 1")).first():
                return
            res = standardize(db)
            db.execute(text("insert into part_types_standardized (id, summary) values (1, :s) on conflict (id) do nothing"), {"s": str(res)})
            db.commit()
            log.info("part types standardized: %s", res)
        except Exception:
            db.rollback()
            log.exception("standardizing part types failed (it will be tried again at the next start-up)")
        finally:
            db.close()

    threading.Thread(target=run, name="part-types-standardize", daemon=True).start()


# --------------------------------------------------------------------------
# used by the product save
# --------------------------------------------------------------------------
def canonical_part_type(db: Session, value: Optional[str]) -> Optional[str]:
    """The list's own spelling of `value`, None when it is blank. A value that is not on the list is refused, so the field cannot
    drift back to free text. If the list itself cannot be read the value is let through rather than blocking the save."""
    v = _clean(value or "")
    if not v:
        return None
    try:
        name = db.execute(text("select name from part_types where lower(name) = lower(:n)"), {"n": v}).scalar()
    except Exception:
        db.rollback()
        log.exception("could not read the part type list")
        return v
    if not name:
        raise HTTPException(400, f'"{v}" is not on the Part type list. Pick one from the list, or use Add a type first.')
    return name


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def _item(db: Session, tid: int) -> dict:
    r = db.execute(text("select id, name from part_types where id = :i"), {"i": tid}).first()
    return {"id": r.id, "name": r.name}


@router.get("")
@needs(*READ)
def list_part_types(category_id: Optional[int] = None, db: Session = Depends(get_db)):
    rows = db.execute(text("select id, name from part_types order by lower(name)")).all()
    links: dict[int, set[int]] = {}
    for r in db.execute(text("select part_type_id, category_id from part_type_categories")).all():
        links.setdefault(r.part_type_id, set()).add(r.category_id)
    above: set[int] = set()
    if category_id is not None:
        above = {
            r.id
            for r in db.execute(
                text(
                    "with recursive a as (select id, parent_id from categories where id = :c "
                    "union select c.id, c.parent_id from categories c join a on c.id = a.parent_id) select id from a"
                ),
                {"c": category_id},
            ).all()
        }
    items = []
    for r in rows:
        mine = links.get(r.id, set())
        fits = category_id is None or not mine or bool(mine & above)  # a type with no link fits everywhere
        items.append({"id": r.id, "name": r.name, "fits": fits, "linked": sorted(mine)})
    return {"items": items}


class PartTypeIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    category_id: Optional[int] = None
    force: bool = False  # add it even though a similar one exists


def _link(db: Session, tid: int, category_id: Optional[int]) -> None:
    if category_id is None:
        return
    if not db.execute(text("select 1 from categories where id = :c"), {"c": category_id}).first():
        raise HTTPException(400, "That category does not exist.")
    db.execute(text("insert into part_type_categories (part_type_id, category_id) values (:t, :c) on conflict do nothing"), {"t": tid, "c": category_id})


@router.post("")
@needs(*WRITE)
def add_part_type(body: PartTypeIn, db: Session = Depends(get_db)):
    """Add a type. The same thing in other words (case, '&' / 'and', a plural) is not added twice: the existing one is returned.
    A close match is offered back first (`similar`); send force = true to add it anyway."""
    name = _clean(body.name)
    if not name:
        raise HTTPException(400, "Type a name.")
    existing = db.execute(text("select id, name from part_types")).all()
    same = [e for e in existing if _norm(e.name) == _norm(name)]
    if same:
        _link(db, same[0].id, body.category_id)
        db.commit()
        return {"created": False, "matched": True, "item": {"id": same[0].id, "name": same[0].name}}
    close = [{"id": e.id, "name": e.name} for e in existing if _similar(e.name, name)]
    if close and not body.force:
        return {"created": False, "matched": False, "similar": close[:5]}
    tid = db.execute(text("insert into part_types (name) values (:n) returning id"), {"n": name}).scalar()
    _link(db, tid, body.category_id)
    db.commit()
    return {"created": True, "matched": False, "item": _item(db, tid)}


class LinkIn(BaseModel):
    category_id: int


@router.post("/{type_id}/link")
@needs(*WRITE)
def link_part_type(type_id: int, body: LinkIn, db: Session = Depends(get_db)):
    if not db.execute(text("select 1 from part_types where id = :i"), {"i": type_id}).first():
        raise HTTPException(404, "Part type not found")
    _link(db, type_id, body.category_id)
    db.commit()
    return {"ok": True}


@router.get("/manage")
@needs(*READ)
def manage_list(db: Session = Depends(get_db)):
    """For the Part types screen: each type, how many products use it, and the categories it is listed under."""
    counts = {
        (r.n or "").lower(): r.c
        for r in db.execute(
            text("select lower(btrim(part_type)) as n, count(*) as c from products where nullif(btrim(part_type), '') is not null group by 1")
        ).all()
    }
    links: dict[int, list[int]] = {}
    for r in db.execute(text("select part_type_id, category_id from part_type_categories")).all():
        links.setdefault(r.part_type_id, []).append(r.category_id)
    rows = db.execute(text("select id, name from part_types order by lower(name)")).all()
    return {"items": [{"id": r.id, "name": r.name, "products": counts.get(r.name.lower().strip(), 0), "category_ids": sorted(links.get(r.id, []))} for r in rows]}


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.patch("/{type_id}")
@needs(*WRITE)
def rename_part_type(type_id: int, body: RenameIn, db: Session = Depends(get_db)):
    """Rename a type. Products store the type's name, so every product using the old name is moved to the new one in the same step.
    Refused when another type already means the same thing (case, '&' / 'and', a plural): use that one instead."""
    new = _clean(body.name)
    if not new:
        raise HTTPException(400, "Type a name.")
    cur = db.execute(text("select id, name from part_types where id = :i for update"), {"i": type_id}).first()
    if not cur:
        raise HTTPException(404, "Part type not found")
    if cur.name == new:
        return {"ok": True, "item": {"id": cur.id, "name": cur.name}, "products_changed": 0}
    for e in db.execute(text("select id, name from part_types where id <> :i"), {"i": type_id}).all():
        if _norm(e.name) == _norm(new):
            raise HTTPException(409, f'"{e.name}" is already on the list and means the same thing. Use that one, or pick a different name.')
    db.execute(text("update part_types set name = :n where id = :i"), {"n": new, "i": type_id})
    changed = db.execute(
        text("update products set part_type = :n where lower(btrim(part_type)) = lower(:o) and part_type is distinct from :n"),
        {"n": new, "o": cur.name.strip()},
    ).rowcount
    db.commit()
    return {"ok": True, "item": {"id": cur.id, "name": new}, "products_changed": changed}


@router.post("/standardize")
@needs(*WRITE)
def run_standardize(db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Only an owner or admin can do this.")
    return standardize(db)
