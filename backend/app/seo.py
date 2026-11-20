"""
Public data for search engines: sitemap lists and vehicle (make/model) pages.
Read-only, no login required. Mounted at /store/seo/*.

  GET /store/seo/vehicles                         every make > model that has published parts (for index pages)
  GET /store/seo/vehicle/{make_slug}/{model_slug} resolves a make/model address to the ids the storefront needs
  GET /store/seo/sitemap/products                 published product slugs + last change (paged)
  GET /store/seo/sitemap/pages                    category and vehicle pages worth listing

A "model" here is always a top-level model. A submodel (vehicle_models.parent_model_id) is not a page of its
own: its parts show on the page of the model it hangs under, so its address points there (canonical_model_slug).
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.fitment_sub import family_sql

router = APIRouter(prefix="/store/seo", tags=["store"])

# Same rule the store uses everywhere: only published products are public.
PUBLISHED_FILTER = "p.is_published = true"

# Top-level models that have at least one published part made for them, with how many.
# (Counts parts with specific fitment only, like the machine pages' own "fits your machine" lists;
# universal parts also show on those pages but would make every model look the same size.)
ROOT_MODELS_WITH_PARTS = f"""
    select r.id as model_id, r.name as model, r.slug as model_slug,
           mk.id as make_id, mk.name as make, mk.slug as make_slug,
           vt.slug as vehicle_type, count(distinct p.id) as products
    from vehicle_models r
    join vehicle_makes mk on mk.id = r.make_id
    left join vehicle_types vt on vt.id = r.vehicle_type_id
    join vehicle_models m on m.id = r.id or m.parent_model_id = r.id
    join vehicles v on v.model_id = m.id
    join product_fitment pf on pf.vehicle_id = v.id
    join products p on p.id = pf.product_id
    where r.parent_model_id is null
      and {PUBLISHED_FILTER}
      and not p.fitment_exempt
    group by r.id, r.name, r.slug, mk.id, mk.name, mk.slug, vt.slug
"""


@router.get("/vehicles")
def vehicles(db: Session = Depends(get_db)):
    rows = db.execute(text(f"select * from ({ROOT_MODELS_WITH_PARTS}) q order by q.make, q.model")).all()
    out: list[dict] = []
    for r in rows:
        if not out or out[-1]["slug"] != r.make_slug:
            out.append({"name": r.make, "slug": r.make_slug, "models": []})
        out[-1]["models"].append(
            {"name": r.model, "slug": r.model_slug, "vehicle_type": r.vehicle_type, "products": r.products}
        )
    return out


@router.get("/vehicle/{make_slug}/{model_slug}")
def vehicle(make_slug: str, model_slug: str, db: Session = Depends(get_db)):
    m = db.execute(
        text(
            "select r.id, r.name, r.slug, r.parent_model_id, "
            "mk.id as make_id, mk.name as make, mk.slug as make_slug, vt.slug as vehicle_type, "
            "(select pr.slug from vehicle_models pr where pr.id = r.parent_model_id) as parent_slug "
            "from vehicle_models r "
            "join vehicle_makes mk on mk.id = r.make_id "
            "left join vehicle_types vt on vt.id = r.vehicle_type_id "
            "where mk.slug = :ms and r.slug = :s"
        ),
        {"ms": make_slug, "s": model_slug},
    ).first()
    if not m:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    # A submodel's address points at the model it hangs under; the storefront redirects there.
    root_id = m.parent_model_id or m.id
    counted = db.execute(
        text(f"select * from ({ROOT_MODELS_WITH_PARTS}) q where q.model_id = :root"), {"root": root_id}
    ).first()
    if not counted:
        # No published parts: a page for it would be empty, so it does not exist for search engines.
        raise HTTPException(status_code=404, detail="No parts for this vehicle yet")

    years = db.execute(
        text(
            "select distinct v.year from vehicles v "
            f"where v.model_id in {family_sql('model_id')} and v.year is not null order by v.year desc"
        ),
        {"model_id": root_id},
    ).all()
    return {
        "make": {"id": m.make_id, "name": m.make, "slug": m.make_slug},
        # id is the model id the existing /store/fitment/products endpoint expects (it covers the whole family).
        "model": {"id": root_id, "name": counted.model, "slug": counted.model_slug, "vehicle_type": counted.vehicle_type},
        "canonical_model_slug": counted.model_slug,
        "is_submodel": m.parent_model_id is not None,
        "years": [y.year for y in years],
        "products": counted.products,
    }


@router.get("/sitemap/products")
def sitemap_products(
    limit: int = Query(5000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    total = db.execute(text(f"select count(*) from products p where {PUBLISHED_FILTER}")).scalar()
    rows = db.execute(
        text(
            f"select p.slug, p.updated_at from products p where {PUBLISHED_FILTER} "
            "and p.slug is not null and p.slug <> '' order by p.id limit :limit offset :offset"
        ),
        {"limit": limit, "offset": offset},
    ).all()
    return {
        "total": total,
        "items": [{"slug": r.slug, "updated_at": r.updated_at.isoformat() if r.updated_at else None} for r in rows],
    }


@router.get("/sitemap/pages")
def sitemap_pages(db: Session = Depends(get_db)):
    # A category is listed when it, or anything under it, has a published product (its page shows the whole tree).
    cats = db.execute(
        text(
            "with recursive tree as ("
            " select id as root_id, id from categories"
            " union all"
            " select t.root_id, c.id from categories c join tree t on c.parent_id = t.id"
            ") "
            "select c.slug from categories c "
            "where c.slug is not null and exists ("
            f" select 1 from tree t join products p on p.category_id = t.id where t.root_id = c.id and {PUBLISHED_FILTER}"
            ") order by c.sort_order, c.name"
        )
    ).all()
    models = db.execute(
        text(f"select make_slug, model_slug from ({ROOT_MODELS_WITH_PARTS}) q order by q.make_slug, q.model_slug")
    ).all()
    return {
        "categories": [c.slug for c in cats],
        "vehicles": [{"make": m.make_slug, "model": m.model_slug} for m in models],
    }
