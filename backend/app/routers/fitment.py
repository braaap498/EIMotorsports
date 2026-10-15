from fastapi import APIRouter, Request, Query

router = APIRouter(prefix="/fitment", tags=["fitment"])

# Only published products show in the store.
PUBLISHED_FILTER = "p.is_published = true"


@router.get("/years")
async def years(request: Request):
    rows = await request.app.state.pool.fetch(
        "select distinct year from fitment_catalog order by year desc"
    )
    return [r["year"] for r in rows]


@router.get("/makes")
async def makes(request: Request, year: int):
    rows = await request.app.state.pool.fetch(
        "select distinct make_id, make from fitment_catalog where year = $1 order by make",
        year,
    )
    return [{"id": r["make_id"], "name": r["make"]} for r in rows]


@router.get("/models")
async def models(request: Request, year: int, make_id: int):
    rows = await request.app.state.pool.fetch(
        """select model_id, model, products from fitment_catalog
           where year = $1 and make_id = $2 order by model""",
        year,
        make_id,
    )
    return [
        {"id": r["model_id"], "name": r["model"], "products": r["products"]}
        for r in rows
    ]


@router.get("/products")
async def products(
    request: Request,
    year: int,
    model_id: int,
    limit: int = Query(24, le=100),
    offset: int = 0,
):
    pool = request.app.state.pool
    base = f"""
        from product_fitment pf
        join vehicles v on v.id = pf.vehicle_id
        join products p on p.id = pf.product_id
        where v.model_id = $1 and v.year = $2 and {PUBLISHED_FILTER}
    """
    total = await pool.fetchval(f"select count(distinct p.id) {base}", model_id, year)
    rows = await pool.fetch(
        f"""select distinct p.id, p.title {base}
            order by p.title limit $3 offset $4""",
        model_id,
        year,
        limit,
        offset,
    )
    return {"total": total, "items": [dict(r) for r in rows]}
