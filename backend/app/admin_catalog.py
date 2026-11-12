"""
Admin back-office: vehicle fitment (types, makes, models, years) and products.
Mounted at /admin/* via admin_router in main.py.

Access: routes are admin/owner-only unless they carry a @needs(...) marker
(app/auth.py), in which case employees holding one of those permissions
(app/permissions.py) may use them too. Reads of the vehicle lists are open to
anyone who can view products or fitment; everything that changes vehicle
types, makes, models or years stays admin-only.

Notes on how the data is shaped:
  * There is no "years" table. A year exists for a model when a row exists in
    `vehicles` (model_id, year). "Adding a year" means adding vehicles rows.
  * product_fitment links a product to a vehicles row (one machine, one year).
  * A product with is_universal = true fits everything and needs no
    product_fitment rows.
  * The public selector reads `fitment_catalog`. If that is a materialized view
    it is refreshed after every change here; if it is a plain view nothing is
    needed.
"""
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user, has_perm, needs, require_route_access
from app.database import get_db
from app.images import thumb_url
from app.permissions import FIELD_PERMS, label_for
from app.kits import KitPartIn, replace_parts
from app.skus import PLACEHOLDER_SKU, SKU_PREFIX, clean_ebay_item_id, shelf_code_for
from app.sanitize import clean_description

# No prefix here: main.py mounts this inside admin_router, which supplies
# "/admin" and the staff check. require_route_access then makes each route
# admin-only unless it is marked with @needs(...).
router = APIRouter(tags=["admin"], dependencies=[Depends(require_route_access)])

# Who may read the vehicle / category lists (the product and fitment screens need them).
READ_LISTS = ("products.view", "fitment.view")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def like(q: Optional[str]) -> Optional[str]:
    q = (q or "").strip()
    return f"%{q}%" if q else None


def finish(db: Session, refresh: bool = False) -> None:
    """Commit, refreshing fitment_catalog first when it is a materialized view."""
    if refresh:
        is_matview = db.execute(
            text(
                "select 1 from pg_matviews "
                "where schemaname = 'public' and matviewname = 'fitment_catalog'"
            )
        ).first()
        if is_matview:
            db.execute(text("refresh materialized view fitment_catalog"))
    db.commit()


def conflict(db: Session, detail: str):
    db.rollback()
    raise HTTPException(status_code=409, detail=detail)


# --------------------------------------------------------------------------
# vehicle types
# --------------------------------------------------------------------------
class NameIn(BaseModel):
    name: str
    slug: Optional[str] = None


@router.get("/vehicle-types")
@needs(*READ_LISTS)
def list_types(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select vt.id, vt.name, vt.slug, "
            "(select count(*) from vehicle_models vm where vm.vehicle_type_id = vt.id) as models "
            "from vehicle_types vt order by vt.name"
        )
    ).all()
    return [dict(r._mapping) for r in rows]


@router.post("/vehicle-types", status_code=201)
def create_type(body: NameIn, db: Session = Depends(get_db)):
    try:
        row = db.execute(
            text("insert into vehicle_types (name, slug) values (:n, :s) returning id, name, slug"),
            {"n": body.name.strip(), "s": body.slug or slugify(body.name)},
        ).first()
    except IntegrityError:
        conflict(db, "A type with that name or slug already exists")
    finish(db)
    return dict(row._mapping)


@router.patch("/vehicle-types/{type_id}")
def update_type(type_id: int, body: NameIn, db: Session = Depends(get_db)):
    try:
        row = db.execute(
            text(
                "update vehicle_types set name = :n, slug = coalesce(cast(:s as text), slug) "
                "where id = :id returning id, name, slug"
            ),
            # Slug only changes when sent explicitly; a rename keeps the old slug.
            {"n": body.name.strip(), "s": body.slug or None, "id": type_id},
        ).first()
    except IntegrityError:
        conflict(db, "A type with that name or slug already exists")
    if not row:
        raise HTTPException(404, "Type not found")
    finish(db, refresh=True)
    return dict(row._mapping)


@router.delete("/vehicle-types/{type_id}", status_code=204)
def delete_type(type_id: int, db: Session = Depends(get_db)):
    used = db.execute(
        text("select count(*) from vehicle_models where vehicle_type_id = :id"), {"id": type_id}
    ).scalar()
    if used:
        conflict(db, f"{used} models use this type; reassign them first")
    db.execute(text("delete from vehicle_types where id = :id"), {"id": type_id})
    finish(db)


# --------------------------------------------------------------------------
# makes
# --------------------------------------------------------------------------
@router.get("/makes")
@needs(*READ_LISTS)
def list_makes(q: Optional[str] = None, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select mk.id, mk.name, mk.slug, "
            "(select count(*) from vehicle_models vm where vm.make_id = mk.id) as models "
            "from vehicle_makes mk "
            "where cast(:like as text) is null or mk.name ilike cast(:like as text) "
            "order by mk.name"
        ),
        {"like": like(q)},
    ).all()
    return [dict(r._mapping) for r in rows]


@router.post("/makes", status_code=201)
def create_make(body: NameIn, db: Session = Depends(get_db)):
    try:
        row = db.execute(
            text("insert into vehicle_makes (name, slug) values (:n, :s) returning id, name, slug"),
            {"n": body.name.strip(), "s": body.slug or slugify(body.name)},
        ).first()
    except IntegrityError:
        conflict(db, "A make with that name or slug already exists")
    finish(db)
    return dict(row._mapping)


@router.patch("/makes/{make_id}")
def update_make(make_id: int, body: NameIn, db: Session = Depends(get_db)):
    try:
        row = db.execute(
            text(
                "update vehicle_makes set name = :n, slug = coalesce(cast(:s as text), slug) "
                "where id = :id returning id, name, slug"
            ),
            # Slug only changes when sent explicitly; a rename keeps the old slug.
            {"n": body.name.strip(), "s": body.slug or None, "id": make_id},
        ).first()
    except IntegrityError:
        conflict(db, "A make with that name or slug already exists")
    if not row:
        raise HTTPException(404, "Make not found")
    finish(db, refresh=True)
    return dict(row._mapping)


@router.delete("/makes/{make_id}", status_code=204)
def delete_make(make_id: int, db: Session = Depends(get_db)):
    used = db.execute(
        text("select count(*) from vehicle_models where make_id = :id"), {"id": make_id}
    ).scalar()
    if used:
        conflict(db, f"{used} models belong to this make; move or delete them first")
    db.execute(text("delete from vehicle_makes where id = :id"), {"id": make_id})
    finish(db)


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------
class ModelCreate(BaseModel):
    make_id: int
    vehicle_type_id: int  # required for new models
    name: str
    slug: Optional[str] = None
    year_from: Optional[int] = None
    year_to: Optional[int] = None


class ModelUpdate(BaseModel):
    make_id: Optional[int] = None
    vehicle_type_id: Optional[int] = None
    name: Optional[str] = None
    slug: Optional[str] = None


class MergeIn(BaseModel):
    into_id: int


def _insert_years(db: Session, model_id: int, year_from: int, year_to: int) -> int:
    if year_from > year_to:
        raise HTTPException(400, "year_from must be <= year_to")
    if year_to - year_from > 80:
        raise HTTPException(400, "Year range is too large")
    res = db.execute(
        text(
            "insert into vehicles (model_id, year) "
            "select :m, y from generate_series(cast(:a as int), cast(:b as int)) as y "
            "where not exists (select 1 from vehicles v where v.model_id = :m and v.year = y)"
        ),
        {"m": model_id, "a": year_from, "b": year_to},
    )
    return res.rowcount


@router.get("/models")
@needs(*READ_LISTS)
def list_models(
    make_id: Optional[int] = None,
    type_id: Optional[int] = None,
    untyped: bool = False,
    q: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    params = {
        "make_id": make_id,
        "type_id": type_id,
        "untyped": untyped,
        "like": like(q),
        "limit": limit,
        "offset": offset,
    }
    where = """
        where (cast(:make_id as bigint) is null or vm.make_id = cast(:make_id as bigint))
          and (cast(:type_id as bigint) is null or vm.vehicle_type_id = cast(:type_id as bigint))
          and (not :untyped or vm.vehicle_type_id is null)
          and (cast(:like as text) is null or vm.name ilike cast(:like as text))
    """
    total = db.execute(text(f"select count(*) from vehicle_models vm {where}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            select vm.id, vm.name, vm.slug, vm.make_id, mk.name as make,
                   vm.vehicle_type_id, vt.slug as vehicle_type,
                   (select min(v.year) from vehicles v where v.model_id = vm.id) as year_min,
                   (select max(v.year) from vehicles v where v.model_id = vm.id) as year_max,
                   (select count(distinct pf.product_id)
                      from product_fitment pf join vehicles v on v.id = pf.vehicle_id
                     where v.model_id = vm.id) as products
            from vehicle_models vm
            join vehicle_makes mk on mk.id = vm.make_id
            left join vehicle_types vt on vt.id = vm.vehicle_type_id
            {where}
            order by mk.name, vm.name
            limit :limit offset :offset
            """
        ),
        params,
    ).all()
    return {"total": total, "items": [dict(r._mapping) for r in rows]}


@router.post("/models", status_code=201)
def create_model(body: ModelCreate, db: Session = Depends(get_db)):
    try:
        row = db.execute(
            text(
                "insert into vehicle_models (make_id, vehicle_type_id, name, slug) "
                "values (:mk, :vt, :n, :s) returning id, name, slug, make_id, vehicle_type_id"
            ),
            {
                "mk": body.make_id,
                "vt": body.vehicle_type_id,
                "n": body.name.strip(),
                "s": body.slug or slugify(body.name),
            },
        ).first()
    except IntegrityError:
        conflict(db, "That model already exists for this make, or the make/type id is invalid")
    added = 0
    if body.year_from is not None and body.year_to is not None:
        added = _insert_years(db, row.id, body.year_from, body.year_to)
    finish(db, refresh=True)
    return {**dict(row._mapping), "years_added": added}


@router.patch("/models/{model_id}")
def update_model(model_id: int, body: ModelUpdate, db: Session = Depends(get_db)):
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"]:
        fields["name"] = fields["name"].strip()
        fields.setdefault("slug", slugify(fields["name"]))
    if not fields:
        raise HTTPException(400, "Nothing to update")
    sets = ", ".join(f"{k} = :{k}" for k in fields)  # keys come from the model, not the client
    try:
        row = db.execute(
            text(f"update vehicle_models set {sets} where id = :id returning id"),
            {**fields, "id": model_id},
        ).first()
    except IntegrityError:
        conflict(db, "That change collides with an existing model, or an id is invalid")
    if not row:
        raise HTTPException(404, "Model not found")
    finish(db, refresh=True)
    return {"id": model_id, "updated": list(fields)}


@router.post("/models/{model_id}/merge")
def merge_model(model_id: int, body: MergeIn, db: Session = Depends(get_db)):
    """Fold one model into another (e.g. an eBay-spelled duplicate)."""
    src, dst = model_id, body.into_id
    if src == dst:
        raise HTTPException(400, "Cannot merge a model into itself")
    found = db.execute(
        text("select count(*) from vehicle_models where id in (:a, :b)"), {"a": src, "b": dst}
    ).scalar()
    if found != 2:
        raise HTTPException(404, "Model not found")

    # Source vehicles whose (year, submodel) already exists on the target.
    dupes = db.execute(
        text(
            "select s.id as src_vehicle, t.id as dst_vehicle "
            "from vehicles s join vehicles t "
            "  on t.model_id = :dst and t.year = s.year "
            " and t.submodel is not distinct from s.submodel "
            "where s.model_id = :src"
        ),
        {"src": src, "dst": dst},
    ).all()

    for d in dupes:
        db.execute(
            text(
                "insert into product_fitment (product_id, vehicle_id, notes, source) "
                "select pf.product_id, :dv, pf.notes, pf.source from product_fitment pf "
                "where pf.vehicle_id = :sv "
                "and not exists (select 1 from product_fitment x "
                "                where x.product_id = pf.product_id and x.vehicle_id = :dv)"
            ),
            {"sv": d.src_vehicle, "dv": d.dst_vehicle},
        )
        db.execute(text("delete from product_fitment where vehicle_id = :sv"), {"sv": d.src_vehicle})
        db.execute(text("delete from vehicles where id = :sv"), {"sv": d.src_vehicle})

    moved = db.execute(
        text("update vehicles set model_id = :dst where model_id = :src"), {"src": src, "dst": dst}
    ).rowcount
    db.execute(text("delete from vehicle_models where id = :src"), {"src": src})
    finish(db, refresh=True)
    return {"merged_into": dst, "years_combined": len(dupes), "years_moved": moved}


@router.delete("/models/{model_id}", status_code=204)
def delete_model(model_id: int, db: Session = Depends(get_db)):
    used = db.execute(
        text(
            "select count(*) from product_fitment pf join vehicles v on v.id = pf.vehicle_id "
            "where v.model_id = :id"
        ),
        {"id": model_id},
    ).scalar()
    if used:
        conflict(db, f"{used} product fitment rows use this model; merge it into another model instead")
    db.execute(text("delete from vehicles where model_id = :id"), {"id": model_id})
    db.execute(text("delete from vehicle_models where id = :id"), {"id": model_id})
    finish(db, refresh=True)


# --------------------------------------------------------------------------
# years (per model)
# --------------------------------------------------------------------------
class YearRange(BaseModel):
    year_from: int
    year_to: int


@router.get("/years")
@needs(*READ_LISTS)
def list_years(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select year, count(distinct model_id) as models from vehicles "
            "group by year order by year desc"
        )
    ).all()
    return [dict(r._mapping) for r in rows]


@router.get("/models/{model_id}/years")
@needs(*READ_LISTS)
def model_years(model_id: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select v.id as vehicle_id, v.year, v.submodel, "
            "(select count(*) from product_fitment pf where pf.vehicle_id = v.id) as products "
            "from vehicles v where v.model_id = :id order by v.year desc"
        ),
        {"id": model_id},
    ).all()
    return [dict(r._mapping) for r in rows]


# --------------------------------------------------------------------------
# products on a model: see them, and move them to another model (consolidating a duplicate)
# --------------------------------------------------------------------------
_MODEL_PRODUCTS_WHERE = """
    from products p
    where exists (select 1 from product_fitment pf join vehicles v on v.id = pf.vehicle_id
                   where pf.product_id = p.id and v.model_id = :m)
      and (cast(:like as text) is null
           or p.title ilike cast(:like as text)
           or p.ebay_item_id ilike cast(:like as text)
           or p.sku ilike cast(:like as text)
           or p.manufacturer_part_number ilike cast(:like as text)
           or p.custom_label_sku ilike cast(:like as text))
"""


def _model_or_404(db: Session, model_id: int) -> None:
    if not db.execute(text("select 1 from vehicle_models where id = :i"), {"i": model_id}).first():
        raise HTTPException(404, "Model not found")


@router.get("/models/{model_id}/products")
@needs(*READ_LISTS)
def model_products(
    model_id: int,
    q: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Every product that fits this model, with the years it fits on this model."""
    _model_or_404(db, model_id)
    params = {"m": model_id, "like": like(q), "limit": limit, "offset": offset}
    total = db.execute(text(f"select count(*) {_MODEL_PRODUCTS_WHERE}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            select p.id, p.sku, p.custom_label_sku, p.ebay_item_id, p.title,
                   (select coalesce(array_agg(distinct v.year order by v.year), '{{}}')
                      from product_fitment pf join vehicles v on v.id = pf.vehicle_id
                     where pf.product_id = p.id and v.model_id = :m) as years,
                   (select count(distinct v.model_id)
                      from product_fitment pf join vehicles v on v.id = pf.vehicle_id
                     where pf.product_id = p.id and v.model_id <> :m) as other_models
            {_MODEL_PRODUCTS_WHERE}
            order by p.title, p.id
            limit :limit offset :offset
            """
        ),
        params,
    ).all()
    return {"total": total, "items": [dict(r._mapping) for r in rows]}


class MoveProducts(BaseModel):
    to_model_id: int
    product_ids: list[int] = Field(default_factory=list, max_length=2000)
    all_matching: bool = False  # every product on the model that matches `q` (or all of them when q is empty)
    q: Optional[str] = None


@router.post("/models/{model_id}/products/move")
def move_model_products(model_id: int, body: MoveProducts, db: Session = Depends(get_db)):
    """Move products from this model to another, year for year: a product that fit 2008 here now fits 2008 on the target.
    A year the target does not have yet is added to it. The products stop fitting this model, so once it is empty it can be
    deleted. Nothing else about the products changes. All or nothing: if anything fails, nothing is moved."""
    src, dst = model_id, body.to_model_id
    if src == dst:
        raise HTTPException(400, "Choose a different model to move them to")
    _model_or_404(db, src)
    _model_or_404(db, dst)
    if body.all_matching:
        ids = [
            r[0]
            for r in db.execute(
                text(f"select p.id {_MODEL_PRODUCTS_WHERE}"), {"m": src, "like": like(body.q)}
            ).all()
        ]
    else:
        ids = sorted({int(i) for i in body.product_ids})
    if not ids:
        raise HTTPException(400, "Choose at least one product to move")
    prm = {"src": src, "dst": dst, "ids": ids}
    try:
        on_src = db.execute(
            text(
                "select count(distinct pf.product_id) from product_fitment pf join vehicles sv on sv.id = pf.vehicle_id "
                "where sv.model_id = :src and pf.product_id = any(cast(:ids as bigint[]))"
            ),
            prm,
        ).scalar()
        # 1. years the products use here that the target does not have yet
        years_added = db.execute(
            text(
                "insert into vehicles (model_id, year, submodel) "
                "select distinct :dst, sv.year, sv.submodel "
                "from product_fitment pf join vehicles sv on sv.id = pf.vehicle_id "
                "where sv.model_id = :src and pf.product_id = any(cast(:ids as bigint[])) "
                "and not exists (select 1 from vehicles t where t.model_id = :dst and t.year = sv.year "
                "                and t.submodel is not distinct from sv.submodel)"
            ),
            prm,
        ).rowcount
        # 2. the same years on the target
        db.execute(
            text(
                """
                with src as (
                  select pf.product_id, pf.notes, pf.source,
                         (select min(t.id) from vehicles t
                           where t.model_id = :dst and t.year = sv.year and t.submodel is not distinct from sv.submodel) as tv
                  from product_fitment pf join vehicles sv on sv.id = pf.vehicle_id
                  where sv.model_id = :src and pf.product_id = any(cast(:ids as bigint[]))
                )
                insert into product_fitment (product_id, vehicle_id, notes, source)
                select distinct on (s.product_id, s.tv) s.product_id, s.tv, s.notes, s.source
                from src s
                where s.tv is not null
                  and not exists (select 1 from product_fitment x where x.product_id = s.product_id and x.vehicle_id = s.tv)
                """
            ),
            prm,
        )
        # 3. they no longer fit the source model
        removed = db.execute(
            text(
                "delete from product_fitment where product_id = any(cast(:ids as bigint[])) "
                "and vehicle_id in (select id from vehicles where model_id = :src)"
            ),
            prm,
        ).rowcount
        remaining = db.execute(
            text(
                "select count(distinct pf.product_id) from product_fitment pf join vehicles v on v.id = pf.vehicle_id "
                "where v.model_id = :src"
            ),
            prm,
        ).scalar()
    except IntegrityError:
        conflict(db, "Could not move them (a fitment row collides). Nothing was changed.")
    finish(db, refresh=True)
    return {"moved_products": int(on_src or 0), "years_added": int(years_added or 0), "fitment_rows": int(removed or 0), "remaining": int(remaining or 0)}


@router.post("/models/{model_id}/years", status_code=201)
def add_model_years(model_id: int, body: YearRange, db: Session = Depends(get_db)):
    added = _insert_years(db, model_id, body.year_from, body.year_to)
    finish(db, refresh=True)
    return {"years_added": added}


@router.delete("/models/{model_id}/years/{year}", status_code=204)
def delete_model_year(
    model_id: int, year: int, force: bool = False, db: Session = Depends(get_db)
):
    used = db.execute(
        text(
            "select count(*) from product_fitment pf join vehicles v on v.id = pf.vehicle_id "
            "where v.model_id = :m and v.year = :y"
        ),
        {"m": model_id, "y": year},
    ).scalar()
    if used and not force:
        conflict(db, f"{used} products fit this year; pass force=true to remove them too")
    db.execute(
        text(
            "delete from product_fitment where vehicle_id in "
            "(select id from vehicles where model_id = :m and year = :y)"
        ),
        {"m": model_id, "y": year},
    )
    db.execute(text("delete from vehicles where model_id = :m and year = :y"), {"m": model_id, "y": year})
    finish(db, refresh=True)


# --------------------------------------------------------------------------
# products
# --------------------------------------------------------------------------
class ProductFields(BaseModel):
    ebay_item_id: Optional[str] = None        # e.g. EBAY-178554199250; optional
    slug: Optional[str] = None
    title: Optional[str] = None
    custom_label_sku: Optional[str] = None    # the eBay custom label, e.g. 9144PSxx120558xx120216; optional
    manufacturer_part_number: Optional[str] = None
    show_part_number: Optional[bool] = None   # customers can only see the manufacturer part number when true
    category_id: Optional[int] = None
    part_manufacturer_id: Optional[int] = None
    part_type: Optional[str] = None
    bore_size_mm: Optional[float] = None
    shim_count: Optional[int] = None
    shim_od_mm: Optional[float] = None
    pressure_rating_bar: Optional[float] = None
    units_per_package: Optional[int] = None
    barcode: Optional[str] = None
    barcode_type: Optional[str] = None
    list_price: Optional[float] = None
    cost: Optional[float] = None
    quantity_on_hand: Optional[int] = None
    reorder_point: Optional[int] = None
    weight_oz: Optional[float] = None
    length_in: Optional[float] = None
    width_in: Optional[float] = None
    height_in: Optional[float] = None
    condition: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    is_published: Optional[bool] = None
    is_universal: Optional[bool] = None
    # A universal product shows in the Year/Make/Model search only while this is true.
    # When false it is found under its category (and for any model it has fitment rows for).
    universal_in_vehicle_search: Optional[bool] = None
    # "No vehicle fitment needed" (helmets, apparel, gear): never shown in a vehicle search,
    # and left out of the needs-fitment work list.
    fitment_exempt: Optional[bool] = None


class ProductCreate(ProductFields):
    # Pieces of a kit, picked from a vendor's catalog. When given, the custom label is built from them.
    kit_parts: Optional[list[KitPartIn]] = None
    kit_label_by: str = Field("mfr", pattern="^(mfr|sku)$")  # new label: Autodist parts as manufacturer # or vendor SKU


class ProductUpdate(ProductFields):
    # Our own EI- number. New products always get the next one from the database; an existing product's
    # number can be changed to another EI- number nobody uses yet (see _change_sku).
    sku: Optional[str] = None


# sku (our EI-0000001 number) is never typed in on a NEW product: the database hands out the next number.
PRODUCT_COLUMNS = ", ".join(f"p.{name}" for name in ProductFields.model_fields) + ", p.sku, p.id, p.updated_at"


def _unique_slug(db: Session, base: str, exclude_id: Optional[int] = None) -> str:
    slug, n = base, 1
    while db.execute(
        text("select 1 from products where slug = :s and (cast(:x as bigint) is null or id <> cast(:x as bigint))"),
        {"s": slug, "x": exclude_id},
    ).first():
        n += 1
        slug = f"{base}-{n}"
    return slug


def _clean_sku(raw: Optional[str]) -> str:
    """EI- followed by digits only. Returns it upper-cased, or raises a 400 saying what is wrong."""
    v = (raw or "").strip().upper()
    if not v:
        raise HTTPException(400, "The SKU can't be blank. It has to be EI- followed by a number, like EI-0000123.")
    if not re.fullmatch(re.escape(SKU_PREFIX) + r"\d{1,10}", v):
        raise HTTPException(400, "The SKU has to start with EI- followed by digits only, like EI-0000123.")
    if v == PLACEHOLDER_SKU or int(v[len(SKU_PREFIX):]) == 0:
        raise HTTPException(400, "EI-0000000 is the 'no number yet' placeholder and can't be used as a real SKU.")
    return v


def _change_sku(db: Session, product_id: int, old_sku: Optional[str], new_sku: str) -> dict:
    """
    Gives a product a different EI- number and carries it everywhere it is stored as text, in ONE transaction
    (the caller commits). Everything that points at the product by id (eBay listings, kit/PO lines, stock
    history, fitment, photos) follows on its own. What is copied as text and must be changed here:
      * the shelf barcode label code EIB-xxxxxxx (same digits as the SKU), so a reprinted label scans to the part
      * the SKU written on receiving-issue records for this part
    Refuses (nothing changed) if another product has the SKU or another part already owns the EIB- code.
    """
    used = db.execute(
        text("select id, title from products where upper(sku) = :s and id <> :p limit 1"),
        {"s": new_sku, "p": product_id},
    ).first()
    if used:
        conflict(db, f"{new_sku} is already used by another product ({used.title}). Pick a different number.")
    new_code = shelf_code_for(new_sku)
    owner = db.execute(
        text("select product_id from product_barcodes where upper(code) = upper(:c) and product_id <> :p limit 1"),
        {"c": new_code, "p": product_id},
    ).first()
    if owner:
        conflict(db, f"The shelf barcode {new_code} already belongs to another product. Pick a different number.")
    try:
        db.execute(
            text("update products set sku = :s, updated_at = now() where id = :p"), {"s": new_sku, "p": product_id}
        )
        moved = db.execute(
            text(
                "update product_barcodes set code = :c where id = ("
                "  select id from product_barcodes where product_id = :p and kind = 'shelf' and is_auto "
                "  and code like 'EIB-%' order by id limit 1)"
            ),
            {"c": new_code, "p": product_id},
        ).rowcount
        db.execute(
            text("update po_receiving_issues set sku = :s where product_id = :p"), {"s": new_sku, "p": product_id}
        )
    except IntegrityError:
        conflict(db, f"{new_sku} (or its shelf barcode {new_code}) is already in use. Pick a different number.")
    return {"sku": new_sku, "shelf_code": new_code, "barcode_changed": bool(moved), "old_sku": old_sku}


def _product_or_404(db: Session, product_id: int):
    row = db.execute(
        text(f"select {PRODUCT_COLUMNS} from products p where p.id = :id"), {"id": product_id}
    ).first()
    if not row:
        raise HTTPException(404, "Product not found")
    return row


# A new product starts from these values, so a form that always sends them
# (published = off, active = on, universal = off) is not treated as a change.
_NEW_PRODUCT_DEFAULTS = {
    "is_published": False,
    "is_active": True,
    "is_universal": False,
    "show_part_number": False,
    "universal_in_vehicle_search": True,
    "fitment_exempt": False,
}


def _same(old, new) -> bool:
    """True when saving `new` over `old` would not actually change anything."""
    if old is None or old == "":
        return new is None or new == ""
    if new is None or new == "":
        return False
    if isinstance(old, bool) or isinstance(new, bool):
        return old == new
    if isinstance(old, str) and isinstance(new, str):
        return old.strip() == new.strip()
    try:
        return float(old) == float(new)
    except (TypeError, ValueError):
        return str(old).strip() == str(new).strip()


def _require_field_perms(user: AuthUser, fields: dict, current, covered: tuple[str, ...] = ()) -> None:
    """
    Employees may only change the fields they were granted. Only fields whose
    value really changes count, so the edit form can send the whole product
    back without tripping over fields the employee never touched. `covered`
    lists permissions already implied by the action itself (creating a product
    covers filling in its details).
    """
    need: set[str] = set()
    for key, value in fields.items():
        if _same(current.get(key), value):
            continue
        need.add(FIELD_PERMS.get(key, "products.edit_details"))
    missing = [p for p in sorted(need) if p not in covered and not has_perm(user, p)]
    if missing:
        raise HTTPException(
            403, "You don't have permission to change: " + "; ".join(label_for(p) for p in missing)
        )


@router.get("/categories")
@needs(*READ_LISTS)
def list_categories(db: Session = Depends(get_db)):
    rows = db.execute(
        text("select id, parent_id, name, sort_order from categories order by sort_order, name")
    ).all()
    return [dict(r._mapping) for r in rows]


# Columns the products list can be sorted by (whitelisted: the client never sends SQL).
_PRODUCT_SORTS = {
    "sku": "p.sku",
    "custom_label_sku": "p.custom_label_sku",
    "title": "lower(p.title)",
    "list_price": "p.list_price",
    "quantity_on_hand": "p.quantity_on_hand",
    "fitment_count": "(select count(*) from product_fitment pf2 where pf2.product_id = p.id)",
    "is_published": "p.is_published",
}


# What each Fitment filter means. The client sends only a key; the SQL is chosen here.
_HAS_FITMENT = "exists (select 1 from product_fitment pf where pf.product_id = p.id)"
FITMENT_FILTERS = {
    # the work list: needs vehicle fitment and has none yet
    "needs_fitment": f"(not coalesce(p.is_universal, false) and not p.fitment_exempt and not {_HAS_FITMENT})",
    "has_fitment": _HAS_FITMENT,
    "universal": "coalesce(p.is_universal, false)",
    "universal_search": "(coalesce(p.is_universal, false) and p.universal_in_vehicle_search)",
    "universal_category": "(coalesce(p.is_universal, false) and not p.universal_in_vehicle_search)",
    "not_vehicle": "p.fitment_exempt",
}

# The one status shown for each product (a product with fitment rows AND a universal setting shows the setting).
_STATUS_SQL = (
    "case when p.fitment_exempt then 'not_vehicle'"
    " when coalesce(p.is_universal, false) and p.universal_in_vehicle_search then 'universal_search'"
    " when coalesce(p.is_universal, false) then 'universal_category'"
    f" when {_HAS_FITMENT} then 'has_fitment'"
    " else 'needs_fitment' end"
)

# The four ways a product can be matched to vehicles: (is_universal, universal_in_vehicle_search, fitment_exempt).
FITMENT_MODES = {
    "vehicle": (False, True, False),
    "universal_search": (True, True, False),
    "universal_category": (True, False, False),
    "not_vehicle": (False, True, True),
}
FitmentMode = Literal["vehicle", "universal_search", "universal_category", "not_vehicle"]

# A product's category plus everything under it, so choosing "Apparel" also finds "Apparel > Jackets".
_CATEGORY_TREE = (
    "with recursive tree as ("
    " select id from categories where id = cast(:category_id as bigint)"
    " union all"
    " select c.id from categories c join tree t on c.parent_id = t.id) "
)


def _product_where(
    q: Optional[str] = None,
    category_id: Optional[int] = None,
    published: Optional[bool] = None,
    universal: Optional[bool] = None,
    no_fitment: bool = False,
    fitment: Optional[str] = None,
    source: Optional[str] = None,
) -> tuple[str, dict]:
    """The WHERE clause and parameters shared by the products list, the fitment summary and the bulk change."""
    if fitment and fitment not in FITMENT_FILTERS:
        raise HTTPException(400, "Unknown fitment filter.")
    if source and source != "autodist":
        raise HTTPException(400, "Unknown source.")
    params = {
        "like": like(q),
        "category_id": category_id,
        "published": published,
        "universal": universal,
        "no_fitment": no_fitment,
        "supplier": "autodist",
    }
    where = """
        where (cast(:like as text) is null
               or p.title ilike cast(:like as text)
               or p.ebay_item_id ilike cast(:like as text)
               or p.sku ilike cast(:like as text)
               or p.manufacturer_part_number ilike cast(:like as text)
               or p.custom_label_sku ilike cast(:like as text))
          and (cast(:category_id as bigint) is null or p.category_id in (select id from tree))
          and (cast(:published as boolean) is null or p.is_published = cast(:published as boolean))
          and (cast(:universal as boolean) is null or p.is_universal = cast(:universal as boolean))
          and (not :no_fitment or %s)
    """ % FITMENT_FILTERS["needs_fitment"]
    if fitment:
        where += f" and {FITMENT_FILTERS[fitment]}"
    if source == "autodist":
        where += " and exists (select 1 from supplier_product_links l where l.product_id = p.id and l.supplier = :supplier)"
    return where, params


@router.get("/products")
@needs(*READ_LISTS)
def list_products(
    q: Optional[str] = None,
    category_id: Optional[int] = None,
    published: Optional[bool] = None,
    universal: Optional[bool] = None,
    no_fitment: bool = False,
    fitment: Optional[str] = None,
    source: Optional[str] = None,
    sort: Optional[str] = None,
    dir: str = "asc",
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    sort_expr = _PRODUCT_SORTS.get(sort or "")
    if sort and not sort_expr:
        raise HTTPException(400, "Unknown sort column.")
    direction = "desc" if dir == "desc" else "asc"
    order_by = f"{sort_expr} {direction} nulls last, p.id desc" if sort_expr else "p.id desc"
    where, params = _product_where(q, category_id, published, universal, no_fitment, fitment, source)
    params.update({"limit": limit, "offset": offset})
    total = db.execute(text(f"{_CATEGORY_TREE} select count(*) from products p {where}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            {_CATEGORY_TREE}
            select p.id, p.sku as sku, p.custom_label_sku, p.ebay_item_id, p.title, p.slug, p.list_price, p.quantity_on_hand,
                   p.category_id, p.is_published, p.is_active, p.is_universal,
                   p.universal_in_vehicle_search, p.fitment_exempt,
                   (select count(*) from product_fitment pf where pf.product_id = p.id) as fitment_count,
                   {_STATUS_SQL} as fitment_status
            from products p {where}
            order by {order_by} limit :limit offset :offset
            """
        ),
        params,
    ).all()
    return {"total": total, "items": [dict(r._mapping) for r in rows]}


@router.get("/products/fitment-summary")
@needs(*READ_LISTS)
def fitment_summary(
    q: Optional[str] = None,
    category_id: Optional[int] = None,
    published: Optional[bool] = None,
    universal: Optional[bool] = None,
    source: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """How many products are in each fitment status, for the current search and category (the status filter itself is not applied)."""
    where, params = _product_where(q, category_id, published, universal, False, None, source)
    row = db.execute(
        text(
            f"""
            {_CATEGORY_TREE}
            select count(*) as total,
                   count(*) filter (where {FITMENT_FILTERS['needs_fitment']}) as needs_fitment,
                   count(*) filter (where {_STATUS_SQL} = 'has_fitment') as has_fitment,
                   count(*) filter (where {_STATUS_SQL} = 'universal_search') as universal_search,
                   count(*) filter (where {_STATUS_SQL} = 'universal_category') as universal_category,
                   count(*) filter (where {_STATUS_SQL} = 'not_vehicle') as not_vehicle
            from products p {where}
            """
        ),
        params,
    ).one()
    return {k: int(v or 0) for k, v in row._mapping.items()}


class FitmentModeIn(BaseModel):
    mode: FitmentMode
    ids: Optional[list[int]] = Field(None, max_length=1000)  # these products ...
    # ... or every product the list is showing (same filters as the list). expected_count is the number
    # the screen showed, so a list that changed in the meantime is refused instead of changing the wrong products.
    q: Optional[str] = None
    category_id: Optional[int] = None
    published: Optional[bool] = None
    universal: Optional[bool] = None
    fitment: Optional[str] = None
    source: Optional[str] = None
    expected_count: Optional[int] = None


@router.post("/products/fitment-mode")
@needs("fitment.add")
def set_fitment_mode(body: FitmentModeIn, db: Session = Depends(get_db)):
    """Choose how many products match vehicles at once: vehicle-specific, universal (in vehicle search or category only), or not vehicle-based."""
    uni, in_search, exempt = FITMENT_MODES[body.mode]
    sets = (
        "is_universal = :uni, universal_in_vehicle_search = :in_search, fitment_exempt = :exempt, updated_at = now()"
    )
    values = {"uni": uni, "in_search": in_search, "exempt": exempt}
    if body.ids is not None:
        ids = sorted(set(body.ids))
        if not ids:
            raise HTTPException(400, "No products selected.")
        n = db.execute(text(f"update products set {sets} where id = any(:ids)"), {**values, "ids": ids}).rowcount
    else:
        has_filter = any(
            v not in (None, "") for v in (body.q, body.category_id, body.published, body.universal, body.fitment, body.source)
        )
        if not has_filter:
            raise HTTPException(400, "Pick a category, search or filter first. This will not change the whole catalog at once.")
        where, params = _product_where(body.q, body.category_id, body.published, body.universal, False, body.fitment, body.source)
        matched = db.execute(text(f"{_CATEGORY_TREE} select count(*) from products p {where}"), params).scalar() or 0
        if body.expected_count is None or matched != body.expected_count:
            conflict(db, f"The list now has {matched} products, not {body.expected_count}. Nothing was changed. Reload the list and try again.")
        n = db.execute(
            text(f"{_CATEGORY_TREE} update products set {sets} where id in (select p.id from products p {where})"),
            {**params, **values},
        ).rowcount
    finish(db, refresh=True)
    return {"updated": n, "mode": body.mode}


@router.get("/products/{product_id}")
@needs(*READ_LISTS)
def get_product(product_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    row = _product_or_404(db, product_id)
    photos = db.execute(
        text(
            "select id, source_url, storage_path, image_key, is_primary, hidden, sort_order from product_photos "
            "where product_id = :id order by is_primary desc, sort_order, id"
        ),
        {"id": product_id},
    ).all()
    out = []
    for p in photos:
        d = dict(p._mapping)
        d["thumb"] = thumb_url(d["image_key"]) or d["source_url"]
        out.append(d)
    data = {**dict(row._mapping), "photos": out}
    if not has_perm(user, "products.edit_price"):
        data.pop("cost", None)  # what the shop paid is only shown to people who can change prices
    return data


@router.post("/products", status_code=201)
@needs("products.create")
def create_product(body: ProductCreate, db: Session = Depends(get_db), user: AuthUser = Depends(current_user)):
    fields = body.model_dump(exclude_unset=True)
    kit_parts = body.kit_parts or []
    fields.pop("kit_parts", None)
    label_by = fields.pop("kit_label_by", "mfr")
    if not fields.get("title"):
        raise HTTPException(400, "A title is required")
    if "ebay_item_id" in fields:
        fields["ebay_item_id"] = clean_ebay_item_id(fields["ebay_item_id"])
    _require_field_perms(user, fields, _NEW_PRODUCT_DEFAULTS, covered=("products.edit_details",))
    if "description" in fields:
        fields["description"] = clean_description(fields["description"])
    fields["slug"] = _unique_slug(db, slugify(fields.get("slug") or fields["title"]))
    cols = ", ".join(fields)  # keys come from the model, not the client
    vals = ", ".join(f":{k}" for k in fields)
    try:
        new_id = db.execute(
            text(f"insert into products ({cols}) values ({vals}) returning id"), fields
        ).scalar()
    except IntegrityError:
        conflict(db, "A product with that eBay item ID already exists")
    cls = None
    if kit_parts:
        if not has_perm(user, "products.edit_details") and not has_perm(user, "products.create"):
            conflict(db, "You don't have permission to set kit parts.")
        cls = replace_parts(db, new_id, kit_parts, label_by)["cls"]  # builds and stores the custom label
    finish(db, refresh=True)
    return {"id": new_id, "slug": fields["slug"], "custom_label_sku": cls}


@router.patch("/products/{product_id}")
@needs("products.edit_details", "products.edit_price", "products.publish", "inventory.adjust", "fitment.add")
def update_product(
    product_id: int,
    body: ProductUpdate,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(current_user),
):
    current = _product_or_404(db, product_id)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Nothing to update")
    _require_field_perms(user, fields, current._mapping)
    # Clean only a description that really changed. This runs after the
    # permission check, so an employee who may not edit details can still save
    # the form with the description untouched.
    if "description" in fields and not _same(current._mapping.get("description"), fields["description"]):
        fields["description"] = clean_description(fields["description"])
    if "title" in fields and not fields["title"]:
        raise HTTPException(400, "title cannot be empty")
    if "ebay_item_id" in fields:
        fields["ebay_item_id"] = clean_ebay_item_id(fields["ebay_item_id"])
    if "slug" in fields:
        fields["slug"] = _unique_slug(db, slugify(fields["slug"] or ""), exclude_id=product_id)
    new_sku = None
    if "sku" in fields:
        wanted = _clean_sku(fields.pop("sku"))
        if wanted != (current._mapping.get("sku") or "").strip().upper():
            new_sku = wanted
    if fields:
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        try:
            db.execute(
                text(f"update products set {sets}, updated_at = now() where id = :id"),
                {**fields, "id": product_id},
            )
        except IntegrityError:
            conflict(db, "That eBay item ID or slug is already used by another product")
    sku_result = _change_sku(db, product_id, current._mapping.get("sku"), new_sku) if new_sku else None
    finish(db, refresh=True)
    # A changed custom label must reach every linked eBay listing: the screen calls POST /kits/{id}/sync next.
    out = {"id": product_id, "updated": list(fields) + (["sku"] if sku_result else []), "cls_changed": "custom_label_sku" in fields}
    if sku_result:
        out.update(sku_result)
    return out


# --------------------------------------------------------------------------
# product <-> fitment
# --------------------------------------------------------------------------
class FitmentAdd(BaseModel):
    model_id: int
    all_years: bool = False          # every year the model has
    year_from: Optional[int] = None  # a range ...
    year_to: Optional[int] = None
    years: Optional[list[int]] = None  # ... and/or individually ticked years
    notes: Optional[str] = None


class FitmentRemove(BaseModel):
    vehicle_ids: Optional[list[int]] = None
    model_id: Optional[int] = None   # remove this product from the whole model


@router.get("/products/{product_id}/fitment")
@needs(*READ_LISTS)
def product_fitment(product_id: int, db: Session = Depends(get_db)):
    prod = _product_or_404(db, product_id)
    rows = db.execute(
        text(
            """
            select vm.id as model_id, mk.name as make, vm.name as model,
                   vt.slug as vehicle_type, v.id as vehicle_id, v.year, v.submodel, pf.notes
            from product_fitment pf
            join vehicles v on v.id = pf.vehicle_id
            join vehicle_models vm on vm.id = v.model_id
            join vehicle_makes mk on mk.id = vm.make_id
            left join vehicle_types vt on vt.id = vm.vehicle_type_id
            where pf.product_id = :id
            order by mk.name, vm.name, v.year
            """
        ),
        {"id": product_id},
    ).all()
    models: dict[int, dict] = {}
    for r in rows:
        m = models.setdefault(
            r.model_id,
            {
                "model_id": r.model_id,
                "make": r.make,
                "model": r.model,
                "vehicle_type": r.vehicle_type,
                "years": [],
            },
        )
        m["years"].append({"vehicle_id": r.vehicle_id, "year": r.year, "submodel": r.submodel})
    return {"is_universal": prod.is_universal, "models": list(models.values())}


@router.post("/products/{product_id}/fitment", status_code=201)
@needs("fitment.add")
def add_product_fitment(product_id: int, body: FitmentAdd, db: Session = Depends(get_db)):
    _product_or_404(db, product_id)
    available = db.execute(
        text("select id, year from vehicles where model_id = :m"), {"m": body.model_id}
    ).all()
    if not available:
        raise HTTPException(404, "That model has no years yet; add years to the model first")
    available_years = {v.year for v in available}

    if body.all_years:
        wanted = set(available_years)
    else:
        wanted = set(body.years or [])
        if body.year_from is not None and body.year_to is not None:
            if body.year_from > body.year_to:
                raise HTTPException(400, "year_from must be <= year_to")
            wanted |= set(range(body.year_from, body.year_to + 1))
    if not wanted:
        raise HTTPException(400, "Choose all years, a year range, or at least one year")

    vehicle_ids = [v.id for v in available if v.year in wanted]
    skipped = sorted(wanted - available_years)
    added = 0
    if vehicle_ids:
        added = db.execute(
            text(
                "insert into product_fitment (product_id, vehicle_id, notes, source) "
                "select :pid, v.id, :notes, 'admin' from vehicles v "
                "where v.id = any(cast(:vids as bigint[])) "
                "and not exists (select 1 from product_fitment pf "
                "                where pf.product_id = :pid and pf.vehicle_id = v.id)"
            ),
            {"pid": product_id, "vids": vehicle_ids, "notes": body.notes},
        ).rowcount
    finish(db, refresh=True)
    return {"added": added, "already_present": len(vehicle_ids) - added, "skipped_years": skipped}


@router.post("/products/{product_id}/fitment/remove")
@needs("fitment.remove")
def remove_product_fitment(product_id: int, body: FitmentRemove, db: Session = Depends(get_db)):
    _product_or_404(db, product_id)
    if body.vehicle_ids:
        res = db.execute(
            text(
                "delete from product_fitment where product_id = :pid "
                "and vehicle_id = any(cast(:vids as bigint[]))"
            ),
            {"pid": product_id, "vids": body.vehicle_ids},
        )
    elif body.model_id:
        res = db.execute(
            text(
                "delete from product_fitment where product_id = :pid and vehicle_id in "
                "(select id from vehicles where model_id = :m)"
            ),
            {"pid": product_id, "m": body.model_id},
        )
    else:
        raise HTTPException(400, "Provide vehicle_ids or model_id")
    finish(db, refresh=True)
    return {"removed": res.rowcount}
