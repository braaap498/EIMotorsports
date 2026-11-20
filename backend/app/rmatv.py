"""
Rocky Mountain ATV/MC (RMATV) supplier data.

Mounted at /admin/rmatv/* (owner/admin only).

  GET  /summary           what is stored, and the last imports
  GET  /brands            brands in the catalog, for the filter
  GET  /items             browse / search the stored parts
  POST /import/detect     which column of the file feeds which field (reads nothing, changes nothing)
  POST /import/start      begin an import (the browser then sends the rows in pieces)
  POST /import/chunk      one piece of the file
  POST /import/finish     check the whole file arrived, then retire parts that left the file

HOW IT FITS
  * Rocky Mountain parts live in the same table as Automatic Distributors parts (supplier_items), told apart by
    supplier = 'rmatv'. Every query in autodist.py already filters on its own supplier, so the two never mix.
    Purchase orders, Distributor parts on a product, and receiving all work from a part's id, so they work for
    these parts without any change: the PO line's Part # is the Rocky Mountain part number, and its cost is the
    dealer cost stored here.
  * Rocky Mountain stock is one number (no warehouses), kept in supplier_items.qty_other. It is NEVER added to
    products.quantity_on_hand: that number only counts parts on our own shelf.
  * `upsert_rows` is the one door the data goes through. Today the rows come from a file the person uploads. If
    Rocky Mountain turns out to have an API, its adapter should build the same dicts (see clean_row) and call
    `upsert_rows` between _begin_run and _finish_run; nothing else here changes. Any API key belongs in the
    server's environment (RMATV_API_KEY and the like), never in the browser and never in the database.
  * We do not know the exact columns of Rocky Mountain's file, so the browser reads the file, /import/detect
    guesses which column is which, and the person can correct each guess before anything is stored.

LICENSE
  Dealer cost and stock are the distributor's confidential data, handled like Automatic Distributors': every route
  here is admin-only and nothing is mounted on the public store API. All of it sits in supplier_items rows with
  supplier = 'rmatv' (and supplier_import_runs), so it can be removed in one step if the agreement ends.
"""
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin
from app.autodist import norm_part, norm_upc
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/rmatv", tags=["admin-rmatv"], dependencies=[Depends(require_admin)])

SUPPLIER = "rmatv"
# Same safety net as Automatic Distributors: if a "full" file holds fewer than this share of the parts we already
# have, something is wrong with the file; nothing is retired until a person confirms.
MIN_KEEP_RATIO = 0.8
MAX_COLUMNS = 200
MAX_PIECE_ROWS = 6000
SKU_MAX = 60


def ensure_columns() -> None:
    """Rocky Mountain's single stock number needs its own column (Automatic's are per warehouse). Called once at startup."""
    db = _session_factory()()
    try:
        db.execute(text("alter table supplier_items add column if not exists qty_other integer not null default 0"))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not add supplier_items.qty_other; Rocky Mountain stock will not work until it exists (run sql/rmatv_setup.sql)")
    finally:
        db.close()


# --------------------------------------------------------------------------
# which column is which (pure functions, no database)
# --------------------------------------------------------------------------
# key, label shown on the page, what it is for
FIELDS: list[tuple[str, str, bool]] = [
    ("sku", "Rocky Mountain part number", True),
    ("name", "Name / description", False),
    ("manufacturer", "Brand", False),
    ("mfr_number", "Manufacturer part number", False),
    ("dealer_cost", "Your cost (dealer price)", False),
    ("list_price", "List / retail price", False),
    ("qty", "Quantity in stock", False),
    ("upc", "UPC / barcode", False),
    ("unit_of_measure", "Unit of measure", False),
]

# Header names (case, spaces and punctuation ignored), best guess first. The lists do not overlap, so one column
# never feeds two fields. A bare "Price" or "Cost" is NOT guessed: it could be retail or ours, so the person picks it.
_ALIASES: dict[str, list[str]] = {
    "sku": ["rmatvpartnumber", "rmatvpart", "rmatvsku", "rockymountainpartnumber", "partnumber", "partno", "partnum", "part", "sku", "itemnumber", "itemno", "item", "productnumber", "productno", "stocknumber"],
    "name": ["itemname", "productname", "name", "partname", "partdescription", "productdescription", "itemdescription", "description", "title"],
    "manufacturer": ["brand", "brandname", "manufacturer", "manufacturername", "mfr", "mfgname", "make"],
    "mfr_number": ["manufacturerpartnumber", "manufacturernumber", "mfrpartnumber", "mfrnumber", "mfrpart", "mfgpartnumber", "mfgpart", "mpn", "brandpartnumber", "vendorpartnumber", "vendorpart"],
    "dealer_cost": ["dealercost", "dealerprice", "dealernet", "yourcost", "yourprice", "customerprice", "netprice", "wholesaleprice", "wholesale", "yourdealerprice"],
    "list_price": ["listprice", "retailprice", "retail", "msrp", "suggestedretail"],
    "qty": ["qtyavailable", "quantityavailable", "availableqty", "availablequantity", "qtyonhand", "onhand", "quantityonhand", "instock", "stock", "qty", "quantity", "inventory", "available"],
    "upc": ["upc", "upccode", "upcnumber", "barcode", "ean", "gtin", "upcean"],
    "unit_of_measure": ["unitofmeasure", "uom", "unit", "packagequantity"],
}
FIELD_KEYS = {k for k, _, _ in FIELDS}


def _head(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def detect_mapping(columns: list[str]) -> dict[str, int]:
    """The file's header row -> {field: index of the column that feeds it}. Fields with no good guess are left out."""
    heads = [_head(c) for c in columns]
    out: dict[str, int] = {}
    used: set[int] = set()
    for field, _label, _req in FIELDS:
        for alias in _ALIASES[field]:
            hit = next((i for i, h in enumerate(heads) if h == alias and i not in used), None)
            if hit is not None:
                out[field] = hit
                used.add(hit)
                break
    return out


def _check_mapping(mapping: dict[str, int]) -> dict[str, int]:
    for k, i in mapping.items():
        if k not in FIELD_KEYS:
            raise HTTPException(400, f"Unknown field '{k}'.")
        if not isinstance(i, int) or i < 0 or i >= MAX_COLUMNS:
            raise HTTPException(400, "A chosen column is out of range.")
    if "sku" not in mapping:
        raise HTTPException(400, "Choose which column holds the Rocky Mountain part number.")
    if len(set(mapping.values())) != len(mapping):
        raise HTTPException(400, "Two fields are set to the same column. Each field needs its own column.")
    return mapping


# --------------------------------------------------------------------------
# cleaning one row
# --------------------------------------------------------------------------
def _price(value: Optional[str]) -> Optional[Decimal]:
    """'$1,225.74' -> 1225.74. Zero, negative, or not a number -> None (a $0 export value means 'unknown', not 'free')."""
    v = re.sub(r"[\s$,]", "", value or "")
    try:
        d = Decimal(v).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return d if Decimal("0") < d < Decimal("10000000") else None


def _qty(value: Optional[str]) -> int:
    v = re.sub(r"[\s,]", "", value or "")
    try:
        return max(0, min(int(Decimal(v)), 100_000_000))
    except (InvalidOperation, ValueError):
        return 0


def clean_row(values: list[str], mapping: dict[str, int]) -> Optional[dict]:
    """One file row + the column mapping -> the values stored. None when the row has no usable part number."""

    def get(field: str) -> str:
        i = mapping.get(field)
        return values[i].strip() if i is not None and i < len(values) and values[i] is not None else ""

    sku = get("sku")
    # a part number we would have to cut short would put a WRONG number on a purchase order, so such a row is refused
    if not sku or len(sku) > SKU_MAX:
        return None
    mfr = get("mfr_number") or None
    upc = get("upc") or None
    return {
        "sku": sku,
        "mfr_number": mfr,
        "mfr_number_norm": norm_part(mfr),
        "manufacturer": get("manufacturer") or None,
        "name": get("name") or None,
        "unit_of_measure": get("unit_of_measure") or None,
        "list_price": _price(get("list_price")),
        "dealer_cost": _price(get("dealer_cost")),
        "qty_other": _qty(get("qty")),
        "upc": upc,
        "upc_norm": norm_upc(upc),
    }


def clean_rows(rows: list[list[str]], mapping: dict[str, int]) -> tuple[list[dict], int]:
    """A piece of the file -> (clean rows, lines skipped). A part number twice in one piece keeps the later line and
    counts the earlier one as skipped, so stored + skipped always equals the lines sent."""
    keep: dict[str, dict] = {}
    skipped = 0
    for values in rows:
        row = clean_row(values, mapping)
        if row is None:
            skipped += 1
            continue
        if row["sku"] in keep:
            skipped += 1
        keep[row["sku"]] = row
    return list(keep.values()), skipped


# --------------------------------------------------------------------------
# storing rows: the one door for the file today and an API later
# --------------------------------------------------------------------------
_UPSERT = """
with incoming as (
  select * from unnest(
    cast(:sku as text[]), cast(:mfr as text[]), cast(:mfrn as text[]), cast(:brand as text[]),
    cast(:name as text[]), cast(:uom as text[]), cast(:lp as numeric[]), cast(:dc as numeric[]),
    cast(:qo as integer[]), cast(:upc as text[]), cast(:upcn as text[])
  ) as t(sku, mfr_number, mfr_number_norm, manufacturer, name, unit_of_measure, list_price, dealer_cost,
         qty_other, upc, upc_norm)
), up as (
  insert into supplier_items (supplier, sku, mfr_number, mfr_number_norm, manufacturer, name, unit_of_measure,
                              list_price, dealer_cost, qty_maine, qty_indiana, qty_nevada, qty_other, upc, upc_norm,
                              last_run_id)
  select :sup, sku, mfr_number, mfr_number_norm, manufacturer, name, unit_of_measure,
         list_price, dealer_cost, 0, 0, 0, qty_other, upc, upc_norm, :run
  from incoming
  on conflict (supplier, sku) do update set
    mfr_number = excluded.mfr_number, mfr_number_norm = excluded.mfr_number_norm,
    manufacturer = excluded.manufacturer, name = excluded.name, unit_of_measure = excluded.unit_of_measure,
    list_price = excluded.list_price, dealer_cost = excluded.dealer_cost,
    qty_other = excluded.qty_other, upc = excluded.upc, upc_norm = excluded.upc_norm,
    last_seen_at = now(), last_run_id = excluded.last_run_id,
    updated_at = case
      when not supplier_items.active
        or (supplier_items.mfr_number, supplier_items.manufacturer, supplier_items.name, supplier_items.list_price,
            supplier_items.dealer_cost, supplier_items.qty_other, supplier_items.upc)
           is distinct from
           (excluded.mfr_number, excluded.manufacturer, excluded.name, excluded.list_price,
            excluded.dealer_cost, excluded.qty_other, excluded.upc)
      then now() else supplier_items.updated_at end,
    active = true
  returning (xmax = 0) as inserted
)
select count(*) filter (where inserted) as new_rows from up
"""


def upsert_rows(db: Session, run_id: int, rows: list[dict]) -> int:
    """Insert or update these parts (see clean_row for the shape). Returns how many were new.
    Does not commit; the caller does."""
    if not rows:
        return 0
    col = lambda k: [r[k] for r in rows]  # noqa: E731
    new_rows = db.execute(
        text(_UPSERT),
        {
            "sup": SUPPLIER,
            "run": run_id,
            "sku": col("sku"),
            "mfr": col("mfr_number"),
            "mfrn": col("mfr_number_norm"),
            "brand": col("manufacturer"),
            "name": col("name"),
            "uom": col("unit_of_measure"),
            "lp": col("list_price"),
            "dc": col("dealer_cost"),
            "qo": col("qty_other"),
            "upc": col("upc"),
            "upcn": col("upc_norm"),
        },
    ).scalar()
    return int(new_rows or 0)


# --------------------------------------------------------------------------
# importing a file, piece by piece
# --------------------------------------------------------------------------
class DetectIn(BaseModel):
    columns: list[str] = Field(..., max_length=MAX_COLUMNS, description="the file's header row")


class StartIn(BaseModel):
    source: str = Field("file_upload", max_length=40)


class ChunkIn(BaseModel):
    run_id: int
    mapping: dict[str, int] = Field(..., description="field -> index of the file column that feeds it")
    rows: list[list[str]] = Field(..., max_length=MAX_PIECE_ROWS, description="some data lines of the file, already split into cells")


class FinishIn(BaseModel):
    run_id: int
    rows_sent: int = Field(..., ge=0, description="how many data lines the browser sent in total")
    force: bool = False  # retire the missing parts even though the file looks much smaller than what we hold


@router.post("/import/detect")
def import_detect(body: DetectIn):
    return {
        "mapped": detect_mapping(body.columns),
        "fields": [{"key": k, "label": label, "required": req} for k, label, req in FIELDS],
    }


def _run_or_404(db: Session, run_id: int):
    run = db.execute(
        text("select id, status, rows_seen, rows_new, rows_skipped from supplier_import_runs where id = :i and supplier = :s"),
        {"i": run_id, "s": SUPPLIER},
    ).first()
    if run is None:
        raise HTTPException(404, "That import does not exist.")
    return run


def _begin_run(db: Session, source: str, who: str) -> int:
    busy = db.execute(
        text(
            "select id from supplier_import_runs where supplier = :s and status = 'running' "
            "and started_at > now() - interval '30 minutes' limit 1"
        ),
        {"s": SUPPLIER},
    ).scalar()
    if busy:
        raise HTTPException(409, "An import is already running. Wait for it to finish (or about 30 minutes).")
    run_id = db.execute(
        text("insert into supplier_import_runs (supplier, source, created_by) values (:s, :src, :u) returning id"),
        {"s": SUPPLIER, "src": source, "u": who},
    ).scalar()
    db.commit()
    return run_id


@router.post("/import/start")
def import_start(body: StartIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    return {"run_id": _begin_run(db, body.source, user.email or user.user_id)}


@router.post("/import/chunk")
def import_chunk(body: ChunkIn, db: Session = Depends(get_db)):
    mapping = _check_mapping(body.mapping)
    run = _run_or_404(db, body.run_id)
    if run.status != "running":
        raise HTTPException(409, "That import is not running any more.")
    try:
        rows, skipped = clean_rows(body.rows, mapping)
        new_rows = upsert_rows(db, body.run_id, rows)
        db.execute(
            text(
                "update supplier_import_runs set rows_seen = rows_seen + :n, rows_new = rows_new + :w, "
                "rows_skipped = rows_skipped + :k where id = :i"
            ),
            {"n": len(rows), "w": new_rows, "k": skipped, "i": body.run_id},
        )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("rmatv import chunk failed (run %s)", body.run_id)
        db.execute(
            text("update supplier_import_runs set status = 'failed', finished_at = now(), note = :n where id = :i"),
            {"n": "A piece of the file could not be stored. See the server log.", "i": body.run_id},
        )
        db.commit()
        raise HTTPException(500, "A piece of the file could not be stored. The import was stopped; nothing was retired.")
    return {"stored": len(rows), "new": new_rows, "skipped": skipped}


def _finish_run(db: Session, run_id: int, rows_sent: int, force: bool) -> dict:
    """Check the whole file arrived, then retire the parts that left the file."""
    run = _run_or_404(db, run_id)
    if run.status not in ("running", "needs_review"):
        raise HTTPException(409, "That import is already finished.")

    def fail(note: str, code: int = 409):
        db.execute(
            text("update supplier_import_runs set status = 'failed', finished_at = now(), note = :n where id = :i"),
            {"n": note, "i": run_id},
        )
        db.commit()
        raise HTTPException(code, note + " Nothing was retired.")

    if run.rows_seen == 0:
        fail("The file held no usable parts.")
    if run.rows_seen + run.rows_skipped != rows_sent:
        fail(
            f"The server received {run.rows_seen + run.rows_skipped:,} lines but {rows_sent:,} were sent; "
            "part of the file did not arrive."
        )

    stale = db.execute(
        text("select count(*) from supplier_items where supplier = :s and active and last_run_id is distinct from :r"),
        {"s": SUPPLIER, "r": run_id},
    ).scalar() or 0
    prior = stale + (run.rows_seen - run.rows_new)  # parts we held before this file: the ones it re-sent, plus the ones it didn't
    if prior > 0 and stale > (1 - MIN_KEEP_RATIO) * prior and not force:
        db.execute(
            text("update supplier_import_runs set status = 'needs_review', note = :n where id = :i"),
            {"n": f"{stale:,} of the {prior:,} parts we hold are not in this file.", "i": run_id},
        )
        db.commit()
        return {"ok": False, "needs_review": True, "missing": int(stale), "held": int(prior), "stored": run.rows_seen, "new": run.rows_new}

    retired = db.execute(
        text(
            "update supplier_items set active = false, updated_at = now() "
            "where supplier = :s and active and last_run_id is distinct from :r"
        ),
        {"s": SUPPLIER, "r": run_id},
    ).rowcount
    db.execute(
        text("update supplier_import_runs set status = 'done', finished_at = now(), rows_deactivated = :d, note = null where id = :i"),
        {"d": retired, "i": run_id},
    )
    db.commit()
    return {"ok": True, "needs_review": False, "stored": run.rows_seen, "new": run.rows_new, "skipped": run.rows_skipped, "retired": retired}


@router.post("/import/finish")
def import_finish(body: FinishIn, db: Session = Depends(get_db)):
    return _finish_run(db, body.run_id, body.rows_sent, body.force)


# --------------------------------------------------------------------------
# looking at what is stored
# --------------------------------------------------------------------------
@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    s = db.execute(
        text(
            """
            select count(*) as total,
                   count(*) filter (where active) as active,
                   count(*) filter (where active and qty_other > 0) as in_stock,
                   count(distinct manufacturer) filter (where active) as brands,
                   max(last_seen_at) as last_seen
            from supplier_items where supplier = :s
            """
        ),
        {"s": SUPPLIER},
    ).first()
    runs = db.execute(
        text(
            "select id, source, status, started_at, finished_at, rows_seen, rows_new, rows_skipped, rows_deactivated, note, created_by "
            "from supplier_import_runs where supplier = :s order by id desc limit 100"
        ),
        {"s": SUPPLIER},
    ).all()
    return {"counts": dict(s._mapping), "runs": [dict(r._mapping) for r in runs]}


@router.get("/brands")
def brands(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select manufacturer, count(*) as n from supplier_items where supplier = :s and active "
            "and coalesce(manufacturer, '') <> '' group by manufacturer order by manufacturer"
        ),
        {"s": SUPPLIER},
    ).all()
    return {"items": [{"name": r.manufacturer, "count": r.n} for r in rows]}


_SORTS = {
    "sku": "s.sku",
    "name": "lower(s.name)",
    "brand": "lower(s.manufacturer)",
    "mfr": "s.mfr_number",
    "cost": "s.dealer_cost",
    "list": "s.list_price",
    "stock": "s.qty_other",
    "updated": "s.updated_at",
}


def _like(word: str) -> str:
    """A typed word as a LIKE pattern that matches it literally (a part number can hold _ or %)."""
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("/items")
def items(
    q: Optional[str] = Query(None, max_length=200, description="words anywhere: part number, manufacturer number, name, brand or UPC"),
    brand: Optional[str] = Query(None, max_length=100),
    in_stock: bool = False,
    status: str = Query("active", pattern="^(active|inactive|all)$"),
    sort: str = Query("sku", pattern="^(sku|name|brand|mfr|cost|list|stock|updated)$"),
    desc: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    where = ["s.supplier = :sup"]
    params: dict = {"sup": SUPPLIER, "n": limit, "o": offset}
    if status == "active":
        where.append("s.active")
    elif status == "inactive":
        where.append("not s.active")
    if brand:
        where.append("lower(s.manufacturer) = lower(:brand)")
        params["brand"] = brand.strip()
    if in_stock:
        where.append("s.qty_other > 0")
    for i, word in enumerate([w for w in re.split(r"\s+", (q or "").strip()) if w][:6]):
        params[f"w{i}"] = _like(word)
        cond = (
            f"s.sku ilike :w{i} or s.mfr_number ilike :w{i} or s.name ilike :w{i} or s.manufacturer ilike :w{i} or s.upc ilike :w{i}"
        )
        norm = re.sub(r"[^A-Za-z0-9]", "", word).upper()
        if norm:  # dashes and spaces in part numbers are ignored, as everywhere else
            params[f"p{i}"] = f"%{norm}%"
            cond += (
                f" or upper(regexp_replace(s.sku, '[^A-Za-z0-9]', '', 'g')) like :p{i} or s.mfr_number_norm like :p{i}"
            )
        digits = re.sub(r"\D", "", word).lstrip("0")
        if len(digits) >= 8:  # a typed or scanned barcode: UPC-A and EAN-13 forms meet (same rule as norm_upc)
            params[f"u{i}"] = f"%{digits}%"
            cond += f" or s.upc_norm like :u{i}"
        where.append(f"({cond})")
    clause = " and ".join(where)
    total = db.execute(text(f"select count(*) from supplier_items s where {clause}"), params).scalar()
    order = f"{_SORTS[sort]} {'desc' if desc else 'asc'} nulls last, s.sku"
    rows = db.execute(
        text(
            "select s.id, s.sku, s.mfr_number, s.manufacturer, s.name, s.unit_of_measure, s.list_price, s.dealer_cost, "
            f"s.qty_other, s.upc, s.active, s.updated_at from supplier_items s where {clause} order by {order} limit :n offset :o"
        ),
        params,
    ).all()
    out = []
    for r in rows:
        d = dict(r._mapping)
        for k in ("list_price", "dealer_cost"):
            d[k] = float(d[k]) if d[k] is not None else None
        out.append(d)
    return {"total": total, "items": out}
