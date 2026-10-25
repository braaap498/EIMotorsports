"""
Public storefront fitment lookups (Year > Make > Model > Parts).
Read-only, no login required. Mounted at /store/fitment/*.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.images import card_image, gallery
from app.sanitize import clean_description
from app.stock import available_sql

router = APIRouter(prefix="/store/fitment", tags=["store"])

# Only published products show in the store.
PUBLISHED_FILTER = "p.is_published = true"

# Machine types (vehicle_types.slug). An unknown ?type= value is ignored,
# which means "all types", so a bad link never gives an empty store.
VALID_TYPES = {"snowmobile", "atv", "utv", "dirt-bike", "motorcycle", "pwc"}


def clean_type(value: Optional[str]) -> Optional[str]:
    return value if value in VALID_TYPES else None


@router.get("/vehicle-types")
def vehicle_types(db: Session = Depends(get_db)):
    rows = db.execute(
        text("select id, name, slug from vehicle_types order by name")
    ).all()
    return [{"id": r.id, "name": r.name, "slug": r.slug} for r in rows]


@router.get("/years")
def years(db: Session = Depends(get_db)):
    rows = db.execute(
        text("select distinct year from fitment_catalog order by year desc")
    ).all()
    return [r.year for r in rows]


@router.get("/makes")
def makes(
    year: int,
    type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    # Left joins so a model with no type yet still shows when no type is chosen.
    rows = db.execute(
        text(
            "select distinct fc.make_id, fc.make "
            "from fitment_catalog fc "
            "left join vehicle_models vm on vm.id = fc.model_id "
            "left join vehicle_types vt on vt.id = vm.vehicle_type_id "
            "where fc.year = :year "
            "and (cast(:type as text) is null or vt.slug = cast(:type as text)) "
            "order by fc.make"
        ),
        {"year": year, "type": clean_type(type)},
    ).all()
    return [{"id": r.make_id, "name": r.make} for r in rows]


@router.get("/models")
def models(
    year: int,
    make_id: int,
    type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text(
            "select fc.model_id, fc.model, fc.products, vt.slug as vehicle_type "
            "from fitment_catalog fc "
            "left join vehicle_models vm on vm.id = fc.model_id "
            "left join vehicle_types vt on vt.id = vm.vehicle_type_id "
            "where fc.year = :year and fc.make_id = :make_id "
            "and (cast(:type as text) is null or vt.slug = cast(:type as text)) "
            "order by fc.model"
        ),
        {"year": year, "make_id": make_id, "type": clean_type(type)},
    ).all()
    return [
        {
            "id": r.model_id,
            "name": r.model,
            "products": r.products,
            "vehicle_type": r.vehicle_type,
        }
        for r in rows
    ]


@router.get("/products")
def products(
    year: int,
    model_id: int,
    limit: int = Query(24, le=100),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    # Universal products fit every machine, so they are always included.
    base = f"""
        from products p
        where {PUBLISHED_FILTER}
          and (
            p.is_universal
            or exists (
              select 1 from product_fitment pf
              join vehicles v on v.id = pf.vehicle_id
              where pf.product_id = p.id
                and v.model_id = :model_id and v.year = :year
            )
          )
    """
    params = {"model_id": model_id, "year": year}
    total = db.execute(text(f"select count(*) {base}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            select p.id, p.title, p.slug, p.list_price, p.is_universal,
              (select ph.source_url from product_photos ph
                where ph.product_id = p.id
                order by ph.is_primary desc, ph.sort_order, ph.id limit 1) as photo_source_url,
              (select ph.image_key from product_photos ph
                where ph.product_id = p.id
                order by ph.is_primary desc, ph.sort_order, ph.id limit 1) as photo_image_key
            {base}
            order by p.is_universal, p.title limit :limit offset :offset
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


@router.get("/product/{slug}")
def product_detail(slug: str, db: Session = Depends(get_db)):
    p = db.execute(
        text(
            "select id, sku, title, slug, list_price, condition, description, "
            "manufacturer_part_number, on_order, "
            f"{available_sql('p')} as available_qty from products p "
            f"where p.slug = :slug and {PUBLISHED_FILTER}"
        ),
        {"slug": slug},
    ).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    photos = db.execute(
        text(
            "select source_url, image_key from product_photos "
            "where product_id = :id order by is_primary desc, sort_order, id"
        ),
        {"id": p.id},
    ).all()
    specifics = db.execute(
        text(
            "select name, value from product_item_specifics "
            "where product_id = :id order by sort_order, id"
        ),
        {"id": p.id},
    ).all()
    return {
        "id": p.id,
        "sku": p.sku,
        "title": p.title,
        "slug": p.slug,
        "price": f"{p.list_price:.2f}" if p.list_price is not None else None,
        "condition": p.condition,
        "part_number": p.manufacturer_part_number,
        "available": max(0, int(p.available_qty or 0)),
        "backordered": bool(p.on_order) and int(p.available_qty or 0) <= 0,
        "description": clean_description(p.description),
        "photos": gallery(photos),
        "specifics": [{"name": s.name, "value": s.value} for s in specifics],
    }
