"""
Public storefront fitment lookups (Year > Make > Model > Parts).
Read-only, no login required. Mounted at /store/fitment/*.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(prefix="/store/fitment", tags=["store"])

# Only published products show in the store.
PUBLISHED_FILTER = "p.is_published = true"


@router.get("/years")
def years(db: Session = Depends(get_db)):
    rows = db.execute(
        text("select distinct year from fitment_catalog order by year desc")
    ).all()
    return [r.year for r in rows]


@router.get("/makes")
def makes(year: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select distinct make_id, make from fitment_catalog "
            "where year = :year order by make"
        ),
        {"year": year},
    ).all()
    return [{"id": r.make_id, "name": r.make} for r in rows]


@router.get("/models")
def models(year: int, make_id: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select model_id, model, products from fitment_catalog "
            "where year = :year and make_id = :make_id order by model"
        ),
        {"year": year, "make_id": make_id},
    ).all()
    return [{"id": r.model_id, "name": r.model, "products": r.products} for r in rows]


@router.get("/products")
def products(
    year: int,
    model_id: int,
    limit: int = Query(24, le=100),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    base = f"""
        from product_fitment pf
        join vehicles v on v.id = pf.vehicle_id
        join products p on p.id = pf.product_id
        where v.model_id = :model_id and v.year = :year and {PUBLISHED_FILTER}
    """
    params = {"model_id": model_id, "year": year}
    total = db.execute(text(f"select count(distinct p.id) {base}"), params).scalar()
    rows = db.execute(
        text(
            f"select distinct p.id, p.title {base} "
            "order by p.title limit :limit offset :offset"
        ),
        {**params, "limit": limit, "offset": offset},
    ).all()
    return {"total": total, "items": [{"id": r.id, "title": r.title} for r in rows]}
