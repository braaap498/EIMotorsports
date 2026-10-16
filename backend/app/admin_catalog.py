"""
Admin back-office: vehicle fitment (types, makes, models, years) and products.
Mounted at /admin/* via admin_router in main.py. Every route requires an
owner/admin token (app/auth.py).

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
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db

# No prefix here: main.py mounts this inside admin_router, which supplies
# "/admin" and the staff check. require_admin below is the stricter
# owner/admin check for everything in this file.
router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


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
                "update vehicle_types set name = :n, slug = :s where id = :id "
                "returning id, name, slug"
            ),
            {"n": body.name.strip(), "s": body.slug or slugify(body.name), "id": type_id},
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
                "update vehicle_makes set name = :n, slug = :s where id = :id "
                "returning id, name, slug"
            ),
            {"n": body.name.strip(), "s": body.slug or slugify(body.name), "id": make_id},
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
def list_years(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select year, count(distinct model_id) as models from vehicles "
            "group by year order by year desc"
        )
    ).all()
    return [dict(r._mapping) for r in rows]


@router.get("/models/{model_id}/years")
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
    sku: Optional[str] = None
    slug: Optional[str] = None
    title: Optional[str] = None
    internal_part_number: Optional[str] = None
    manufacturer_part_number: Optional[str] = None
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


PRODUCT_COLUMNS = ", ".join(f"p.{name}" for name in ProductFields.model_fields) + ", p.id, p.updated_at"


def _unique_slug(db: Session, base: str, exclude_id: Optional[int] = None) -> str:
    slug, n = base, 1
    while db.execute(
        text("select 1 from products where slug = :s and (cast(:x as bigint) is null or id <> cast(:x as bigint))"),
        {"s": slug, "x": exclude_id},
    ).first():
        n += 1
        slug = f"{base}-{n}"
    return slug


def _product_or_404(db: Session, product_id: int):
    row = db.execute(
        text(f"select {PRODUCT_COLUMNS} from products p where p.id = :id"), {"id": product_id}
    ).first()
    if not row:
        raise HTTPException(404, "Product not found")
    return row


@router.get("/products")
def list_products(
    q: Optional[str] = None,
    category_id: Optional[int] = None,
    published: Optional[bool] = None,
    universal: Optional[bool] = None,
    no_fitment: bool = False,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    params = {
        "like": like(q),
        "category_id": category_id,
        "published": published,
        "universal": universal,
        "no_fitment": no_fitment,
        "limit": limit,
        "offset": offset,
    }
    where = """
        where (cast(:like as text) is null
               or p.title ilike cast(:like as text)
               or p.sku ilike cast(:like as text)
               or p.manufacturer_part_number ilike cast(:like as text)
               or p.internal_part_number ilike cast(:like as text))
          and (cast(:category_id as bigint) is null or p.category_id = cast(:category_id as bigint))
          and (cast(:published as boolean) is null or p.is_published = cast(:published as boolean))
          and (cast(:universal as boolean) is null or p.is_universal = cast(:universal as boolean))
          and (not :no_fitment or (not p.is_universal and not exists
                 (select 1 from product_fitment pf where pf.product_id = p.id)))
    """
    total = db.execute(text(f"select count(*) from products p {where}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            select p.id, p.sku, p.title, p.slug, p.list_price, p.quantity_on_hand,
                   p.category_id, p.is_published, p.is_active, p.is_universal,
                   (select count(*) from product_fitment pf where pf.product_id = p.id) as fitment_count
            from products p {where}
            order by p.id desc limit :limit offset :offset
            """
        ),
        params,
    ).all()
    return {"total": total, "items": [dict(r._mapping) for r in rows]}


@router.get("/products/{product_id}")
def get_product(product_id: int, db: Session = Depends(get_db)):
    row = _product_or_404(db, product_id)
    photos = db.execute(
        text(
            "select id, source_url, storage_path, is_primary, sort_order from product_photos "
            "where product_id = :id order by is_primary desc, sort_order, id"
        ),
        {"id": product_id},
    ).all()
    return {**dict(row._mapping), "photos": [dict(p._mapping) for p in photos]}


@router.post("/products", status_code=201)
def create_product(body: ProductFields, db: Session = Depends(get_db)):
    fields = body.model_dump(exclude_unset=True)
    if not fields.get("sku") or not fields.get("title"):
        raise HTTPException(400, "sku and title are required")
    fields["slug"] = _unique_slug(db, slugify(fields.get("slug") or fields["title"]))
    cols = ", ".join(fields)  # keys come from the model, not the client
    vals = ", ".join(f":{k}" for k in fields)
    try:
        new_id = db.execute(
            text(f"insert into products ({cols}) values ({vals}) returning id"), fields
        ).scalar()
    except IntegrityError:
        conflict(db, "A product with that SKU already exists")
    finish(db, refresh=True)
    return {"id": new_id, "slug": fields["slug"]}


@router.patch("/products/{product_id}")
def update_product(product_id: int, body: ProductFields, db: Session = Depends(get_db)):
    _product_or_404(db, product_id)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Nothing to update")
    for required in ("sku", "title"):
        if required in fields and not fields[required]:
            raise HTTPException(400, f"{required} cannot be empty")
    if "slug" in fields:
        fields["slug"] = _unique_slug(db, slugify(fields["slug"] or ""), exclude_id=product_id)
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    try:
        db.execute(
            text(f"update products set {sets}, updated_at = now() where id = :id"),
            {**fields, "id": product_id},
        )
    except IntegrityError:
        conflict(db, "That SKU or slug is already used by another product")
    finish(db, refresh=True)
    return {"id": product_id, "updated": list(fields)}


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
