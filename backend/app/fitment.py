"""
Public storefront fitment lookups (Year > Make > Model > Parts).
Read-only, no login required. Mounted at /store/fitment/*.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.fitment_sub import family_sql
from app.images import card_image, gallery
from app.sanitize import clean_description
from app.availability import stock_mode_sql, web_effective_sql
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
    year: Optional[int] = None,
    type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    # No year = makes from every year. Left joins so a model with no type yet
    # still shows when no type is chosen.
    rows = db.execute(
        text(
            "select distinct fc.make_id, fc.make "
            "from fitment_catalog fc "
            "left join vehicle_models vm on vm.id = fc.model_id "
            "left join vehicle_types vt on vt.id = vm.vehicle_type_id "
            "where (cast(:year as int) is null or fc.year = cast(:year as int)) "
            "and (cast(:type as text) is null or vt.slug = cast(:type as text)) "
            "order by fc.make"
        ),
        {"year": year, "type": clean_type(type)},
    ).all()
    return [{"id": r.make_id, "name": r.make} for r in rows]


@router.get("/models")
def models(
    make_id: int,
    year: Optional[int] = None,
    type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    # With no year, fitment_catalog has one row per year for each model, so group
    # to one row per model. "products" is then the best single year's count
    # (summing would double count); the selector doesn't display it.
    # Submodels are models of their own (vehicle_models.parent_model_id). A model that only has fitment through its submodels is
    # still listed, so choosing it shows the parts of all its submodels (see /products).
    rows = db.execute(
        text(
            "select q.model_id, q.model, max(q.products) as products, max(q.vehicle_type) as vehicle_type from ("
            " select fc.model_id, fc.model, fc.products, vt.slug as vehicle_type"
            " from fitment_catalog fc"
            " left join vehicle_models vm on vm.id = fc.model_id"
            " left join vehicle_types vt on vt.id = vm.vehicle_type_id"
            " where fc.make_id = :make_id"
            " and (cast(:year as int) is null or fc.year = cast(:year as int))"
            " and (cast(:type as text) is null or vt.slug = cast(:type as text))"
            " union all"
            " select vp.id, vp.name, fc.products, vt.slug"
            " from fitment_catalog fc"
            " join vehicle_models vc on vc.id = fc.model_id"
            " join vehicle_models vp on vp.id = vc.parent_model_id"
            " left join vehicle_types vt on vt.id = vp.vehicle_type_id"
            " where vp.make_id = :make_id"
            " and (cast(:year as int) is null or fc.year = cast(:year as int))"
            " and (cast(:type as text) is null or vt.slug = cast(:type as text))"
            ") q group by q.model_id, q.model order by q.model"
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
    model_id: int,
    year: Optional[int] = None,
    limit: int = Query(24, le=100),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    # A universal product shows for every machine only when the admin left
    # "show in vehicle search" on. With it off, the product still appears under
    # its category and still shows for any model it has specific fitment rows
    # for (oil that is universal but also listed for particular machines).
    # No year = every year of this model; exists() keeps a part that fits
    # several years from showing up more than once.
    base = f"""
        from products p
        where {PUBLISHED_FILTER}
          and not p.fitment_exempt
          and (
            (p.is_universal and p.universal_in_vehicle_search)
            or exists (
              select 1 from product_fitment pf
              join vehicles v on v.id = pf.vehicle_id
              where pf.product_id = p.id
                and v.model_id in {family_sql()}
                and (cast(:year as int) is null or v.year = cast(:year as int))
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
                where ph.product_id = p.id and not ph.hidden
                order by ph.is_primary desc, ph.sort_order, ph.id limit 1) as photo_source_url,
              (select ph.image_key from product_photos ph
                where ph.product_id = p.id and not ph.hidden
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
            "select id, sku_display as sku, title, slug, list_price, condition, description, category_id, updated_at, "
            "manufacturer_part_number, show_part_number, on_order, expected_date, "
            f"(expected_date - current_date) as days_until, "
            f"{web_effective_sql('p')} as available_qty, {stock_mode_sql('p')} as stock_mode from products p "
            f"where p.slug = :slug and {PUBLISHED_FILTER}"
        ),
        {"slug": slug},
    ).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    photos = db.execute(
        text(
            "select source_url, image_key from product_photos "
            "where product_id = :id and not hidden order by is_primary desc, sort_order, id"
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
    category = None
    if p.category_id:
        c = db.execute(
            text("select name, slug from categories where id = :c"), {"c": p.category_id}
        ).first()
        if c:
            category = {"name": c.name, "slug": c.slug}

    # Which machines this part fits, one entry per model with its years. A submodel is shown under its own name
    # but links (model_slug) to the page of the model it hangs under, which is the page that exists.
    fit_rows = db.execute(
        text(
            "select mk.name as make, mk.slug as make_slug, vm.name as model, "
            "coalesce(pm.slug, vm.slug) as model_slug, "
            "array_agg(distinct v.year order by v.year) filter (where v.year is not null) as years "
            "from product_fitment pf "
            "join vehicles v on v.id = pf.vehicle_id "
            "join vehicle_models vm on vm.id = v.model_id "
            "left join vehicle_models pm on pm.id = vm.parent_model_id "
            "join vehicle_makes mk on mk.id = vm.make_id "
            "where pf.product_id = :id "
            "group by mk.name, mk.slug, vm.name, coalesce(pm.slug, vm.slug) "
            "order by mk.name, vm.name"
        ),
        {"id": p.id},
    ).all()
    fits = [
        {"make": f.make, "make_slug": f.make_slug, "model": f.model, "model_slug": f.model_slug, "years": list(f.years or [])}
        for f in fit_rows[:25]
    ]

    backordered = bool(p.on_order) and int(p.available_qty or 0) <= 0
    return {
        "id": p.id,
        "sku": p.sku,
        "title": p.title,
        "slug": p.slug,
        "price": f"{p.list_price:.2f}" if p.list_price is not None else None,
        "condition": p.condition,
        # The manufacturer part number is only shown when the admin ticked "show to customers".
        "part_number": p.manufacturer_part_number if p.show_part_number else None,
        "available": max(0, int(p.available_qty or 0)),
        "stock_mode": p.stock_mode if p.stock_mode in ("available", "count") else "available",
        "backordered": backordered,
        "expected_date": p.expected_date.isoformat() if backordered and p.expected_date else None,
        "days_until": int(p.days_until) if backordered and p.days_until is not None else None,
        "description": clean_description(p.description),
        "photos": gallery(photos),
        "specifics": [{"name": s.name, "value": s.value} for s in specifics],
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "category": category,
        "fits": fits,
        "fits_total": len(fit_rows),
    }
