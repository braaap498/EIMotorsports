"""
Automatic Distributors (Autodist) supplier data.

Mounted at /admin/autodist/* (owner/admin only).

  GET  /summary                what is stored, and the last imports
  POST /import/start           begin an import (the browser then sends the file in pieces)
  POST /import/chunk           one piece of the feed file
  POST /import/finish          check the whole file arrived, then retire parts that left the feed
  GET  /items?q=               search the stored supplier parts
  GET  /match-report           how many of OUR products match a supplier part (changes nothing)

HOW IT FITS
  * Supplier stock lives in its own tables (supplier_items). It is NEVER added to products.quantity_on_hand:
    that number drives reservations, picking and reordering (app/stock.py) and must only count parts on
    our shelf.
  * `upsert_rows` is the one door the data goes through. Today the rows come from the feed file; when the
    Autodist API is wired in, its adapter should build the same dicts (see clean_row) and call
    `upsert_rows` - nothing else here changes. The API key belongs in the server's settings
    (autodist_api_key in app/config.py), never in the browser.

LICENSE (Product Data License Agreement)
  This is licensed data. Dealer cost and stock are confidential (7.3) and must not be made available in
  bulk (4(c)). So: every route here is admin-only, nothing here is mounted on the public store API, and
  the tables are closed to the public Supabase roles (see sql/autodist_tables.sql). All Autodist data sits
  in supplier_items / supplier_import_runs so it can be deleted in one step if the agreement ends (11.2).
"""
import csv
import io
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin
from app.database import get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/autodist", tags=["admin-autodist"], dependencies=[Depends(require_admin)])

SUPPLIER = "autodist"
# If a "full" import holds fewer than this share of the parts we already have, something is wrong with
# the file; nothing is retired until a person confirms.
MIN_KEEP_RATIO = 0.8

# Column names in Autodist's Inventory Feed file -> our names.
FEED_COLUMNS = {
    "SKU": "sku",
    "Manufacturer Number": "mfr_number",
    "Manufacturer": "manufacturer",
    "Item_Name": "name",
    "Unit_Of_Measure": "unit_of_measure",
    "List_Price": "list_price",
    "Regular_Dealer_Price": "dealer_cost",
    "Maine_Quantity": "qty_maine",
    "Indiana_Quantity": "qty_indiana",
    "Nevada_Quantity": "qty_nevada",
    "UPC": "upc",
}


# --------------------------------------------------------------------------
# cleaning one row (pure functions, no database)
# --------------------------------------------------------------------------
def norm_part(value: Optional[str]) -> Optional[str]:
    """A part number for comparing: upper case, letters and digits only. 'ab-14 1001' -> 'AB141001'."""
    v = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    return v or None


def norm_upc(value: Optional[str]) -> Optional[str]:
    """A barcode for comparing: digits only, leading zeros dropped, so UPC-A and EAN-13 forms meet."""
    v = re.sub(r"\D", "", value or "").lstrip("0")
    return v if len(v) >= 8 else None


def _money(value: Optional[str]) -> Optional[Decimal]:
    try:
        v = Decimal((value or "").strip()).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return v if 0 <= v < Decimal("10000000") else None


def _qty(value: Optional[str]) -> int:
    try:
        return max(0, int(Decimal((value or "").strip())))
    except (InvalidOperation, ValueError):
        return 0


def clean_row(raw: dict) -> Optional[dict]:
    """One feed row (our column names) -> the values stored. None when the row has no SKU."""
    sku = (raw.get("sku") or "").strip()
    if not sku:
        return None
    mfr = (raw.get("mfr_number") or "").strip() or None
    upc = (raw.get("upc") or "").strip() or None
    return {
        "sku": sku[:60],
        "mfr_number": mfr,
        "mfr_number_norm": norm_part(mfr),
        "manufacturer": (raw.get("manufacturer") or "").strip() or None,
        "name": (raw.get("name") or "").strip() or None,
        "unit_of_measure": (raw.get("unit_of_measure") or "").strip() or None,
        "list_price": _money(raw.get("list_price")),
        "dealer_cost": _money(raw.get("dealer_cost")),
        "qty_maine": _qty(raw.get("qty_maine")),
        "qty_indiana": _qty(raw.get("qty_indiana")),
        "qty_nevada": _qty(raw.get("qty_nevada")),
        "upc": upc,
        "upc_norm": norm_upc(upc),
    }


def parse_feed_csv(body: str) -> tuple[list[dict], int]:
    """The text of (a piece of) the Inventory Feed CSV -> (clean rows, rows skipped as unreadable)."""
    reader = csv.DictReader(io.StringIO(body, newline=""))
    missing = [c for c in FEED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise HTTPException(400, "This does not look like the Inventory Feed file. Missing columns: " + ", ".join(missing))
    rows: dict[str, dict] = {}
    skipped = 0
    for rec in reader:
        if None in rec or any(v is None for v in rec.values()):  # too many or too few columns
            skipped += 1
            continue
        row = clean_row({FEED_COLUMNS[k]: v for k, v in rec.items() if k in FEED_COLUMNS})
        if row is None:
            skipped += 1
            continue
        rows[row["sku"]] = row  # a SKU twice in one piece: the later one wins
    return list(rows.values()), skipped


# --------------------------------------------------------------------------
# storing rows: the one door for the feed file today and the API later
# --------------------------------------------------------------------------
_UPSERT = """
with incoming as (
  select * from unnest(
    cast(:sku as text[]), cast(:mfr as text[]), cast(:mfrn as text[]), cast(:brand as text[]),
    cast(:name as text[]), cast(:uom as text[]), cast(:lp as numeric[]), cast(:dc as numeric[]),
    cast(:qm as integer[]), cast(:qi as integer[]), cast(:qn as integer[]),
    cast(:upc as text[]), cast(:upcn as text[])
  ) as t(sku, mfr_number, mfr_number_norm, manufacturer, name, unit_of_measure, list_price, dealer_cost,
         qty_maine, qty_indiana, qty_nevada, upc, upc_norm)
), up as (
  insert into supplier_items (supplier, sku, mfr_number, mfr_number_norm, manufacturer, name, unit_of_measure,
                              list_price, dealer_cost, qty_maine, qty_indiana, qty_nevada, upc, upc_norm,
                              last_run_id)
  select :sup, sku, mfr_number, mfr_number_norm, manufacturer, name, unit_of_measure,
         list_price, dealer_cost, qty_maine, qty_indiana, qty_nevada, upc, upc_norm, :run
  from incoming
  on conflict (supplier, sku) do update set
    mfr_number = excluded.mfr_number, mfr_number_norm = excluded.mfr_number_norm,
    manufacturer = excluded.manufacturer, name = excluded.name, unit_of_measure = excluded.unit_of_measure,
    list_price = excluded.list_price, dealer_cost = excluded.dealer_cost,
    qty_maine = excluded.qty_maine, qty_indiana = excluded.qty_indiana, qty_nevada = excluded.qty_nevada,
    upc = excluded.upc, upc_norm = excluded.upc_norm,
    last_seen_at = now(), last_run_id = excluded.last_run_id,
    updated_at = case
      when not supplier_items.active
        or (supplier_items.mfr_number, supplier_items.manufacturer, supplier_items.name, supplier_items.list_price,
            supplier_items.dealer_cost, supplier_items.qty_maine, supplier_items.qty_indiana,
            supplier_items.qty_nevada, supplier_items.upc)
           is distinct from
           (excluded.mfr_number, excluded.manufacturer, excluded.name, excluded.list_price,
            excluded.dealer_cost, excluded.qty_maine, excluded.qty_indiana, excluded.qty_nevada, excluded.upc)
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
            "qm": col("qty_maine"),
            "qi": col("qty_indiana"),
            "qn": col("qty_nevada"),
            "upc": col("upc"),
            "upcn": col("upc_norm"),
        },
    ).scalar()
    return int(new_rows or 0)


# --------------------------------------------------------------------------
# importing a feed file, piece by piece
# --------------------------------------------------------------------------
class StartIn(BaseModel):
    source: str = Field("csv_upload", max_length=40)


class ChunkIn(BaseModel):
    run_id: int
    csv: str = Field(..., max_length=4_000_000, description="the header line plus some data lines of the feed file")


class FinishIn(BaseModel):
    run_id: int
    rows_sent: int = Field(..., ge=0, description="how many data lines the browser sent in total")
    force: bool = False  # retire the missing parts even though the file looks much smaller than what we hold


def _run_or_404(db: Session, run_id: int):
    run = db.execute(
        text("select id, status, rows_seen, rows_new, rows_skipped from supplier_import_runs where id = :i and supplier = :s"),
        {"i": run_id, "s": SUPPLIER},
    ).first()
    if run is None:
        raise HTTPException(404, "That import does not exist.")
    return run


@router.post("/import/start")
def import_start(body: StartIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
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
        {"s": SUPPLIER, "src": body.source, "u": user.email or user.user_id},
    ).scalar()
    db.commit()
    return {"run_id": run_id}


@router.post("/import/chunk")
def import_chunk(body: ChunkIn, db: Session = Depends(get_db)):
    run = _run_or_404(db, body.run_id)
    if run.status != "running":
        raise HTTPException(409, "That import is not running any more.")
    try:
        rows, skipped = parse_feed_csv(body.csv)
        new_rows = upsert_rows(db, body.run_id, rows)
        db.execute(
            text(
                "update supplier_import_runs set rows_seen = rows_seen + :n, rows_new = rows_new + :w, "
                "rows_skipped = rows_skipped + :k where id = :i"
            ),
            {"n": len(rows), "w": new_rows, "k": skipped, "i": body.run_id},
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("autodist import chunk failed (run %s)", body.run_id)
        db.execute(
            text("update supplier_import_runs set status = 'failed', finished_at = now(), note = :n where id = :i"),
            {"n": "A piece of the file could not be stored. See the server log.", "i": body.run_id},
        )
        db.commit()
        raise HTTPException(500, "A piece of the file could not be stored. The import was stopped; nothing was retired.")
    return {"stored": len(rows), "new": new_rows, "skipped": skipped}


@router.post("/import/finish")
def import_finish(body: FinishIn, db: Session = Depends(get_db)):
    run = _run_or_404(db, body.run_id)
    if run.status not in ("running", "needs_review"):
        raise HTTPException(409, "That import is already finished.")

    def fail(note: str, code: int = 409):
        db.execute(
            text("update supplier_import_runs set status = 'failed', finished_at = now(), note = :n where id = :i"),
            {"n": note, "i": body.run_id},
        )
        db.commit()
        raise HTTPException(code, note + " Nothing was retired.")

    if run.rows_seen == 0:
        fail("The file held no usable parts.")
    if run.rows_seen + run.rows_skipped != body.rows_sent:
        fail(
            f"The server received {run.rows_seen + run.rows_skipped:,} lines but {body.rows_sent:,} were sent; "
            "part of the file did not arrive."
        )

    stale = db.execute(
        text("select count(*) from supplier_items where supplier = :s and active and last_run_id is distinct from :r"),
        {"s": SUPPLIER, "r": body.run_id},
    ).scalar() or 0
    prior = stale + (run.rows_seen - run.rows_new)  # parts we held before this file: the ones it re-sent, plus the ones it didn't
    if prior > 0 and stale > (1 - MIN_KEEP_RATIO) * prior and not body.force:
        db.execute(
            text("update supplier_import_runs set status = 'needs_review', note = :n where id = :i"),
            {"n": f"{stale:,} of the {prior:,} parts we hold are not in this file.", "i": body.run_id},
        )
        db.commit()
        return {"ok": False, "needs_review": True, "missing": int(stale), "held": int(prior), "stored": run.rows_seen, "new": run.rows_new}

    retired = db.execute(
        text(
            "update supplier_items set active = false, updated_at = now() "
            "where supplier = :s and active and last_run_id is distinct from :r"
        ),
        {"s": SUPPLIER, "r": body.run_id},
    ).rowcount
    db.execute(
        text(
            "update supplier_import_runs set status = 'done', finished_at = now(), rows_deactivated = :d, note = null where id = :i"
        ),
        {"d": retired, "i": body.run_id},
    )
    db.commit()
    return {"ok": True, "needs_review": False, "stored": run.rows_seen, "new": run.rows_new, "skipped": run.rows_skipped, "retired": retired}


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
                   count(*) filter (where active and qty_maine + qty_indiana + qty_nevada > 0) as in_stock,
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
            "from supplier_import_runs where supplier = :s order by id desc limit 5"
        ),
        {"s": SUPPLIER},
    ).all()
    return {"counts": dict(s._mapping), "runs": [dict(r._mapping) for r in runs]}


@router.get("/items")
def items(
    q: Optional[str] = None,
    active_only: bool = True,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    where = ["supplier = :s"]
    params: dict = {"s": SUPPLIER, "n": limit, "o": offset}
    if active_only:
        where.append("active")
    for i, w in enumerate((q or "").split()[:6]):
        params[f"w{i}"] = f"%{w}%"
        where.append(
            f"(sku ilike :w{i} or mfr_number ilike :w{i} or name ilike :w{i} or manufacturer ilike :w{i} or upc ilike :w{i})"
        )
    clause = " and ".join(where)
    total = db.execute(text(f"select count(*) from supplier_items where {clause}"), params).scalar()
    rows = db.execute(
        text(
            f"select id, sku, mfr_number, manufacturer, name, unit_of_measure, list_price, dealer_cost, "
            f"qty_maine, qty_indiana, qty_nevada, upc, active, updated_at "
            f"from supplier_items where {clause} order by manufacturer, sku limit :n offset :o"
        ),
        params,
    ).all()
    return {"total": total, "rows": [dict(r._mapping) for r in rows]}


# --------------------------------------------------------------------------
# which of our products have a supplier part (read only)
# --------------------------------------------------------------------------
# The manufacturer part number: the product's own field, or the item specific of the same name
# (the same rule the purchase orders use).
_MPN = (
    "coalesce(nullif(btrim(p.manufacturer_part_number), ''), "
    "(select s.value from product_item_specifics s where s.product_id = p.id "
    "  and s.name = 'Manufacturer Part Number' order by s.sort_order, s.id limit 1))"
)

_MATCH = f"""
with prod as (
  select p.id, p.sku_display as sku, p.title,
         nullif(ltrim(regexp_replace(coalesce(p.barcode, ''), '\\D', '', 'g'), '0'), '') as upc_n,
         nullif(upper(regexp_replace(coalesce({_MPN}, ''), '[^A-Za-z0-9]', '', 'g')), '') as mpn_n
  from products p
), cand as (
  select prod.id, prod.sku, prod.title,
         (select count(*) from supplier_items s where s.supplier = :s and s.active and prod.upc_n is not null
            and length(prod.upc_n) >= 8 and s.upc_norm = prod.upc_n) as by_upc,
         (select count(*) from supplier_items s where s.supplier = :s and s.active and prod.mpn_n is not null
            and s.mfr_number_norm = prod.mpn_n) as by_mpn
  from prod
)
select id, sku, title, by_upc, by_mpn,
       case when by_upc = 1 then 'upc'
            when by_upc > 1 then 'upc_many'
            when by_mpn = 1 then 'mpn'
            when by_mpn > 1 then 'mpn_many'
            else 'none' end as kind
from cand
"""


@router.get("/match-report")
def match_report(db: Session = Depends(get_db)):
    """Counts, plus the part-number-only matches to look at. Nothing is saved or changed."""
    rows = db.execute(text(_MATCH), {"s": SUPPLIER}).all()
    counts = {"upc": 0, "upc_many": 0, "mpn": 0, "mpn_many": 0, "none": 0}
    for r in rows:
        counts[r.kind] += 1

    # Part-number-only matches: show our title next to the supplier's, since the same number can belong
    # to a different brand. (A barcode match is exact and needs no checking.)
    review_ids = [r.id for r in rows if r.kind in ("mpn", "mpn_many")][:300]
    review: list[dict] = []
    if review_ids:
        found = db.execute(
            text(
                f"""
                select p.id, p.sku_display as sku, p.title,
                       s.sku as supplier_sku, s.manufacturer, s.name as supplier_name, s.dealer_cost
                from products p
                join supplier_items s on s.supplier = :s and s.active
                 and s.mfr_number_norm = nullif(upper(regexp_replace(coalesce({_MPN}, ''), '[^A-Za-z0-9]', '', 'g')), '')
                where p.id = any(cast(:ids as bigint[]))
                order by p.id, s.manufacturer
                """
            ),
            {"s": SUPPLIER, "ids": review_ids},
        ).all()
        review = [dict(f._mapping) for f in found]
    return {"total_products": len(rows), "counts": counts, "review": review, "review_limited": len(review_ids) == 300}
