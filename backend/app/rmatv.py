"""
Rocky Mountain ATV/MC (RMATV) supplier data.

Mounted at /admin/rmatv/* (owner/admin only).

  GET  /summary           what is stored, and the last imports
  GET  /brands            brands in the catalog, for the filter
  GET  /items             browse / search the stored parts (same filters as the Automatic Distributors catalog)
  POST /items/{id}/make-product   make the part's single product; its photo links become the product's photos and its fitment cell
                          (year-year:MAKE:MODEL|...) becomes the product's fitment (makes/models/years added to the fitment tool only when new)
  POST /photos/store      download the photos of those products into our storage (background)   GET /photos/status
  GET/POST /relink        carry the products' links from the hand-typed saved parts (and dropped catalog parts) to the catalog parts
  POST /import/detect     which column of the file feeds which field (reads nothing, changes nothing)
  POST /import/start      begin an import (the browser then sends the rows in pieces)
  POST /import/chunk      one piece of the file
  POST /import/finish     check the whole file arrived, then retire parts that left the file
  GET  /sftp/status       is the Rocky Mountain SFTP login set up on the server (nothing is contacted)
  POST /sftp/check        log in, list the folders and files (two levels), show the server's host key
  POST /sftp/peek         column names and sample lines of the data files found (nothing is stored)
  GET/PUT /sftp/config    which file is the catalog / your prices / the photo links, and any column choices you made
  POST /import/sftp       pull the catalog file, then your prices, from the SFTP server (runs in the background)
  POST /import/sftp-prices   pull just the price file
  POST /import/sftp-photos   read photo links from the photo file (or the catalog file)
  GET/PUT /sftp/schedule  automatic sync (off by default): the server checks the files' date and size and imports only on change
  GET  /overview          the catalog at a glance      GET /items/{id}  one part in full      GET /match-report  our products vs the catalog

HOW IT FITS
  * Rocky Mountain parts live in the same table as Automatic Distributors parts (supplier_items), told apart by
    supplier = 'rmatv'. Every query in autodist.py already filters on its own supplier, so the two never mix.
    Purchase orders, Distributor parts on a product, and receiving all work from a part's id, so they work for
    these parts without any change: the PO line's Part # is the Rocky Mountain part number, and its cost is the
    dealer cost stored here.
  * Rocky Mountain stock: supplier_items.qty_utah and qty_kentucky hold the two warehouses and qty_other their TOTAL. The files give
    Utah and Kentucky (QTY_UT, QTY_KY) or only one TOTAL_QTY (then the two warehouse columns stay empty); the total is the sum, and a capped value such as "10+" counts as 10. It is NEVER added to
    products.quantity_on_hand: that number only counts parts on our own shelf.
  * LIST PRICE: the catalog shows, sorts, filters and computes margin on the MAP price when a part has one (supplier_items.map_price,
    from the catalog file or the price file), else on the list / MSRP price.
  * `upsert_rows` is the one door the data goes through. Today the rows come from a file the person uploads. If
    Rocky Mountain turns out to have an API, its adapter should build the same dicts (see clean_row) and call
    `upsert_rows` between _begin_run and _finish_run; nothing else here changes. Any API key belongs in the
    server's environment (RMATV_API_KEY and the like), never in the browser and never in the database.
  * SFTP (sftp.rockymountainatv.com, port 22) is the live feed. The login is read from the server's environment
    (RMATV_SFTP_USER, RMATV_SFTP_PASSWORD, optionally RMATV_SFTP_HOST / RMATV_SFTP_PORT / RMATV_SFTP_HOSTKEY), never stored
    in the database or sent to the browser. Needs `paramiko` in requirements.txt. The SFTP jobs go through the same
    `upsert_rows` / `_finish_run` as the file upload, so nothing else here changes.
  * Two catalog files: the main catalog file (Inventory_TIER_1_v3.csv, full detail) and an optional second one for the OEM parts
    (Inventory_TIER_1_withOEM_v2.csv adds Honda, Kawasaki, Suzuki and CFMoto parts). Both are read in ONE run, so the retire step
    sees them together. A part in both files is taken from the main file; the second file only adds the parts the first lacks.
  * Photo links come with the catalog file (its PICTURE column) in the same run. A product made from the part gets them as
    product_photos rows and they are downloaded into our storage; the part itself keeps only the link. A row with no link
    never wipes the links already held.
  * The old hand-typed list (Distributor parts > Saved parts) is replaced by the catalog. A product's pointer to its part
    (product_kit_parts.manual_item_id) is moved to the same catalog part by /relink; nothing is deleted.
  * We do not know the exact columns of Rocky Mountain's file, so the browser reads the file, /import/detect
    guesses which column is which, and the person can correct each guess before anything is stored.

LICENSE
  Dealer cost and stock are the distributor's confidential data, handled like Automatic Distributors': every route
  here is admin-only and nothing is mounted on the public store API. All of it sits in supplier_items rows with
  supplier = 'rmatv' (and supplier_import_runs), so it can be removed in one step if the agreement ends.
"""
import base64
import csv
import hashlib
import json
import logging
import os
import re
import socket
import stat as _stat
import tempfile
import threading
from html.parser import HTMLParser
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        db.execute(text("alter table supplier_items add column if not exists fitment text"))
        db.execute(text("alter table supplier_items add column if not exists qty_utah integer"))
        db.execute(text("alter table supplier_items add column if not exists is_oem boolean not null default false"))
        db.execute(text("alter table supplier_items add column if not exists qty_kentucky integer"))
        db.execute(text("alter table supplier_items add column if not exists map_price numeric"))
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
    ("sku", "SKU", True),
    ("name", "Name / description", False),
    ("manufacturer", "Brand", False),
    ("mfr_number", "Mfr #", False),
    ("dealer_cost", "Your cost (dealer price)", False),
    ("list_price", "List / retail price (MSRP)", False),
    ("map_price", "MAP price (used as the list price when there is one)", False),
    ("qty", "Total quantity in stock (used only when the file has no Utah / Kentucky columns)", False),
    ("warehouse", "Warehouse stock in one column (like UT:3|KY:5)", False),
    ("qty_ut", "Stock in Utah", False),
    ("qty_ky", "Stock in Kentucky", False),
    ("upc", "UPC / barcode", False),
    ("unit_of_measure", "Unit of measure", False),
    ("images", "Photo link(s)", False),
    ("fitment", "Fitment (year-year:MAKE:MODEL|...)", False),
]

# Header names (case, spaces and punctuation ignored), best guess first. The lists do not overlap, so one column
# never feeds two fields. A bare "Price" or "Cost" is NOT guessed: it could be retail or ours, so the person picks it.
# a column of photo links (Rocky Mountain's own files call it PICTURE); the best guess first
_IMAGE_ALIASES = ["picture", "pictureurl", "pictureurls", "pictures", "images", "imageurls", "imagelinks", "imageurl", "imagelink", "image", "photos", "photourls", "photourl", "photo"]
# a column of fitment cells, e.g. "1979-1989:HONDA:XR80|1982-1985:HONDA:ATC 70"
_FITMENT_ALIASES = ["fitment", "fitments", "vehiclefitment", "fitmentdata", "fitsvehicles", "fits", "compatibility", "compatiblevehicles", "applications", "application"]
_ALIASES: dict[str, list[str]] = {
    "sku": ["prodno", "rmatvpartnumber", "rmatvpart", "rmatvsku", "rockymountainpartnumber", "partnumber", "partno", "partnum", "part", "sku", "itemnumber", "itemno", "item", "productnumber", "productno", "stocknumber"],
    "name": ["itemname", "productname", "name", "partname", "partdescription", "productdescription", "itemdescription", "description", "title"],
    "manufacturer": ["brand", "brandname", "manufacturer", "manufacturername", "mfr", "mfgname", "make"],
    "mfr_number": ["manufacturerpartnumber", "manufacturernumber", "mfrpartnumber", "mfrnumber", "mfrpart", "mfgpartnumber", "mfgpart", "mpn", "brandpartnumber", "vendorpartnumber", "vendorpart", "mfid"],
    "dealer_cost": ["dealercost", "dealerprice", "dealernet", "yourcost", "yourprice", "customerprice", "netprice", "wholesaleprice", "wholesale", "yourdealerprice"],
    "list_price": ["listprice", "msrp", "retailprice", "retail", "suggestedretail"],
    "qty": ["totalqty", "qtyavailable", "quantityavailable", "availableqty", "availablequantity", "qtyonhand", "onhand", "quantityonhand", "instock", "stock", "qty", "quantity", "inventory", "available"],
    "warehouse": ["warehouse", "warehouses", "warehouseqty", "warehousestock", "warehouseinventory", "stockbywarehouse", "inventorybywarehouse"],
    "qty_ut": ["qtyut", "qtyutah", "utahqty", "utqty", "utahstock", "utstock", "utah"],
    "qty_ky": ["qtyky", "qtykentucky", "kyqty", "kentuckyqty", "kentuckystock", "kystock", "kentucky"],
    "map_price": ["mapprice", "mappricing", "map", "minimumadvertisedprice"],
    "upc": ["upc", "upccode", "upcnumber", "barcode", "ean", "gtin", "upcean"],
    "unit_of_measure": ["unitofmeasure", "uom", "unit", "packagequantity"],
    "images": _IMAGE_ALIASES,
    "fitment": _FITMENT_ALIASES,
}
FIELD_KEYS = {k for k, _, _ in FIELDS}


def _head(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _with_price(aliases: dict[str, list[str]], heads: list[str]) -> dict[str, list[str]]:
    """Rocky Mountain's own layout (PRODNO and MF_ID columns): there a bare PRICE column is the dealer's cost. In any other
    file a bare Price could be retail or ours, so it is not guessed (see above)."""
    if "prodno" not in heads or "mfid" not in heads:
        return aliases
    out = dict(aliases)
    for key in ("dealer_cost", "my_cost"):
        if key in out and "price" not in out[key]:
            out[key] = [*out[key], "price"]
    return out


def detect_mapping(columns: list[str]) -> dict[str, int]:
    """The file's header row -> {field: index of the column that feeds it}. Fields with no good guess are left out."""
    heads = [_head(c) for c in columns]
    aliases = _with_price(_ALIASES, heads)
    out: dict[str, int] = {}
    used: set[int] = set()
    for field, _label, _req in FIELDS:
        for alias in aliases[field]:
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
        raise HTTPException(400, "Choose which column holds the SKU.")
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
    """'12' -> 12. A capped value such as '10+' (Rocky Mountain shows no more than that) counts as 10."""
    v = re.sub(r"[\s,+>]", "", value or "")
    try:
        return max(0, min(int(Decimal(v)), 100_000_000))
    except (InvalidOperation, ValueError):
        return 0


def _parse_warehouses(cell: str) -> tuple[int, int]:
    """'UT:3|KY:5' -> (3, 5). A warehouse left out of the cell has none (0); 'UT:10+' counts as 10. Also reads UTAH=3;KENTUCKY=5."""
    ut = ky = 0
    for part in re.split(r"[|;,/\r\n]+", cell or ""):
        m = re.match(r"\s*([A-Za-z]+)\s*[:=]\s*(.+?)\s*$", part)
        if not m:
            continue
        code, val = m.group(1).upper(), _qty(m.group(2))
        if code in ("UT", "UTAH"):
            ut += val
        elif code in ("KY", "KENTUCKY"):
            ky += val
    return ut, ky


def _split_urls(cell: str) -> list[str]:
    """A cell holding one or more photo links (separated by | ; , spaces or new lines) -> the http(s) links, in order, no repeats."""
    urls: list[str] = []
    for u in re.split(r"[|;\r\n]+|,\s*(?=https?://)|\s+(?=https?://)", (cell or "").strip()):
        u = u.strip().replace(" ", "%20")
        if u.lower().startswith(("http://", "https://")) and u not in urls:
            urls.append(u[:500])
    return urls


def _clean_urls(images) -> list[str]:
    """supplier_items.images (a list of links, or None) -> the usable links."""
    return [u.strip() for u in (images or []) if u and u.strip().lower().startswith(("http://", "https://"))]


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
    # two warehouses (Utah + Kentucky) when the file has them: the total is their sum; else the file's single total
    if "warehouse" in mapping:
        ut, ky = _parse_warehouses(get("warehouse"))
        total = min(ut + ky, 100_000_000)
    elif "qty_ut" in mapping or "qty_ky" in mapping:
        ut, ky = _qty(get("qty_ut")), _qty(get("qty_ky"))
        total = min(ut + ky, 100_000_000)
    else:
        ut = ky = None
        total = _qty(get("qty"))
    return {
        "sku": sku,
        "mfr_number": mfr,
        "mfr_number_norm": norm_part(mfr),
        "manufacturer": get("manufacturer") or None,
        "name": get("name") or None,
        "unit_of_measure": get("unit_of_measure") or None,
        "list_price": _price(get("list_price")),
        "dealer_cost": _price(get("dealer_cost")),
        "map_price": _price(get("map_price")),
        "qty_utah": ut,
        "qty_kentucky": ky,
        "qty_other": total,
        "upc": upc,
        "upc_norm": norm_upc(upc),
        "fitment": get("fitment")[:20000],   # "" when the row has none: the fitment already stored is kept
        "images": "\n".join(_split_urls(get("images"))[:6]),   # "" when the row has none: the links already stored are kept
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
    cast(:qo as integer[]), cast(:upc as text[]), cast(:upcn as text[]), cast(:imgs as text[]), cast(:fit as text[]), cast(:qu as integer[]), cast(:qk as integer[]), cast(:mp as numeric[]), cast(:oem as boolean[])
  ) as t(sku, mfr_number, mfr_number_norm, manufacturer, name, unit_of_measure, list_price, dealer_cost,
         qty_other, upc, upc_norm, imgs, fit, qu, qk, mp, oem)
), up as (
  insert into supplier_items (supplier, sku, mfr_number, mfr_number_norm, manufacturer, name, unit_of_measure,
                              list_price, dealer_cost, qty_maine, qty_indiana, qty_nevada, qty_other, upc, upc_norm,
                              last_run_id, images, images_synced_at, fitment, qty_utah, qty_kentucky, map_price, is_oem)
  select :sup, sku, mfr_number, mfr_number_norm, manufacturer, name, unit_of_measure,
         list_price, dealer_cost, 0, 0, 0, qty_other, upc, upc_norm, :run,
         case when nullif(imgs, '') is null then null else string_to_array(imgs, chr(10)) end,
         case when nullif(imgs, '') is null then null else now() end,
         nullif(fit, ''), qu, qk, mp, oem
  from incoming
  on conflict (supplier, sku) do update set
    mfr_number = excluded.mfr_number, mfr_number_norm = excluded.mfr_number_norm,
    manufacturer = excluded.manufacturer, name = excluded.name, unit_of_measure = excluded.unit_of_measure,
    list_price = excluded.list_price, dealer_cost = excluded.dealer_cost,
    qty_other = excluded.qty_other, upc = excluded.upc, upc_norm = excluded.upc_norm,
    last_seen_at = now(), last_run_id = excluded.last_run_id,
    images = coalesce(excluded.images, supplier_items.images),
    fitment = coalesce(excluded.fitment, supplier_items.fitment),
    qty_utah = excluded.qty_utah, qty_kentucky = excluded.qty_kentucky, is_oem = excluded.is_oem,
    map_price = coalesce(excluded.map_price, supplier_items.map_price),
    images_synced_at = case when excluded.images is not null then now() else supplier_items.images_synced_at end,
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
            "imgs": [r.get("images") or "" for r in rows],
            "fit": [r.get("fitment") or "" for r in rows],
            "qu": [r.get("qty_utah") for r in rows],
            "qk": [r.get("qty_kentucky") for r in rows],
            "mp": [r.get("map_price") for r in rows],
            "oem": [bool(r.get("is_oem")) for r in rows],
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


_COST = "coalesce(s.my_cost, s.dealer_cost)"   # your own price when the price file gave one, else the catalog's dealer price
_LIST = "coalesce(s.map_price, s.list_price)"   # the list price shown: the MAP price when there is one, else the MSRP
_SORTS = {
    "brand": "lower(s.manufacturer)",
    "sku": "s.sku",
    "mfr": "s.mfr_number",
    "name": "lower(s.name)",
    "uom": "s.unit_of_measure",
    "list": _LIST,
    "cost": _COST,
    "margin": f"({_LIST} - {_COST})",
    "margin_pct": f"(({_LIST} - {_COST}) / nullif({_LIST}, 0))",
    "stock": "s.qty_other",
    "utah": "s.qty_utah",
    "kentucky": "s.qty_kentucky",
    "updated": "s.updated_at",
    "seen": "s.last_seen_at",
}
# data gaps the browser can look for (same list as the Automatic Distributors catalog)
_GAPS = {
    "upc": "s.upc_norm is null",
    "mfr": "s.mfr_number is null",
    "name": "s.name is null",
    "cost": f"{_COST} is null",
    "list": f"{_LIST} is null",
    "below_cost": f"({_LIST} is not null and {_COST} is not null and {_LIST} < {_COST})",
    "photo": "s.images is null",
    "fitment": "s.fitment is null",
}


def _like(word: str) -> str:
    """A typed word as a LIKE pattern that matches it literally (a part number can hold _ or %)."""
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _filters(q, field, brand, status, stock, min_qty, max_qty, min_cost, max_cost, min_list, max_list,
             min_margin_pct, max_margin_pct, uom, missing, ours, changed_days, seen_days, warehouse=None, source=None) -> tuple[str, str, dict]:
    """(with-clause, where-clause, params) for the catalog browser. Every value is bound, never pasted in."""
    from app.kits import SINGLES_CTE, single_exists  # late import, like the other modules here

    where = ["s.supplier = :sup"]
    params: dict = {"sup": SUPPLIER}
    if status == "active":
        where.append("s.active")
    elif status == "inactive":
        where.append("not s.active")

    cols = {
        "sku": ["s.sku"], "mfr": ["s.mfr_number"], "name": ["s.name"], "brand": ["s.manufacturer"], "upc": ["s.upc"],
        "all": ["s.sku", "s.mfr_number", "s.name", "s.manufacturer", "s.upc"],
    }[field]
    for i, word in enumerate([w for w in re.split(r"\s+", (q or "").strip()) if w][:8]):
        params[f"w{i}"] = _like(word)
        parts = [f"{c} ilike :w{i}" for c in cols]
        norm = re.sub(r"[^A-Za-z0-9]", "", word).upper()
        if len(norm) >= 3 and field in ("all", "mfr", "sku"):  # dashes and spaces in part numbers are ignored, as everywhere else
            params[f"p{i}"] = f"%{norm}%"
            parts.append(f"upper(regexp_replace(s.sku, '[^A-Za-z0-9]', '', 'g')) like :p{i}")
            parts.append(f"s.mfr_number_norm like :p{i}")
        digits = re.sub(r"\D", "", word).lstrip("0")
        if len(digits) >= 8 and field in ("all", "upc"):  # a typed or scanned barcode: UPC-A and EAN-13 forms meet (same rule as norm_upc)
            params[f"u{i}"] = f"%{digits}%"
            parts.append(f"s.upc_norm like :u{i}")
        where.append("(" + " or ".join(parts) + ")")

    if brand:
        params["brand"] = brand.strip()
        where.append("lower(s.manufacturer) = lower(:brand)")
    if uom:
        params["uom"] = uom
        where.append("s.unit_of_measure = :uom")
    if stock == "in":
        where.append("s.qty_other > 0")
    elif stock == "out":
        where.append("s.qty_other = 0")
    if source == "oem":
        where.append("s.is_oem")
    elif source == "main":
        where.append("not s.is_oem")
    if warehouse == "utah":
        where.append("s.qty_utah > 0")
    elif warehouse == "kentucky":
        where.append("s.qty_kentucky > 0")
    for key, expr, op, val in (
        ("min_qty", "s.qty_other", ">=", min_qty), ("max_qty", "s.qty_other", "<=", max_qty),
        ("min_cost", _COST, ">=", min_cost), ("max_cost", _COST, "<=", max_cost),
        ("min_list", _LIST, ">=", min_list), ("max_list", _LIST, "<=", max_list),
    ):
        if val is not None:
            params[key] = val
            where.append(f"{expr} {op} :{key}")
    pct = f"(({_LIST} - {_COST}) / nullif({_LIST}, 0) * 100)"
    if min_margin_pct is not None:
        params["min_mp"] = min_margin_pct
        where.append(f"{pct} >= :min_mp")
    if max_margin_pct is not None:
        params["max_mp"] = max_margin_pct
        where.append(f"{pct} <= :max_mp")
    if missing:
        where.append(_GAPS[missing])
    with_clause = ""
    if ours:  # "ours" = the part has its SINGLE product (a kit that merely contains it does not count)
        with_clause = f"with {SINGLES_CTE} "
        where.append(single_exists("supplier") if ours == "yes" else f"not {single_exists('supplier')}")
    if changed_days:
        params["cd"] = changed_days
        where.append("s.updated_at >= now() - make_interval(days => :cd)")
    if seen_days:
        params["sd"] = seen_days
        where.append("s.last_seen_at >= now() - make_interval(days => :sd)")
    return with_clause, " and ".join(where), params


@router.get("/items")
def items(
    q: Optional[str] = Query(None, max_length=200, description="every word must match"),
    field: str = Query("all", pattern="^(all|sku|mfr|name|brand|upc)$", description="which column the words are matched against"),
    brand: Optional[str] = Query(None, max_length=120),
    status: str = Query("active", pattern="^(active|inactive|all)$"),
    stock: Optional[str] = Query(None, pattern="^(in|out)$", description="in = stock at Rocky Mountain, out = none"),
    warehouse: Optional[str] = Query(None, pattern="^(utah|kentucky)$", description="stock in this warehouse"),
    source: Optional[str] = Query(None, pattern="^(oem|main)$", description="oem = parts only the OEM file has, main = the first catalog"),
    min_qty: Optional[int] = Query(None, ge=0),
    max_qty: Optional[int] = Query(None, ge=0),
    min_cost: Optional[float] = Query(None, ge=0),
    max_cost: Optional[float] = Query(None, ge=0),
    min_list: Optional[float] = Query(None, ge=0),
    max_list: Optional[float] = Query(None, ge=0),
    min_margin_pct: Optional[float] = Query(None, description="(list - cost) / list, in percent"),
    max_margin_pct: Optional[float] = Query(None),
    uom: Optional[str] = Query(None, max_length=40),
    missing: Optional[str] = Query(None, pattern="^(upc|mfr|name|cost|list|below_cost|photo|fitment)$", description="data gaps to find"),
    ours: Optional[str] = Query(None, pattern="^(yes|no)$", description="the part already has its single product of ours"),
    changed_days: Optional[int] = Query(None, ge=1, le=3650, description="price / stock / details changed within N days"),
    seen_days: Optional[int] = Query(None, ge=1, le=3650, description="still in the feed as of N days ago"),
    sort: str = Query("brand", pattern="^(" + "|".join(_SORTS) + ")$"),
    dir: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """The catalog browser, the same as the Automatic Distributors one: any mix of filters; the totals describe everything that
    matches, not just this page. Each row also says which single product of ours it already has (if any)."""
    from app.kits import SINGLES_CTE, single_count, single_pick  # late import, like the other modules here

    with_clause, clause, params = _filters(
        q, field, brand, status, stock, min_qty, max_qty, min_cost, max_cost, min_list, max_list,
        min_margin_pct, max_margin_pct, uom, missing, ours, changed_days, seen_days, warehouse, source,
    )
    agg = db.execute(
        text(
            f"{with_clause}select count(*) as total, count(*) filter (where s.qty_other > 0) as in_stock, "
            f"count(distinct s.manufacturer) as brands, min({_COST}) as min_cost, max({_COST}) as max_cost, "
            f"avg({_COST}) as avg_cost, coalesce(sum({_COST} * s.qty_other), 0) as stock_value_at_cost, "
            f"coalesce(sum(s.qty_other), 0) as units from supplier_items s where {clause}"
        ),
        params,
    ).first()
    order = f"{_SORTS[sort]} {'desc' if dir == 'desc' else 'asc'} nulls last, s.sku, s.id"
    page = db.execute(
        text(
            f"with {SINGLES_CTE} "
            f"select s.id, s.sku, s.mfr_number, s.manufacturer, s.name, s.unit_of_measure, {_LIST} as list_price, {_COST} as dealer_cost, "
            "s.dealer_cost as standard_cost, s.my_cost, s.map_price, s.list_price as msrp, s.qty_other, s.qty_utah, s.qty_kentucky, s.is_oem, s.qty_other as qty_total, s.upc, s.images, s.active, "
            f"s.updated_at, s.last_seen_at, ({_LIST} - {_COST}) as margin, "
            f"(({_LIST} - {_COST}) / nullif({_LIST}, 0) * 100) as margin_pct, "
            f"{single_pick('supplier')} as product_id, {single_count('supplier')} as products "
            f"from supplier_items s where {clause} order by {order} limit :n offset :o"
        ),
        {**params, "n": limit, "o": offset},
    ).all()
    rows = []
    for r in page:
        d = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in r._mapping.items()}
        d["products"] = int(d["products"] or 0)
        rows.append(d)
    ids = [d["product_id"] for d in rows if d["product_id"]]
    titles = {}
    if ids:
        titles = {t.id: (t.title, t.sku_display) for t in db.execute(text("select id, title, sku_display from products where id = any(cast(:i as bigint[]))"), {"i": ids}).all()}
    for d in rows:
        d["product_title"], d["product_sku"] = titles.get(d["product_id"]) or (None, None)
    from app.sku_reserve import reserved_numbers  # a part with no product yet can still have its EI- number reserved

    held = reserved_numbers(db, "rmatv", [d["id"] for d in rows if not d["product_id"]])
    for d in rows:
        d["reserved_sku"] = held.get(d["id"]) if not d["product_id"] else None
    return {"total": agg.total, "rows": rows, "summary": {k: (float(v) if isinstance(v, Decimal) else v) for k, v in agg._mapping.items()}}


# --------------------------------------------------------------------------
# fitment: the catalog's fitment cell -> the product's fitment
# --------------------------------------------------------------------------
# A cell looks like "1979-1989:HONDA:XR80|1982-1985:HONDA:ATC 70": entries separated by |, each "years:make:model" where years is
# one year or a range. Making a product adds one product_fitment row per machine and year (the same rows the fitment tool's paste
# makes, which also feed the eBay compatible vehicles). A make, model or year is added to the fitment tool ONLY when it is not
# there yet (matched ignoring case, spaces and punctuation, with the tool's own make aliases). A new model takes the vehicle type
# most of that make's models have (left blank when the make has none; the fitment tool lets you set it later).
_FIT_YEARS = re.compile(r"^((?:19|20)\d{2})(?:\s*(?:-|\u2013|to)\s*((?:19|20)\d{2}))?$", re.I)


def _parse_fitment_cell(cell: str) -> tuple[dict, int]:
    """-> ({(make key, model key): {"make": text, "model": text, "years": set}}, the text of each entry that could not be read)."""
    from app.fitment_match import _ALIASES as MAKE_ALIASES, _norm

    out: dict = {}
    bad: list[str] = []
    for part in re.split(r"\s*\|\s*", (cell or "").strip()):
        part = part.strip()
        if not part:
            continue
        bits = [b.strip() for b in part.split(":", 2)]
        m = _FIT_YEARS.match(bits[0]) if len(bits) == 3 else None
        if not m or not bits[1] or not bits[2]:
            bad.append(part[:200])
            continue
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if b < a:
            a, b = b, a
        if b - a > 80:
            bad.append(part[:200])
            continue
        mk = _norm(MAKE_ALIASES.get(bits[1].lower(), bits[1]))
        key = (mk, _norm(bits[2]))
        if not key[0] or not key[1]:
            bad.append(part[:200])
            continue
        e = out.setdefault(key, {"make": bits[1], "model": bits[2], "years": set()})
        e["years"].update(range(a, b + 1))
    return out, bad


def _tidy_make(name: str) -> str:
    """HONDA -> Honda, KTM stays KTM (a new make only; an existing make keeps its own spelling)."""
    n = name.strip()
    return n if not n.isupper() or len(n) <= 3 else n.title()


def _apply_fitment(db: Session, product_id: int, cell: str) -> dict:
    """Add the cell's fitment to the product. Does not commit (the caller's savepoint does)."""
    from app.fitment_match import _ALIASES as MAKE_ALIASES, _norm
    from app.fitment_research import _refresh, _slugify

    parsed, bad = _parse_fitment_cell(cell)
    # unreadable_entries: the text of each entry that could not be read, so a person can add those machines by hand
    c = {"entries": len(parsed), "unreadable": len(bad), "unreadable_entries": bad[:50], "makes_created": 0, "models_created": 0, "years_created": 0, "fitment_added": 0, "already_had": 0}
    if not parsed:
        return c
    makes = {_norm(r.name): r.id for r in db.execute(text("select id, name from vehicle_makes"))}
    models_by_make: dict[int, dict[str, int]] = {}
    for e in parsed.values():
        mk_key = _norm(MAKE_ALIASES.get(e["make"].lower(), e["make"]))
        make_id = makes.get(mk_key)
        if not make_id:
            nm = _tidy_make(e["make"])
            base = _slugify(nm) or "make"
            slug, k = base, 1
            while db.execute(text("select 1 from vehicle_makes where slug = :s"), {"s": slug}).first():
                k += 1
                slug = f"{base}-{k}"
            make_id = db.execute(text("insert into vehicle_makes (name, slug) values (:n, :s) returning id"), {"n": nm, "s": slug}).scalar()
            makes[mk_key] = make_id
            c["makes_created"] += 1
        if make_id not in models_by_make:
            models_by_make[make_id] = {_norm(r.name): r.id for r in db.execute(text("select id, name from vehicle_models where make_id = :m"), {"m": make_id})}
        model_id = models_by_make[make_id].get(_norm(e["model"]))
        if not model_id:
            vt = db.execute(
                text("select vehicle_type_id from vehicle_models where make_id = :m and vehicle_type_id is not null group by vehicle_type_id order by count(*) desc limit 1"),
                {"m": make_id},
            ).scalar()
            base = _slugify(e["model"]) or "model"
            slug, k = base, 1
            while db.execute(text("select 1 from vehicle_models where make_id = :m and slug = :s"), {"m": make_id, "s": slug}).first():
                k += 1
                slug = f"{base}-{k}"
            model_id = db.execute(
                text("insert into vehicle_models (make_id, vehicle_type_id, name, slug) values (:m, :t, :n, :s) returning id"),
                {"m": make_id, "t": vt, "n": e["model"].strip(), "s": slug},
            ).scalar()
            models_by_make[make_id][_norm(e["model"])] = model_id
            c["models_created"] += 1
        ys = ",".join(str(y) for y in sorted(e["years"]))
        c["years_created"] += db.execute(
            text("insert into vehicles (model_id, year) select :m, y from unnest(cast(string_to_array(:ys, ',') as int[])) as y "
                 "where not exists (select 1 from vehicles v where v.model_id = :m and v.year = y)"),
            {"m": model_id, "ys": ys},
        ).rowcount
        added = db.execute(
            text("insert into product_fitment (product_id, vehicle_id, notes, source) "
                 "select :p, v.id, :n, 'research' from vehicles v where v.model_id = :m "
                 "and v.year = any(cast(string_to_array(:ys, ',') as int[])) "
                 "and not exists (select 1 from product_fitment pf where pf.product_id = :p and pf.vehicle_id = v.id)"),
            {"p": product_id, "m": model_id, "ys": ys, "n": "Rocky Mountain catalog"},
        ).rowcount
        c["fitment_added"] += added
        c["already_had"] += len(e["years"]) - added
    _refresh(db)
    return c



class _DetailsParser(HTMLParser):
    """Collects the text under the page's "Details" heading as paragraphs and bullets, up to the next section (reviews, fits, etc.)."""

    STOP = ("reviews", "fits", "other items of interest", "people who have viewed", "select options", "customer questions")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.on = False
        self.done = False
        self.blocks: list[tuple[str, str]] = []  # ("p" | "li", text)
        self._tag = None
        self._buf: list[str] = []
        self.full_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._tag = "skip"
        elif tag in ("h1", "h2", "h3", "h4", "h5", "p", "li", "div", "span", "strong", "b") and self._tag != "skip":
            if tag in ("h1", "h2", "h3", "h4", "h5", "p", "li"):
                self._flush()
                self._tag = tag
        elif tag == "br":
            self._flush()

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._tag = None
        elif tag in ("h1", "h2", "h3", "h4", "h5", "p", "li", "div"):
            self._flush()

    def handle_data(self, data):
        if self._tag != "skip":
            self._buf.append(data)

    def _flush(self):
        t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        tag, self._tag, self._buf = self._tag, (None if self._tag == "skip" else self._tag), []
        if tag == "skip" or not t:
            return
        self.full_text.append(t)
        low = t.lower().rstrip(":")
        if not self.on:
            if low == "details":
                self.on = True
            return
        if self.done:
            return
        if any(low.startswith(x) for x in self.STOP) or (tag and tag.startswith("h") and low != "details"):
            self.done = True
            return
        t = t.lstrip("•*-– ").strip()
        if t:
            self.blocks.append(("li" if tag == "li" or t != t.lstrip() else "p", t))


def _site_description(sku: str) -> Optional[str]:
    """BEST EFFORT: the "Details" text of the part's page on Rocky Mountain's retail site (rockymountainatvmc.com), as the HTML the
    description editor stores (<p> and <ul><li> only). Returns None on any problem (no network, blocked by their bot protection,
    no page found, page does not mention the SKU, no Details section), and the caller then uses the catalog Name. One polite request
    per search / page, short timeout, no attempt to get around a block. Turn off with RMATV_SITE_DESCRIPTION=0."""
    import html as _html
    from urllib.parse import quote
    from urllib.request import Request, urlopen

    if os.environ.get("RMATV_SITE_DESCRIPTION", "1") == "0" or not (sku or "").strip():
        return None
    base = os.environ.get("RMATV_SITE_BASE", "https://www.rockymountainatvmc.com").rstrip("/")
    try:
        for path in ("/search?term=", "/parts?term="):
            req = Request(base + path + quote(sku.strip()), headers={"User-Agent": "Mozilla/5.0 (compatible; EIMotorsports-catalog/1.0)", "Accept": "text/html"})
            with urlopen(req, timeout=6) as r:
                raw = r.read(1_500_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
            if "_Incapsula_Resource" in raw and len(raw) < 5000:  # their bot protection answered instead of the page
                log.info("rmatv site description: blocked by the site's bot protection")
                return None
            if not re.search(re.escape(sku.strip()), raw, re.I):
                continue  # not the part's page (or no result)
            ps = _DetailsParser()
            ps.feed(raw)
            ps._flush()
            if not ps.blocks:
                continue
            out, in_list = [], False
            for kind, t in ps.blocks[:20]:
                if kind == "li" and not in_list:
                    out.append("<ul>")
                    in_list = True
                elif kind == "p" and in_list:
                    out.append("</ul>")
                    in_list = False
                out.append(f"<li>{_html.escape(t)}</li>" if kind == "li" else f"<p>{_html.escape(t)}</p>")
            if in_list:
                out.append("</ul>")
            return "".join(out)[:8000] or None
    except Exception as e:  # never let this block making the product
        log.info("rmatv site description failed: %s", e)
    return None



def _title_without_part_number(brand, name, mfr, sku) -> str:
    """Title = brand + name, with the part number (manufacturer number or SKU) taken out of the name. Falls back to the SKU."""
    from app.autodist import _nice_case
    n = _nice_case(name) or ""
    for pn in sorted({(mfr or "").strip(), (sku or "").strip()}, key=len, reverse=True):
        if len(pn) >= 3:
            n = re.sub(r"[\s#(\[-]*" + re.escape(pn) + r"[\s)\]-]*", " ", n, flags=re.I)
    n = re.sub(r"\s{2,}", " ", n).strip(" -,/")
    b = _nice_case(brand) or ""
    parts = [b] if b and not n.lower().startswith(b.lower()) else []
    if n:
        parts.append(n)
    return " ".join(parts)[:200].strip() or (sku or "")


# Keyword rules for the part type guess: (regex on the catalog Name, candidate part type names in order of preference). The first
# candidate that really exists on the store's part type list (and fits the guessed category) is used, so a rule whose names are not
# on your list does nothing. Add or edit lines here; more specific phrases go BEFORE general ones (the first rule that gives a type wins).
_TYPE_RULES: list[tuple[str, list[str]]] = [
    (r"\b(gas|fuel)\s+(tank\s+)?cap\b|\btank\s+cap\b", ["Fuel Tank Cap", "Gas Cap", "Fuel Cap"]),
    (r"\bfuel\s+(pump|module)\b", ["Fuel Pump"]),
    (r"\bfuel\s+(line|hose)\b", ["Fuel Line", "Fuel Hose"]),
    (r"\bfuel\s+filter\b", ["Fuel Filter"]),
    (r"\bfuel\s+(valve|petcock)\b|\bpetcock\b", ["Fuel Valve", "Petcock"]),
    (r"\bfuel\s+injector\b", ["Fuel Injector"]),
    (r"\bcarb(uretor)?\s+(rebuild\s+)?kit\b|\bcarb(uretor)?\b", ["Carburetor", "Carburetor Kit"]),
    (r"\bair\s+filter\b|\bair\s+cleaner\b", ["Air Filter"]),
    (r"\boil\s+filter\b", ["Oil Filter"]),
    (r"\bspark\s+plug\b", ["Spark Plug"]),
    (r"\bbrake\s+pads?\b", ["Brake Pads", "Brake Pad"]),
    (r"\bbrake\s+(shoes?)\b", ["Brake Shoes", "Brake Shoe"]),
    (r"\bbrake\s+(rotor|disc)\b", ["Brake Rotor", "Brake Disc"]),
    (r"\bbrake\s+(line|hose)\b", ["Brake Line", "Brake Hose"]),
    (r"\bbrake\s+(caliper)\b|\bcaliper\b", ["Brake Caliper", "Caliper"]),
    (r"\bmaster\s+cylinder\b", ["Master Cylinder", "Brake Master Cylinder"]),
    (r"\bwheel\s+bearing\b", ["Wheel Bearing"]),
    (r"\bbearing\s+(and|&)\s+seal\b|\bbearing\s+kit\b", ["Bearing Kit", "Wheel Bearing"]),
    (r"\bball\s+joint\b", ["Ball Joint"]),
    (r"\btie\s+rod\b", ["Tie Rod", "Tie Rod End"]),
    (r"\bcv\s+(axle|joint|boot)\b|\baxle\b", ["CV Axle", "Axle"]),
    (r"\ba[- ]?arm\b|\bcontrol\s+arm\b", ["A-Arm", "Control Arm"]),
    (r"\bshock\s+absorber\b|\bshocks?\b", ["Shock Absorber", "Shock"]),
    (r"\bdrive\s+belt\b|\bcvt\s+belt\b|\bclutch\s+belt\b", ["Drive Belt", "CVT Belt"]),
    (r"\bdrive\s+chain\b|\broller\s+chain\b", ["Drive Chain", "Chain"]),
    (r"\bsprocket\b", ["Sprocket"]),
    (r"\bwater\s+pump\b", ["Water Pump"]),
    (r"\bthermostat\b", ["Thermostat"]),
    (r"\bradiator\s+(hose)\b", ["Radiator Hose"]),
    (r"\bradiator\s+cap\b", ["Radiator Cap"]),
    (r"\bradiator\b", ["Radiator"]),
    (r"\bgasket\s+(kit|set)\b|\bgasket\b", ["Gasket Kit", "Gasket"]),
    (r"\bpiston\s+kit\b|\bpiston\b", ["Piston Kit", "Piston"]),
    (r"\bstator\b", ["Stator"]),
    (r"\bregulator\b|\brectifier\b", ["Regulator/Rectifier", "Voltage Regulator", "Regulator"]),
    (r"\bignition\s+coil\b", ["Ignition Coil"]),
    (r"\bstarter\s+(motor)\b|\bstarter\b", ["Starter", "Starter Motor"]),
    (r"\bbattery\b", ["Battery"]),
    (r"\bheadlight\b|\bhead\s+light\b", ["Headlight"]),
    (r"\btail\s*light\b", ["Taillight", "Tail Light"]),
    (r"\bwinch\b", ["Winch"]),
    (r"\bsnorkel\b", ["Snorkel Kit", "Snorkel"]),
    (r"\bhandlebar\s+grips?\b|\bgrips?\b", ["Grips", "Handlebar Grips"]),
    (r"\bhandlebars?\b", ["Handlebar", "Handlebars"]),
    (r"\bthrottle\s+cable\b", ["Throttle Cable"]),
    (r"\bclutch\s+cable\b", ["Clutch Cable"]),
    (r"\bcable\b", ["Cable"]),
    (r"\bskid\s+plate\b", ["Skid Plate"]),
    (r"\bfender\b", ["Fender"]),
    (r"\bseat\s+cover\b", ["Seat Cover"]),
    (r"\bmirror\b", ["Mirror"]),
    (r"\bwindshield\b", ["Windshield"]),
    (r"\bexhaust\b|\bmuffler\b|\bsilencer\b", ["Exhaust", "Muffler"]),
    (r"\btires?\b", ["Tire", "Tires"]),
    (r"\bwheel\b|\brim\b", ["Wheel"]),
    (r"\bo[- ]?ring\b", ["O-Ring"]),
    (r"\boil\s+seal\b|\bseal\s+kit\b", ["Seal Kit", "Oil Seal"]),
    (r"\bbolt\s+kit\b|\bhardware\s+kit\b", ["Hardware Kit", "Bolt Kit"]),
]
_TYPE_RULES_RE = [(re.compile(rx, re.I), cands) for rx, cands in _TYPE_RULES]


def _guess_category_and_type(db: Session, name: str):
    """Best educated guess at (category_id, part_type name) from the part's name, using the store's own category tree and part
    type list (read at run time). Category: the category whose name has the most words in common with the name (a whole-name
    match wins). Part type: the longest type name found in the name among types that fit the category, then a type whose every
    word is in the name, then the keyword rules in _TYPE_RULES. Either may be None;
    never raises, so a failed guess cannot block making the product."""
    try:
        words = lambda t: {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z0-9]+", (t or "").lower())} - {"and", "the", "for", "with", "kit", "set", "parts", "part", "other"}
        nw = words(name)
        low = (name or "").lower()
        best, best_score = None, 0.0
        try:  # first choice: the same category rules the Automatic Distributors page uses (whole name, keyword rules, word overlap)
            from app.autodist import _load_categories, _pick_category

            best = _pick_category(_load_categories(db), name or "")
            best_score = 99.0 if best is not None else 0.0
        except Exception:
            db.rollback()
            best, best_score = None, 0.0
        for c in ([] if best is not None else db.execute(text("select id, name from categories")).all()):  # fallback: plain word overlap
            cw = words(c.name)
            if not cw:
                continue
            score = len(cw & nw) / len(cw) + (1.0 if c.name.lower() in low else 0) + 0.01 * len(cw & nw)
            if len(cw & nw) and score > best_score:
                best, best_score = c.id, score
        ptype = None
        rows = db.execute(text("select id, name from part_types")).all()
        fits = None
        if best is not None:
            fits = {r.part_type_id for r in db.execute(text(
                "with recursive a as (select id, parent_id from categories where id = :c union select c.id, c.parent_id from categories c join a on c.id = a.parent_id) "
                "select ptc.part_type_id from part_type_categories ptc join a on a.id = ptc.category_id"), {"c": best})}
        linked = {r.part_type_id for r in db.execute(text("select part_type_id from part_type_categories"))}
        ok = lambda r: fits is None or r.id in fits or r.id not in linked
        for r in sorted(rows, key=lambda r: -len(r.name)):  # 1. the whole type name is in the name
            if r.name.lower() in low and ok(r):
                ptype = r.name
                break
        if ptype is None:  # 2. every word of the type name (plural-insensitive) is in the name; most words first
            for r in sorted(rows, key=lambda r: -len(words(r.name))):
                tw = words(r.name)
                if tw and tw <= nw and ok(r):
                    ptype = r.name
                    break
        if ptype is None:  # 3. keyword rules (_TYPE_RULES): the first rule whose candidate really is on the store's list and fits
            by_lower = {r.name.lower(): r for r in rows}
            for rx, cands in _TYPE_RULES_RE:
                if not rx.search(name or ""):
                    continue
                for c in cands:
                    r = by_lower.get(c.lower())
                    if r is not None and ok(r):
                        ptype = r.name
                        break
                if ptype is not None:
                    break
        return best, ptype
    except Exception:
        db.rollback()
        log.exception("could not guess the category / part type")
        return None, None


@router.post("/items/{item_id}/make-product", status_code=201)
def make_product(item_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Make the single product for one Rocky Mountain part, the way the Automatic Distributors page does: a new product (not
    published: no price yet) with one distributor part (quantity 1) that follows the catalog part, the part's manufacturer
    number on the product, and the custom label set to the SKU when that is free. The part's photo links (from the catalog file)
    become the product's photos and are downloaded into our own storage right after (see _store_photos). Refused only when
    the part's SINGLE product already exists; a kit that contains the part does not stop it."""
    from app.admin_catalog import slugify  # late imports: those modules import this one's helpers
    from app.kits import CLS_JOIN, CLS_MAX, SINGLES_CTE, TOKEN_RE, label_token_for, parts_mpn, single_pick

    it = db.execute(
        text("select id, sku, mfr_number, manufacturer, name, dealer_cost, list_price, upc, images, fitment from supplier_items where id = :i and supplier = :s"),
        {"i": item_id, "s": SUPPLIER},
    ).first()
    if not it:
        raise HTTPException(404, "That part is not in the Rocky Mountain catalog.")
    used = db.execute(
        text(f"with {SINGLES_CTE} select p.id, p.title from supplier_items s join products p on p.id = {single_pick('supplier')} where s.id = :i"),
        {"i": item_id},
    ).first()
    if used:
        raise HTTPException(409, f"{it.sku} already has its single product: #{used.id} {used.title}")
    dist = db.execute(
        text("select id from po_distributors where name ~* 'rocky\\s*mountain\\s*atv|rmatv' order by id limit 1")
    ).scalar()
    if dist is None:
        raise HTTPException(400, "Add Rocky Mountain ATV under Distributors first; the part entry needs it.")
    title = _title_without_part_number(it.manufacturer, it.name, it.mfr_number, it.sku)
    import html as _html
    # Description: the "Details" text of the part's page on Rocky Mountain's site when it can be read; if that fails for any reason,
    # the catalog Name (as a paragraph: the editor stores HTML)
    desc = _site_description(it.sku)
    if not desc and (it.name or "").strip():
        desc = f"<p>{_html.escape(it.name.strip())}</p>"
    cat_id, ptype = _guess_category_and_type(db, it.name or "")
    base = slugify(title) or slugify(f"part {it.sku}") or "part"
    slug, k = base, 1
    while db.execute(text("select 1 from products where slug = :s"), {"s": slug}).first():
        k += 1
        slug = f"{base}-{k}"
    urls = _clean_urls(it.images)
    try:
        pid = db.execute(
            text(
                "insert into products (title, slug, manufacturer_part_number, barcode, cost, list_price, description, category_id, part_type, quantity_on_hand, condition, is_published, is_active, is_universal, show_part_number) "
                "values (:title, :slug, :mpn, :bc, :cost, :lp, :desc, :cat, :pt, 0, 'New', false, true, false, false) returning id"
            ),
            {"title": title, "slug": slug, "mpn": (it.mfr_number or "").strip() or None,
             "bc": re.sub(r"\D", "", it.upc or "") or None, "cost": it.dealer_cost,
             "lp": it.list_price, "desc": desc, "cat": cat_id, "pt": ptype},
        ).scalar()
        db.execute(
            text(
                "insert into product_kit_parts (product_id, distributor_id, supplier_item_id, part_number, description, unit_cost, quantity, sort_order) "
                "values (:p, :d, :si, :pn, :desc, :uc, 1, 0)"
            ),
            {"p": pid, "d": dist, "si": it.id, "pn": it.sku,
             "desc": (" ".join(x for x in (it.manufacturer, it.name) if x))[:250] or None, "uc": it.dealer_cost},
        )
        from app.sku_reserve import claim_for_item  # the number reserved for this catalog part (None = keep the database's)

        claim_for_item(db, pid, SUPPLIER, it.id)
        follow = parts_mpn(db, pid)
        if follow:
            db.execute(text("update products set manufacturer_part_number = :m where id = :p"), {"m": follow, "p": pid})
        if (it.manufacturer or "").strip():
            from app.manufacturers import find_or_create

            mid = find_or_create(db, it.manufacturer)
            if mid:
                db.execute(text("update products set part_manufacturer_id = :m where id = :p"), {"m": mid, "p": pid})
        # the supplier's photo links become the product's photos (links only; the files are downloaded after this commit)
        for i, url in enumerate(urls):
            db.execute(
                text("insert into product_photos (product_id, source_url, is_primary, sort_order) values (:p, :u, :pr, :o)"),
                {"p": pid, "u": url, "pr": i == 0, "o": i},
            )
        # the part's fitment cell becomes the product's fitment (makes, models and years are created only when new to the fitment tool)
        fit = None
        if (it.fitment or "").strip():
            try:
                with db.begin_nested():
                    fit = _apply_fitment(db, pid, it.fitment)
            except Exception:
                log.exception("could not add the Rocky Mountain fitment to product %s", pid)
                fit = {"error": True}
        label = None
        tok = label_token_for(it.sku or "")
        if tok and TOKEN_RE.fullmatch(tok) and CLS_JOIN not in tok.lower() and len(tok) <= CLS_MAX and not db.execute(
            text("select 1 from products o where lower(o.custom_label_sku) = lower(:t)"), {"t": tok}
        ).first():
            db.execute(text("update products set custom_label_sku = :t, updated_at = now() where id = :p"), {"t": tok, "p": pid})
            label = tok
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Could not make the product: {str(e)[:300]}")
    return {"product_id": pid, "title": title, "custom_label": label, "published": False, "category_id": cat_id, "part_type": ptype,
            "photos": len(urls), "photos_saving": _start_photos() if urls else False, "fitment": fit}


# ==========================================================================
# SFTP feed (sftp.rockymountainatv.com): the same things the Automatic Distributors FTP gives us
# ==========================================================================
# We do not yet know Rocky Mountain's file names or columns, so nothing is hard-coded: "Test connection" lists what is on the
# server, "Look at the files" shows each data file's columns, and the person says which file is the catalog, which holds their
# prices and which holds photo links (saved in supplier_sync_settings.config). Columns are matched by NAME, so a file whose
# columns move around still imports. Everything lands in supplier_items through upsert_rows, like the file upload.
from app import photo_store  # noqa: E402
from app.autodist import _DETAIL_UPDATE, _LABEL_MATCH_SQL, _MATCH, _fetch_and_store, _num, _open_downloaded  # noqa: E402

SFTP_TIMEOUT = 60
SFTP_BATCH = 2000
SCHEDULER_TICK_SECONDS = 30
_DATA_EXT = (".csv", ".txt", ".tsv", ".dat", ".zip")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp")

# the price file: our own cost, MAP and shipping facts. It only updates parts already stored; it never adds or retires parts.
_PRICE_FIELDS: list[tuple[str, str]] = [
    ("sku", "SKU"), ("my_cost", "Your cost"), ("map_price", "MAP price"), ("list_price", "List / retail price"),
    ("weight_lb", "Weight (lb)"), ("length_in", "Length (in)"), ("width_in", "Width (in)"), ("height_in", "Height (in)"),
]
_PRICE_ALIASES: dict[str, list[str]] = {
    "sku": _ALIASES["sku"],
    "my_cost": _ALIASES["dealer_cost"],
    "map_price": ["mapprice", "mappricing", "map", "minimumadvertisedprice"],
    "list_price": _ALIASES["list_price"],
    "weight_lb": ["weight", "itemweight", "actualweight", "weightlb", "weightlbs", "shipweight", "shippingweight"],
    "length_in": ["length", "itemlength", "lengthin", "depth"],
    "width_in": ["width", "itemwidth", "widthin"],
    "height_in": ["height", "itemheight", "heightin"],
}
_PHOTO_ALIASES: dict[str, list[str]] = {
    "sku": _ALIASES["sku"],
    "images": _IMAGE_ALIASES,
    **{f"image_{i}": [f"imageurl{i}", f"image{i}", f"photo{i}", f"photourl{i}", f"picture{i}", f"imagelink{i}", f"imagepath{i}"] for i in range(1, 7)},
}
_MAP_KEYS = {"catalog_map": {k for k, _, _ in FIELDS}, "oem_map": {k for k, _, _ in FIELDS}, "price_map": {k for k, _ in _PRICE_FIELDS}, "photo_map": set(_PHOTO_ALIASES)}
_FILE_KEYS = ("catalog_file", "oem_file", "price_file", "photo_file")
_CFG_KEYS = (*_FILE_KEYS, "catalog_map", "oem_map", "price_map", "photo_map")


def _sftp_settings() -> tuple[str, int, str, str]:
    host = re.sub(r"^s?ftp://", "", os.environ.get("RMATV_SFTP_HOST", "sftp.rockymountainatv.com").strip()).strip("/")
    try:
        port = int(os.environ.get("RMATV_SFTP_PORT", "22").strip() or 22)
    except ValueError:
        port = 22
    return host, port, os.environ.get("RMATV_SFTP_USER", "").strip(), os.environ.get("RMATV_SFTP_PASSWORD", "")


class _Sftp:
    """One open login. Always close() it."""

    def __init__(self, transport, client, fingerprint: str):
        self.transport, self.sftp, self.fingerprint = transport, client, fingerprint

    def close(self) -> None:
        for c in (self.sftp, self.transport):
            try:
                c.close()
            except Exception:
                pass


def _sftp_connect() -> _Sftp:
    host, port, user, pw = _sftp_settings()
    if not user or not pw:
        raise HTTPException(400, "The Rocky Mountain SFTP login is not set. Add RMATV_SFTP_USER and RMATV_SFTP_PASSWORD in the server environment.")
    try:
        import paramiko
    except ImportError:
        raise HTTPException(503, "SFTP is not installed on the server yet: add paramiko to requirements.txt and redeploy.")
    tr = None
    try:
        sock = socket.create_connection((host, port), timeout=SFTP_TIMEOUT)
        tr = paramiko.Transport(sock)
        tr.connect(username=user, password=pw)
        key = tr.get_remote_server_key()
        fp = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        pinned = os.environ.get("RMATV_SFTP_HOSTKEY", "").strip()
        if pinned and pinned.removeprefix("SHA256:").rstrip("=") != fp.removeprefix("SHA256:"):
            tr.close()
            raise HTTPException(502, f"The server's host key ({fp}) is not the one set in RMATV_SFTP_HOSTKEY. Nothing was sent. If Rocky Mountain changed servers, update that setting.")
        client = paramiko.SFTPClient.from_transport(tr)
        client.get_channel().settimeout(SFTP_TIMEOUT)
        return _Sftp(tr, client, fp)
    except HTTPException:
        raise
    except paramiko.AuthenticationException:
        if tr:
            tr.close()
        raise HTTPException(502, "Rocky Mountain's SFTP server refused the username or password.")
    except Exception as e:  # network, timeout, DNS, key exchange
        if tr:
            tr.close()
        log.warning("rmatv sftp connect failed: %s", e)
        raise HTTPException(502, f"Could not reach the Rocky Mountain SFTP server ({host}:{port}): {e}")


def _sftp_list(cli: _Sftp, folder: str = "") -> list[dict]:
    try:
        attrs = cli.sftp.listdir_attr(folder or ".")
    except (IOError, OSError):
        return []
    out = []
    for a in attrs:
        if a.filename in (".", ".."):
            continue
        out.append({"name": a.filename, "dir": _stat.S_ISDIR(a.st_mode or 0), "size": a.st_size, "mtime": a.st_mtime})
    return sorted(out, key=lambda x: (not x["dir"], x["name"].lower()))


def _join(folder: str, name: str) -> str:
    return f"{folder.rstrip('/')}/{name}" if folder else name


def _sftp_tree(cli: _Sftp, max_entries: int = 400, per_folder: int = 80) -> list[dict]:
    """Every folder and file two levels deep, so we can see what Rocky Mountain publishes (data, prices, images). A huge image
    folder is summarised instead of listed."""
    out: list[dict] = []

    def walk(folder: str, depth: int) -> None:
        kids = _sftp_list(cli, folder)
        for n, k in enumerate(kids):
            if len(out) >= max_entries:
                return
            if n >= per_folder:
                out.append({"path": _join(folder, f"… {len(kids) - per_folder} more"), "dir": False, "size": None, "mtime": None, "image": False, "more": True})
                return
            path = _join(folder, k["name"])
            out.append({"path": path, "dir": k["dir"], "size": k["size"], "mtime": k["mtime"], "image": k["name"].lower().endswith(_IMAGE_EXT), "more": False})
            if k["dir"] and depth < 2:
                walk(path, depth + 1)

    try:
        walk("", 1)
    except Exception as e:  # a folder we cannot open must not break the check
        log.warning("rmatv sftp tree stopped: %s", e)
    return out


def _signature(cli: _Sftp, path: str) -> str:
    """A cheap fingerprint (modified time | size) so a check can tell whether Rocky Mountain published something new WITHOUT
    downloading it."""
    try:
        a = cli.sftp.stat(path)
        return f"{int(a.st_mtime) if a.st_mtime else '-'}|{a.st_size if a.st_size is not None else '-'}"
    except Exception:
        return "-|-"


def _preview(cli: _Sftp, path: str, limit: int = 65536) -> bytes:
    with cli.sftp.open(path, "rb") as f:
        return f.read(limit)


def _sniff(first_line: str) -> str:
    return max([",", "\t", "|", ";"], key=lambda d: first_line.count(d))


def _preview_columns(data: bytes) -> tuple[Optional[list[str]], list[str], Optional[str]]:
    """(columns, up to two sample lines, note) from the first bytes of a file."""
    if data[:2] == b"PK":
        return None, [], "A zip file: it is unzipped during import, so its columns can only be shown after a first import."
    txt = data.decode("utf-8-sig", errors="replace")
    lines = txt.splitlines()[:-1] if "\n" in txt else txt.splitlines()  # the last line is probably cut off
    if not lines:
        return None, [], "empty"
    delim = _sniff(lines[0])
    recs = list(csv.reader(lines[:3], delimiter=delim, quoting=csv.QUOTE_MINIMAL if delim == "," else csv.QUOTE_NONE))
    return [c.strip() for c in recs[0]], [" | ".join(r)[:400] for r in recs[1:3]], None


def _detect(header: list[str], aliases: dict[str, list[str]]) -> dict[str, int]:
    heads = [_head(c) for c in header]
    out: dict[str, int] = {}
    used: set[int] = set()
    for field, names in _with_price(aliases, heads).items():
        for alias in names:
            hit = next((i for i, h in enumerate(heads) if h == alias and i not in used), None)
            if hit is not None:
                out[field] = hit
                used.add(hit)
                break
    return out


def _index_map(header: list[str], overrides: Optional[dict], aliases: dict[str, list[str]]) -> dict[str, int]:
    """Field -> column index. A column the person chose (by its header text) wins; the rest are guessed from the header names."""
    out: dict[str, int] = {}
    for field, hdr in (overrides or {}).items():
        if field in aliases and hdr:
            idx = next((i for i, h in enumerate(header) if h.strip() == str(hdr).strip()), None)
            if idx is not None:
                out[field] = idx
    guess = _detect(header, {f: a for f, a in aliases.items() if f not in out})
    taken = set(out.values())
    for f, i in guess.items():
        if i not in taken:
            out[f] = i
            taken.add(i)
    return out


def _guess_role(columns: list[str]) -> Optional[str]:
    cat = _detect(columns, {k: _ALIASES[k] for k in _ALIASES})
    price = _detect(columns, _PRICE_ALIASES)
    photo = _detect(columns, _PHOTO_ALIASES)
    has_img = any(k.startswith("image") for k in photo)
    if "sku" in cat and has_img and len(cat) <= 3:
        return "photos"
    if "sku" in cat and len(cat) >= 4:
        return "catalog"
    if "sku" in price and ("my_cost" in price or "map_price" in price) and len(cat) < 4:
        return "price"
    if "sku" in cat and has_img:
        return "photos"
    return None


# ---- settings kept in supplier_sync_settings (the same table Automatic Distributors uses) -------------------------------
_settings_ready = False


def _ensure_settings(db: Session) -> None:
    global _settings_ready
    if _settings_ready:
        return
    try:
        db.execute(text("alter table supplier_sync_settings add column if not exists config text"))
        db.execute(text("insert into supplier_sync_settings (supplier) values (:s) on conflict do nothing"), {"s": SUPPLIER})
        db.commit()
        _settings_ready = True
    except Exception:
        db.rollback()
        log.exception("rmatv: could not prepare supplier_sync_settings (is the table there? sql/autodist_sync_settings.sql)")


def _config(db: Session) -> dict:
    _ensure_settings(db)
    try:
        raw = db.execute(text("select config from supplier_sync_settings where supplier = :s"), {"s": SUPPLIER}).scalar()
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        db.rollback()
        return {}


def _sync_note(db: Session, *, summary: Optional[str] = None, error: Optional[str] = None, signature: Optional[str] = None,
               price_signature: Optional[str] = None, changed: bool = False) -> None:
    try:
        db.execute(
            text(
                "update supplier_sync_settings set last_summary = coalesce(:sm, last_summary), last_error = :er, "
                "last_signature = coalesce(:sg, last_signature), last_price_signature = coalesce(:pg, last_price_signature), "
                "last_changed_at = case when :ch then now() else last_changed_at end, updated_at = now() where supplier = :s"
            ),
            {"sm": summary, "er": (error or None) and error[:400], "sg": signature, "pg": price_signature, "ch": changed, "s": SUPPLIER},
        )
        db.commit()
    except Exception:
        db.rollback()
        log.warning("could not record the Rocky Mountain sync result")
    if summary and not error:
        from app.product_price_sync import sync_after_import  # late import, like the others in this file

        sync_after_import(db, SUPPLIER)   # the import changed the catalog: carry its list price / cost onto the linked products


def _set_run_note(db: Session, run_id: int, status: str, note: str) -> None:
    db.execute(text("update supplier_import_runs set status = :st, finished_at = now(), note = :n where id = :i"), {"st": status, "n": note[:400], "i": run_id})
    db.commit()


# ---- reading a remote file -----------------------------------------------------------------------------------------------
def _download(path: str) -> tuple[str, str]:
    """Download one remote file to a temp file. Returns (temp path, signature). The caller deletes the temp file."""
    cli = _sftp_connect()
    tmp_path = None
    try:
        sig = _signature(cli, path)
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".feed") as tmp:
                tmp_path = tmp.name
                cli.sftp.getfo(path, tmp)
        except (IOError, OSError):
            raise HTTPException(404, f"Could not read {path} on the server. Check the file's name in the SFTP settings.")
        return tmp_path, sig
    except Exception:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise
    finally:
        cli.close()


def _open_rows(tmp_path: str, aliases: dict, overrides: Optional[dict], need: tuple = ("sku",)):
    stream, delim, closer = _open_downloaded(tmp_path)
    reader = csv.reader(stream, delimiter=delim, quoting=csv.QUOTE_MINIMAL if delim == "," else csv.QUOTE_NONE)
    header = next(reader, None)
    if not header:
        closer()
        raise HTTPException(400, "That file is empty.")
    header = [h.strip() for h in header]
    mapping = _index_map(header, overrides, aliases)
    if any(n not in mapping for n in need):
        closer()
        raise HTTPException(400, "No SKU column found in that file. Its columns are: " + ", ".join(h for h in header if h)[:600])
    return mapping, reader, closer, header


def _rm_run_job(run_id: int, work, fail_msg: str) -> bool:
    """Shared wrapper for the background jobs: own DB session, error notes, temp-file cleanup."""
    gen = get_db()
    db = next(gen)
    try:
        work(db)
        return True
    except HTTPException as e:
        db.rollback()
        _set_run_note(db, run_id, "failed", str(e.detail))
        _sync_note(db, error=str(e.detail))
    except Exception:
        db.rollback()
        log.exception("rmatv sftp job failed (run %s)", run_id)
        _set_run_note(db, run_id, "failed", fail_msg)
        _sync_note(db, error=fail_msg)
    finally:
        gen.close()
    return False


def _catalog_job(run_id: int) -> bool:
    """Background: download the catalog file (and the second, OEM catalog file when one is chosen), store the parts in batches,
    then retire what left the files (with the safety net). Both files belong to ONE run, so the retire step sees them together:
    a part in either file is kept. A part in both files is taken from the first (main) file; the second only adds the parts the
    first lacks. If either file cannot be read the whole run fails and nothing is retired."""

    def work(db: Session) -> None:
        cfg = _config(db)
        path = (cfg.get("catalog_file") or "").strip()
        if not path:
            raise HTTPException(400, "Choose which file is the catalog first (SFTP feed > Which file is which).")
        oem_path = (cfg.get("oem_file") or "").strip()
        if oem_path and oem_path == path:
            raise HTTPException(400, "The catalog file and the OEM file are the same file. Choose a different file for the OEM parts, or choose none.")
        n = {"seen": 0, "skipped": 0, "new": 0, "dupes": 0, "from_oem": 0, "photos": 0}
        taken: set[str] = set()   # part numbers the first file stored in this run (only kept when there is a second file)

        def load(file_path: str, overrides: Optional[dict], first: bool) -> str:
            tmp, sig = _download(file_path)
            try:
                mapping, reader, closer, _h = _open_rows(tmp, {k: _ALIASES[k] for k in _ALIASES}, overrides)
                batch: dict[str, dict] = {}

                def flush() -> None:
                    if not batch:
                        return
                    rows = list(batch.values())
                    n["new"] += upsert_rows(db, run_id, rows)
                    n["seen"] += len(rows)
                    n["photos"] += sum(1 for r in rows if r.get("images"))
                    if not first:
                        n["from_oem"] += len(rows)
                    db.execute(text("update supplier_import_runs set rows_seen = :n, rows_new = :w, rows_skipped = :k where id = :i"),
                               {"n": n["seen"], "w": n["new"], "k": n["skipped"], "i": run_id})
                    db.commit()
                    batch.clear()

                try:
                    for rec in reader:
                        if not rec or all(not c.strip() for c in rec):
                            continue
                        row = clean_row(rec, mapping)
                        if row is None:
                            n["skipped"] += 1
                            continue
                        if first and oem_path:
                            taken.add(row["sku"])
                        elif not first and row["sku"] in taken:
                            n["dupes"] += 1   # already stored from the main file, with fuller detail
                            continue
                        row["is_oem"] = not first   # a part that only the second (OEM) file has
                        batch[row["sku"]] = row
                        if len(batch) >= SFTP_BATCH:
                            flush()
                    flush()
                finally:
                    closer()
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return sig

        sigs = [load(path, cfg.get("catalog_map"), True)]
        if oem_path:
            sigs.append(load(oem_path, cfg.get("oem_map"), False))
        sig = "+".join(sigs)
        res = _finish_run(db, run_id, n["seen"] + n["skipped"], False)
        oem_note = (f" {n['from_oem']:,} of them come from the OEM file." if oem_path else "") + (f" {n['photos']:,} came with photo links." if n["photos"] else "")
        if res.get("needs_review"):
            _sync_note(db, summary=f"Stored {n['seen']:,} parts; waiting for you to confirm retiring the missing ones.{oem_note}", signature=sig, changed=True)
        else:
            _sync_note(db, summary=f"Imported {n['seen']:,} parts ({n['new']:,} new, {res.get('retired', 0):,} retired).{oem_note}", signature=sig, changed=True)

    return _rm_run_job(run_id, work, "The SFTP import failed. See the server log. Nothing was retired.")


def _clean_price(raw: dict) -> Optional[dict]:
    sku = (raw.get("sku") or "").strip()
    if not sku or len(sku) > SKU_MAX:
        return None
    return {
        "sku": sku, "my_cost": _price(raw.get("my_cost")), "map_price": _price(raw.get("map_price")), "list_price": _price(raw.get("list_price")),
        "weight_lb": _num(raw.get("weight_lb"), 4, 100_000) or None, "length_in": _num(raw.get("length_in"), 4, 100_000) or None,
        "width_in": _num(raw.get("width_in"), 4, 100_000) or None, "height_in": _num(raw.get("height_in"), 4, 100_000) or None,
    }


# A column the file does not have (or a blank cell) leaves what we already hold alone: coalesce(new, old).
_PRICE_UPDATE = """
with incoming as (
  select * from unnest(cast(:sku as text[]), cast(:cost as numeric[]), cast(:map as numeric[]), cast(:lp as numeric[]),
                       cast(:wt as numeric[]), cast(:len as numeric[]), cast(:wid as numeric[]), cast(:hgt as numeric[]))
  as t(sku, my_cost, map_price, list_price, weight_lb, length_in, width_in, height_in)
), up as (
  update supplier_items s set
    my_cost = coalesce(i.my_cost, s.my_cost), map_price = coalesce(i.map_price, s.map_price),
    list_price = coalesce(i.list_price, s.list_price), weight_lb = coalesce(i.weight_lb, s.weight_lb),
    length_in = coalesce(i.length_in, s.length_in), width_in = coalesce(i.width_in, s.width_in),
    height_in = coalesce(i.height_in, s.height_in), price_synced_at = now(),
    updated_at = case when (s.my_cost, s.map_price, s.list_price) is distinct from
                           (coalesce(i.my_cost, s.my_cost), coalesce(i.map_price, s.map_price), coalesce(i.list_price, s.list_price))
                      then now() else s.updated_at end
  from incoming i where s.supplier = :sup and s.sku = i.sku
  returning 1
)
select count(*) from up
"""


def _update_prices(db: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    c = lambda k: [r[k] for r in rows]  # noqa: E731
    return int(db.execute(text(_PRICE_UPDATE), {"sup": SUPPLIER, "sku": c("sku"), "cost": c("my_cost"), "map": c("map_price"), "lp": c("list_price"),
                                                "wt": c("weight_lb"), "len": c("length_in"), "wid": c("width_in"), "hgt": c("height_in")}).scalar() or 0)


def _clean_photo(raw: dict) -> Optional[dict]:
    sku = (raw.get("sku") or "").strip()
    if not sku or len(sku) > SKU_MAX:
        return None
    cells = [raw.get(f"image_{i}") or "" for i in range(1, 7)] + [raw.get("images") or ""]
    urls: list[str] = []
    for cell in cells:
        for u in _split_urls(cell):
            if u not in urls:
                urls.append(u)
    return {"sku": sku, "images": "\n".join(urls[:6])}


def _update_photos(db: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    return int(db.execute(text(_DETAIL_UPDATE), {"sup": SUPPLIER, "sku": [r["sku"] for r in rows], "imgs": [r["images"] for r in rows]}).scalar() or 0)


def _update_job(run_id: int, file_key: str, map_key: str, aliases: dict, need_any: tuple, cleaner, updater, summarize, price_signature: bool = False,
                extra_file_key: Optional[str] = None) -> bool:
    """Background: download a file and use its lines to update parts we already hold (never adds or retires). With extra_file_key
    and that file chosen (the OEM catalog), its lines are used too, for the parts the first file did not cover."""

    def work(db: Session) -> None:
        cfg = _config(db)
        path = (cfg.get(file_key) or "").strip()
        if not path and file_key == "photo_file":
            path = (cfg.get("catalog_file") or "").strip()   # the catalog file may hold the photo links itself
        if not path:
            raise HTTPException(400, "Choose that file first (SFTP feed > Which file is which).")
        extra = (cfg.get(extra_file_key) or "").strip() if extra_file_key else ""
        paths = [path] + ([extra] if extra and extra != path else [])
        n = {"seen": 0, "skipped": 0, "matched": 0}
        done: set[str] = set()   # parts the first file already updated; the second file does not redo them

        def one_file(file_path: str, first: bool) -> str:
            tmp, sig = _download(file_path)
            try:
                mapping, reader, closer, header = _open_rows(tmp, aliases, cfg.get(map_key))
                if not any(k in mapping for k in need_any):
                    closer()
                    raise HTTPException(400, f"{file_path} has none of the columns this needs. Its columns are: " + ", ".join(h for h in header if h)[:500])
                batch: dict[str, dict] = {}

                def flush() -> None:
                    if not batch:
                        return
                    rows = list(batch.values())
                    n["matched"] += updater(db, rows)
                    n["seen"] += len(rows)
                    if first and len(paths) > 1:
                        done.update(batch)
                    db.execute(text("update supplier_import_runs set rows_seen = :n, rows_new = :m, rows_skipped = :k where id = :i"),
                               {"n": n["seen"], "m": n["matched"], "k": n["skipped"], "i": run_id})
                    db.commit()
                    batch.clear()

                try:
                    for rec in reader:
                        if not rec or all(not c.strip() for c in rec):
                            continue
                        row = cleaner({f: (rec[i].strip() if i < len(rec) else "") for f, i in mapping.items()})
                        if row is None:
                            n["skipped"] += 1
                            continue
                        if not first and row["sku"] in done:
                            continue
                        batch[row["sku"]] = row
                        if len(batch) >= SFTP_BATCH:
                            flush()
                    flush()
                finally:
                    closer()
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return sig

        sig = one_file(paths[0], True)
        for more in paths[1:]:
            one_file(more, False)
        if n["seen"] == 0:
            raise HTTPException(400, "That file held no usable lines.")
        db.execute(text("update supplier_import_runs set status = 'done', finished_at = now(), rows_skipped = :k, note = null where id = :i"), {"k": n["skipped"], "i": run_id})
        db.commit()
        _sync_note(db, summary=summarize(n["matched"], n["seen"], n["skipped"]), price_signature=sig if price_signature else None, changed=price_signature)

    return _rm_run_job(run_id, work, "The import failed. See the server log.")


def _price_job(run_id: int) -> bool:
    return _update_job(run_id, "price_file", "price_map", _PRICE_ALIASES, ("my_cost", "map_price", "list_price", "weight_lb"), _clean_price, _update_prices,
                       lambda m, s, k: f"Prices updated: {m:,} of the {s:,} lines in the price file matched a stored part.", price_signature=True)


def _photo_job(run_id: int) -> bool:
    return _update_job(run_id, "photo_file", "photo_map", _PHOTO_ALIASES, ("images", *[f"image_{i}" for i in range(1, 7)]), _clean_photo, _update_photos,
                       lambda m, s, k: f"Photo links updated for {m:,} parts ({s:,} lines read).", extra_file_key="oem_file")


def _sync_all(run_id: int) -> None:
    """Manual sync: the catalog first, then your prices (when a price file is chosen)."""
    if not _catalog_job(run_id):
        return
    gen = get_db()
    db = next(gen)
    try:
        if not (_config(db).get("price_file") or "").strip():
            return
        price_run = _begin_run(db, "sftp_price", "sync")
    except HTTPException:
        return
    finally:
        gen.close()
    _price_job(price_run)


def _require_login() -> None:
    _, _, u, pw = _sftp_settings()
    if not u or not pw:
        raise HTTPException(400, "The Rocky Mountain SFTP login is not set. Add RMATV_SFTP_USER and RMATV_SFTP_PASSWORD in the server environment.")


@router.get("/sftp/status")
def sftp_status():
    """Is the login set up? Contacts nothing and never returns the password."""
    host, port, user, pw = _sftp_settings()
    return {"configured": bool(user and pw), "host": host, "port": port, "user": user or None, "host_key_pinned": bool(os.environ.get("RMATV_SFTP_HOSTKEY", "").strip())}


@router.post("/sftp/check")
def sftp_check(db: Session = Depends(get_db)):
    """Log in and list what is on the server. Changes nothing."""
    cli = _sftp_connect()
    try:
        try:
            where = cli.sftp.normalize(".")
        except Exception:
            where = None
        tree = _sftp_tree(cli)
    finally:
        cli.close()
    files = [t for t in tree if not t["dir"] and not t["more"]]
    cfg = _config(db)
    return {
        "fingerprint": cli.fingerprint, "login_folder": where, "tree": tree, "image_files": sum(1 for t in tree if t["image"]),
        "data_files": [t["path"] for t in files if t["path"].lower().endswith(_DATA_EXT)],
        "other_files": [t["path"] for t in files if not t["path"].lower().endswith(_DATA_EXT) and not t["image"]][:40],
        "config": {k: cfg.get(k) for k in _FILE_KEYS},
    }


class PeekIn(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=20)


@router.post("/sftp/peek")
def sftp_peek(body: PeekIn):
    """Column names and two sample lines of each data file (the first 32 KB of each; nothing is stored). With no paths it looks at
    every data file in the top two folder levels."""
    cli = _sftp_connect()
    out = []
    try:
        paths = body.paths or [t["path"] for t in _sftp_tree(cli) if not t["dir"] and not t["more"] and t["path"].lower().endswith(_DATA_EXT)][:15]
        for path in paths:
            item: dict = {"path": path, "columns": None, "sample": [], "error": None, "note": None, "role": None, "size": None, "mtime": None}
            try:
                a = cli.sftp.stat(path)
                item["size"], item["mtime"] = a.st_size, a.st_mtime
                cols, sample, note = _preview_columns(_preview(cli, path, 32768))
                item["columns"], item["sample"] = cols, sample
                if note == "empty":
                    item["error"] = "empty"
                else:
                    item["note"] = note
                if cols:
                    item["role"] = _guess_role(cols)
                    item["catalog_mapped"] = {f: cols[i] for f, i in _detect(cols, {k: _ALIASES[k] for k in _ALIASES}).items()}
            except Exception as e:
                item["error"] = str(e)[:200]
            out.append(item)
    finally:
        cli.close()
    return {"files": out, "fingerprint": cli.fingerprint}


class ConfigIn(BaseModel):
    catalog_file: Optional[str] = Field(None, max_length=300)
    oem_file: Optional[str] = Field(None, max_length=300)   # the second catalog (OEM parts), optional
    price_file: Optional[str] = Field(None, max_length=300)
    photo_file: Optional[str] = Field(None, max_length=300)
    catalog_map: Optional[dict[str, str]] = None   # field -> the file's header text, for the columns the guess got wrong
    oem_map: Optional[dict[str, str]] = None
    price_map: Optional[dict[str, str]] = None
    photo_map: Optional[dict[str, str]] = None


@router.get("/sftp/config")
def sftp_config(db: Session = Depends(get_db)):
    cfg = _config(db)
    return {k: cfg.get(k) for k in _CFG_KEYS}


@router.put("/sftp/config")
def sftp_config_set(body: ConfigIn, db: Session = Depends(get_db)):
    cfg = _config(db)
    for k, v in body.model_dump(exclude_unset=True).items():
        if k in _FILE_KEYS:
            v = (v or "").strip()
            if v:
                cfg[k] = v
            else:
                cfg.pop(k, None)
        else:
            clean = {f: str(h).strip()[:200] for f, h in (v or {}).items() if f in _MAP_KEYS[k] and str(h).strip()}
            if clean:
                cfg[k] = clean
            else:
                cfg.pop(k, None)
    db.execute(text("update supplier_sync_settings set config = :c, updated_at = now() where supplier = :s"), {"c": json.dumps(cfg), "s": SUPPLIER})
    db.commit()
    return {k: cfg.get(k) for k in _CFG_KEYS}


@router.post("/import/sftp")
def import_sftp(db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Start pulling the catalog (then your prices) from the SFTP server. Returns at once; it runs in the background and shows in the import log."""
    _require_login()
    if not (_config(db).get("catalog_file") or "").strip():
        raise HTTPException(400, "Choose which file is the catalog first (SFTP feed > Which file is which).")
    run_id = _begin_run(db, "sftp", user.email or user.user_id)
    threading.Thread(target=_sync_all, args=(run_id,), daemon=True, name=f"rmatv-sftp-{run_id}").start()
    return {"run_id": run_id}


@router.post("/import/sftp-prices")
def import_sftp_prices(db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    _require_login()
    if not (_config(db).get("price_file") or "").strip():
        raise HTTPException(400, "Choose the price file first (SFTP feed > Which file is which).")
    run_id = _begin_run(db, "sftp_price", user.email or user.user_id)
    threading.Thread(target=_price_job, args=(run_id,), daemon=True, name=f"rmatv-price-{run_id}").start()
    return {"run_id": run_id}


@router.post("/import/sftp-photos")
def import_sftp_photos(db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Read the photo links (the photo file, else the catalog file). Manual only: photos rarely change."""
    _require_login()
    cfg = _config(db)
    if not ((cfg.get("photo_file") or "").strip() or (cfg.get("catalog_file") or "").strip()):
        raise HTTPException(400, "Choose the photo file (or the catalog file, if it holds the photo links) first.")
    run_id = _begin_run(db, "sftp_photos", user.email or user.user_id)
    threading.Thread(target=_photo_job, args=(run_id,), daemon=True, name=f"rmatv-photos-{run_id}").start()
    return {"run_id": run_id}


# ---- automatic sync (off until someone turns it on) --------------------------------------------------------------------
class ScheduleIn(BaseModel):
    enabled: Optional[bool] = None
    interval_minutes: Optional[int] = Field(None, ge=1, le=1440)


def _schedule_row(db: Session) -> dict:
    _ensure_settings(db)
    row = db.execute(
        text("select auto_enabled, interval_minutes, last_checked_at, last_changed_at, last_summary, last_error from supplier_sync_settings where supplier = :s"),
        {"s": SUPPLIER},
    ).first()
    d = dict(row._mapping) if row else {"auto_enabled": False, "interval_minutes": 60}
    d["running"] = bool(db.execute(
        text("select 1 from supplier_import_runs where supplier = :s and status = 'running' and started_at > now() - interval '30 minutes' limit 1"), {"s": SUPPLIER}).scalar())
    return d


@router.get("/sftp/schedule")
def sftp_schedule(db: Session = Depends(get_db)):
    return _schedule_row(db)


@router.put("/sftp/schedule")
def sftp_schedule_set(body: ScheduleIn, db: Session = Depends(get_db)):
    _schedule_row(db)
    if body.enabled is not None:
        if body.enabled:
            _require_login()
            if not (_config(db).get("catalog_file") or "").strip():
                raise HTTPException(400, "Choose which file is the catalog first, then turn automatic sync on.")
        db.execute(text("update supplier_sync_settings set auto_enabled = :e, last_checked_at = case when :e and not auto_enabled then null else last_checked_at end, "
                        "updated_at = now() where supplier = :s"), {"e": body.enabled, "s": SUPPLIER})
    if body.interval_minutes is not None:
        db.execute(text("update supplier_sync_settings set interval_minutes = :m, updated_at = now() where supplier = :s"), {"m": body.interval_minutes, "s": SUPPLIER})
    db.commit()
    return _schedule_row(db)


def _auto_tick() -> None:
    """Every N minutes: ask the server for each chosen file's date and size (a few bytes); download and import only what changed."""
    gen = get_db()
    db = next(gen)
    try:
        _ensure_settings(db)
        claimed = db.execute(
            text("update supplier_sync_settings set last_checked_at = now() where supplier = :s and auto_enabled "
                 "and (last_checked_at is null or last_checked_at <= now() - make_interval(mins => interval_minutes)) "
                 "returning last_signature, last_price_signature"), {"s": SUPPLIER}).first()
        db.commit()
        if claimed is None:
            return  # off, not due yet, or another copy of the server took this check
        cfg = _config(db)
        cat, price = (cfg.get("catalog_file") or "").strip(), (cfg.get("price_file") or "").strip()
        oem = (cfg.get("oem_file") or "").strip()
        if not cat:
            return
        try:
            cli = _sftp_connect()
            try:
                sig = _signature(cli, cat)
                if oem and oem != cat:
                    sig += "+" + _signature(cli, oem)   # same shape _catalog_job stores
                price_sig = _signature(cli, price) if price else None
            finally:
                cli.close()
        except HTTPException as e:
            _sync_note(db, error=str(e.detail))
            return
        inv_changed = sig == "-|-" or sig != claimed[0]
        price_changed = bool(price) and (price_sig == "-|-" or price_sig != claimed[1])
        if not inv_changed and not price_changed:
            _sync_note(db, summary="Checked: Rocky Mountain's files have not changed since the last import.")
            return
        if inv_changed:
            try:
                run_id = _begin_run(db, "sftp_auto", "automatic sync")
            except HTTPException:
                return  # an import is already running
            if not _catalog_job(run_id):
                return
        if price_changed:
            try:
                price_run = _begin_run(db, "sftp_price", "automatic sync")
            except HTTPException:
                return
            _price_job(price_run)
    except Exception:
        db.rollback()
        log.exception("rmatv automatic sync check failed")
    finally:
        gen.close()


def _scheduler_loop() -> None:
    time.sleep(60)  # let the server finish starting
    while True:
        try:
            _auto_tick()
        except Exception:
            log.exception("rmatv scheduler")
        time.sleep(SCHEDULER_TICK_SECONDS)


def _start_scheduler() -> None:
    if os.environ.get("RMATV_SCHEDULER", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    threading.Thread(target=_scheduler_loop, daemon=True, name="rmatv-auto-sync").start()


_start_scheduler()


# ---- the catalog at a glance, one part in full, our products vs the catalog -----------------------------------------------
@router.get("/overview")
def catalog_overview(db: Session = Depends(get_db)):
    """Totals, data gaps, stock, price bands, units of measure and every brand."""
    t = db.execute(
        text(
            f"""
            select count(*) as total, count(*) filter (where s.active) as active, count(*) filter (where not s.active) as retired,
                   count(*) filter (where s.active and s.qty_other > 0) as in_stock, count(*) filter (where s.active and s.qty_other = 0) as out_of_stock,
                   coalesce(sum(s.qty_other) filter (where s.active), 0) as units,
                   count(*) filter (where s.active and s.is_oem) as oem_parts,
                   count(*) filter (where s.active and s.qty_utah > 0) as in_utah, count(*) filter (where s.active and s.qty_kentucky > 0) as in_kentucky,
                   coalesce(sum(s.qty_utah) filter (where s.active), 0) as units_utah, coalesce(sum(s.qty_kentucky) filter (where s.active), 0) as units_kentucky,
                   coalesce(sum({_COST} * s.qty_other) filter (where s.active), 0) as stock_value_at_cost,
                   coalesce(sum({_LIST} * s.qty_other) filter (where s.active), 0) as stock_value_at_list,
                   count(distinct s.manufacturer) filter (where s.active) as brands,
                   count(*) filter (where s.active and s.upc_norm is null) as no_barcode,
                   count(*) filter (where s.active and s.images is null) as no_photo,
                   count(*) filter (where s.active and s.fitment is null) as no_fitment,
                   count(*) filter (where s.active and s.mfr_number is null) as no_mfr_number,
                   count(*) filter (where s.active and s.name is null) as no_name,
                   count(*) filter (where s.active and {_COST} is null) as no_cost,
                   count(*) filter (where s.active and {_LIST} is null) as no_list_price,
                   count(*) filter (where s.active and {_LIST} < {_COST}) as list_below_cost,
                   count(*) filter (where s.active and s.updated_at >= now() - interval '7 days') as changed_7d,
                   count(*) filter (where s.active and s.updated_at >= now() - interval '30 days') as changed_30d,
                   avg(({_LIST} - {_COST}) / nullif({_LIST}, 0) * 100) filter (where s.active and {_LIST} > 0 and {_COST} is not null) as avg_margin_pct,
                   max(s.last_seen_at) as last_seen
            from supplier_items s where s.supplier = :s
            """
        ),
        {"s": SUPPLIER},
    ).first()
    brands = db.execute(
        text(
            f"""
            select coalesce(s.manufacturer, '(no brand)') as brand, s.manufacturer is null as unbranded, count(*) as parts,
                   count(*) filter (where s.qty_other > 0) as in_stock, coalesce(sum(s.qty_other), 0) as units,
                   coalesce(sum({_COST} * s.qty_other), 0) as stock_value_at_cost,
                   min({_COST}) as min_cost, max({_COST}) as max_cost, avg({_COST}) as avg_cost
            from supplier_items s where s.supplier = :s and s.active group by s.manufacturer order by count(*) desc, s.manufacturer limit 3000
            """
        ),
        {"s": SUPPLIER},
    ).all()
    bands = db.execute(
        text(
            f"""
            select band, ord, count(*) as parts from (
              select case when c is null then 'No cost' when c < 5 then 'Under $5' when c < 25 then '$5 – $25' when c < 100 then '$25 – $100'
                          when c < 250 then '$100 – $250' when c < 500 then '$250 – $500' else '$500 and up' end as band,
                     case when c is null then 99 when c < 5 then 1 when c < 25 then 2 when c < 100 then 3 when c < 250 then 4 when c < 500 then 5 else 6 end as ord
              from (select {_COST} as c from supplier_items s where s.supplier = :s and s.active) x) y
            group by band, ord order by ord
            """
        ),
        {"s": SUPPLIER},
    ).all()
    uoms = db.execute(
        text("select coalesce(unit_of_measure, '(none)') as uom, unit_of_measure is null as unset, count(*) as parts from supplier_items "
             "where supplier = :s and active group by unit_of_measure order by count(*) desc limit 50"), {"s": SUPPLIER}).all()
    return {
        "totals": {k: (float(v) if isinstance(v, Decimal) else v) for k, v in t._mapping.items()},
        "brands": [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in b._mapping.items()} for b in brands],
        "price_bands": [{"band": b.band, "parts": b.parts} for b in bands],
        "units_of_measure": [dict(u._mapping) for u in uoms],
    }


@router.get("/items/{item_id}")
def item_detail(item_id: int, db: Session = Depends(get_db)):
    """Everything stored about one part, what we sell that matches it, and other parts that share its barcode or manufacturer number."""
    from app.autodist import _MPN as _P_MPN  # late import, like the other modules here

    row = db.execute(text("select * from supplier_items where id = :id and supplier = :s"), {"id": item_id, "s": SUPPLIER}).first()
    if not row:
        raise HTTPException(404, "That part isn't stored (it may have been removed).")
    item = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row._mapping.items()}
    item["standard_cost"] = item.get("dealer_cost")
    item["msrp"] = item.get("list_price")
    if item.get("map_price") is not None:
        item["list_price"] = item["map_price"]   # the MAP price is the list price when there is one
    if item.get("my_cost") is not None:
        item["dealer_cost"] = item["my_cost"]
    run = None
    if item.get("last_run_id"):
        r = db.execute(text("select id, source, status, started_at, finished_at, rows_seen from supplier_import_runs where id = :id"), {"id": item["last_run_id"]}).first()
        run = dict(r._mapping) if r else None
    upc_n = item.get("upc_norm") if item.get("upc_norm") and len(item["upc_norm"]) >= 8 else None
    mpn_n = item.get("mfr_number_norm")
    mine: list[dict] = []
    if upc_n or mpn_n:
        found = db.execute(
            text(
                f"""
                select * from (
                  select p.id, p.sku_display as sku, p.title, coalesce(p.quantity_on_hand, 0) as on_hand, p.list_price, p.cost, coalesce(p.is_active, true) as is_active,
                         nullif(btrim({_P_MPN}), '') as mfr_no,
                         nullif(ltrim(regexp_replace(coalesce(p.barcode, ''), '[^0-9]', '', 'g'), '0'), '') as upc_n,
                         nullif(upper(regexp_replace(coalesce({_P_MPN}, ''), '[^A-Za-z0-9]', '', 'g')), '') as mpn_n
                  from products p) x
                where (cast(:u as text) is not null and x.upc_n = :u) or (cast(:m as text) is not null and x.mpn_n = :m) order by x.id limit 25
                """
            ),
            {"u": upc_n, "m": mpn_n},
        ).all()
        for f in found:
            d = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in f._mapping.items()}
            d["by"] = "barcode" if upc_n and d["upc_n"] == upc_n else "part number"
            d.pop("upc_n", None)
            d.pop("mpn_n", None)
            mine.append(d)
    linked = db.execute(
        text(
            "select p.id, p.sku_display as sku, p.title, coalesce(p.quantity_on_hand, 0) as on_hand, p.list_price, p.cost, coalesce(p.is_active, true) as is_active "
            "from product_kit_parts k join products p on p.id = k.product_id where k.supplier_item_id = :i group by p.id order by p.id limit 25"
        ),
        {"i": item_id},
    ).all()
    have = {m["id"] for m in mine}
    for f in reversed(linked):  # the products that follow this very part come first
        d = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in f._mapping.items()}
        if d["id"] in have:
            for m in mine:
                if m["id"] == d["id"]:
                    m["by"] = "linked part"
        else:
            d["by"] = "linked part"
            mine.insert(0, d)
    cols = f"id, sku, mfr_number, manufacturer, name, coalesce(map_price, list_price) as list_price, {_COST.replace('s.', '')} as dealer_cost, qty_other, upc, active"

    def similar(where: str, val: str) -> list[dict]:
        rows = db.execute(text(f"select {cols} from supplier_items where supplier = :s and {where} and id <> :id order by manufacturer, sku limit 50"),
                          {"s": SUPPLIER, "v": val, "id": item_id}).all()
        return [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in r._mapping.items()} for r in rows]

    return {
        "item": item, "last_run": run, "ours": mine,
        "same_barcode": similar("upc_norm = :v", upc_n) if upc_n else [],
        "same_part_number": similar("mfr_number_norm = :v", mpn_n) if mpn_n else [],
        "brand_part_count": db.execute(text("select count(*) from supplier_items where supplier = :s and manufacturer = :b and active"),
                                       {"s": SUPPLIER, "b": item["manufacturer"]}).scalar() if item.get("manufacturer") else 0,
    }


@router.get("/match-report")
def match_report(db: Session = Depends(get_db)):
    """How many of OUR products match a Rocky Mountain part (barcode, then part number, then custom label). Changes nothing."""
    rows = db.execute(text(_MATCH), {"s": SUPPLIER}).all()
    counts = {"upc": 0, "upc_many": 0, "mpn": 0, "mpn_many": 0, "none": 0}
    for r in rows:
        counts[r.kind] += 1
    from app.autodist import _MPN as _P_MPN

    review_ids = [r.id for r in rows if r.kind in ("mpn", "mpn_many")][:300]
    review: list[dict] = []
    if review_ids:
        found = db.execute(
            text(
                f"""
                select p.id, p.sku_display as sku, p.title, s.sku as supplier_sku, s.manufacturer, s.name as supplier_name, {_COST} as effective_cost
                from products p join supplier_items s on s.supplier = :s and s.active
                 and s.mfr_number_norm = nullif(upper(regexp_replace(coalesce({_P_MPN}, ''), '[^A-Za-z0-9]', '', 'g')), '')
                where p.id = any(cast(:ids as bigint[])) order by p.id, s.manufacturer
                """
            ),
            {"s": SUPPLIER, "ids": review_ids},
        ).all()
        review = [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in f._mapping.items()} for f in found]
    none_ids = [r.id for r in rows if r.kind == "none"]
    label_rows: list[dict] = []
    if none_ids:
        found = db.execute(text(_LABEL_MATCH_SQL), {"s": SUPPLIER, "ids": none_ids}).all()
        label_rows = [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in f._mapping.items()} for f in found]
    counts["label"] = len({f["id"] for f in label_rows})
    return {"total_products": len(rows), "counts": counts, "review": review, "review_limited": len(review_ids) == 300,
            "label_review": label_rows[:400], "label_limited": len(label_rows) > 400}


# --------------------------------------------------------------------------
# photos: the supplier's links -> our own storage (same as Automatic Distributors)
# --------------------------------------------------------------------------
# A product made from a part starts with the part's photo LINKS (product_photos.source_url, no image_key). This downloads each
# one into our storage (same bucket, resizing and keys as an uploaded photo) and fills in image_key. A hidden photo is skipped.
# Only rows still missing a stored file are touched, so running it again is harmless.
_PHOTO_PENDING = (
    "from product_photos pp where pp.image_key is null and nullif(btrim(pp.source_url), '') is not null "
    "and not coalesce(pp.hidden, false) and pp.source_url not ilike '%ebay%' "
    "and exists (select 1 from product_kit_parts k join supplier_items s on s.id = k.supplier_item_id "
    "            where k.product_id = pp.product_id and s.supplier = :s)"
)
_PHOTO_WORKERS = 6
_photo_state: dict = {"running": False, "message": "", "done": 0, "total": 0, "stored": 0, "failed": 0, "errors": [], "finished_at": None}
_photo_lock = threading.Lock()


def _begin_photo_run() -> bool:
    """Claim the photo run. False when one is already going."""
    with _photo_lock:
        if _photo_state["running"]:
            return False
        _photo_state.update(running=True, message="Starting…", done=0, total=0, stored=0, failed=0, errors=[], finished_at=None)
        return True


def _store_photos(limit: int = 2000) -> dict:
    """Download the pending photos into storage. The caller has already claimed the run with _begin_photo_run().
    Never raises; the outcome is in the returned counts and in _photo_state["message"]."""
    gen = get_db()
    db = next(gen)
    stored = failed = dup = 0
    errors: list[str] = []
    total = 0
    try:
        # products made by hand and then linked to a catalog part (Distributor parts, or /relink) have no photo yet: give them
        # the part's photo links first (kits.copy_catalog_photos), so they are saved in this same run
        from app.kits import copy_catalog_photos

        if copy_catalog_photos(db, SUPPLIER):
            db.commit()
        rows = db.execute(
            text(f"select pp.id, pp.product_id, pp.source_url {_PHOTO_PENDING} order by pp.product_id, pp.sort_order, pp.id limit :lim"),
            {"s": SUPPLIER, "lim": limit},
        ).all()
        total = len(rows)
        _photo_state.update(total=total, done=0, message=f"Saving {total:,} photo{'' if total == 1 else 's'} to storage…")
        stop = threading.Event()
        with ThreadPoolExecutor(max_workers=_PHOTO_WORKERS) as pool:
            futs = {pool.submit(_fetch_and_store, r.source_url.strip(), stop): r for r in rows}
            for n, fut in enumerate(as_completed(futs), 1):
                r = futs[fut]
                try:
                    key = fut.result()
                except Exception as e:
                    failed += 1
                    if len(errors) < 5 and str(e) != "stopped":
                        errors.append(f"product {r.product_id}: {str(e)[:150]}")
                else:
                    try:
                        on_product = db.execute(
                            text("select 1 from product_photos where product_id = :p and image_key = :k and id <> :id limit 1"),
                            {"p": r.product_id, "k": key, "id": r.id},
                        ).first()
                        if on_product:
                            db.execute(text("update product_photos set hidden = true where id = :id"), {"id": r.id})
                            dup += 1
                        else:
                            db.execute(
                                text("update product_photos set image_key = :k, storage_path = :sp where id = :id"),
                                {"k": key, "sp": photo_store.full_path(key), "id": r.id},
                            )
                            stored += 1
                    except Exception as e:
                        db.rollback()
                        failed += 1
                        if len(errors) < 5:
                            errors.append(f"product {r.product_id}: {str(e)[:150]}")
                if n % 25 == 0:
                    db.commit()
                _photo_state.update(done=n, stored=stored, failed=failed, errors=errors)
        db.commit()
        left = db.execute(text(f"select count(*) {_PHOTO_PENDING}"), {"s": SUPPLIER}).scalar() or 0
        msg = f"Saved {stored:,} photo{'' if stored == 1 else 's'} to storage; {failed:,} could not be saved."
        if dup:
            msg += f" {dup:,} duplicate{'' if dup == 1 else 's'} turned off."
        if left:
            msg += f" {left:,} still waiting; run it again to continue."
        _photo_state["message"] = msg
    except Exception as e:
        db.rollback()
        log.exception("saving Rocky Mountain photos failed")
        _photo_state["message"] = f"Stopped: {str(e)[:300]}"
    finally:
        _photo_state.update(running=False, stored=stored, failed=failed, errors=errors, finished_at=time.time())
        gen.close()
    return {"total": total, "stored": stored, "failed": failed, "duplicates": dup, "message": _photo_state["message"]}


def _start_photos(limit: int = 500) -> bool:
    """Start downloading the pending photos in the background (a no-op when a run is already going). True when one was started."""
    if not _begin_photo_run():
        return False
    threading.Thread(target=_store_photos, args=(limit,), daemon=True, name="rmatv-store-photos").start()
    return True


class StorePhotosIn(BaseModel):
    limit: int = Field(500, ge=1, le=5000)


@router.post("/photos/store")
def photos_store(body: StorePhotosIn, user: AuthUser = Depends(require_admin)):
    """Download the photos of products made from Rocky Mountain parts into our storage. Runs in the background."""
    if not _start_photos(body.limit):
        raise HTTPException(409, "Photos are already being saved.")
    return {"running": True}


@router.get("/photos/status")
def photos_status(db: Session = Depends(get_db)):
    from app.kits import catalog_photos_waiting

    pending = (db.execute(text(f"select count(*) {_PHOTO_PENDING}"), {"s": SUPPLIER}).scalar() or 0) + catalog_photos_waiting(db, SUPPLIER)
    return {**{k: v for k, v in _photo_state.items()}, "pending": int(pending)}


# --------------------------------------------------------------------------
# carrying the product links over to the catalog parts
# --------------------------------------------------------------------------
# Before the catalog file was loaded, Rocky Mountain parts were typed by hand ("saved parts"); a product points at its part through
# product_kit_parts (manual_item_id for a saved part, supplier_item_id for a catalog part). When the catalog replaces that list,
# each pointer moves to the catalog part that is the SAME part, matched in this order to ONE active catalog part:
#   1. the same part number (dashes, spaces and case ignored)
#   2. the same manufacturer number (only when exactly one part has it; "brand differs" matches are only moved on request)
#   3. the same barcode (only when exactly one part has it)
# More than one candidate, or none, leaves the pointer exactly as it is. The product's own part number, custom label and price are
# never touched; only which part it points at. Nothing is deleted.
_RELINK_SQL = r"""
with dist as (select id from po_distributors where name ~* 'rocky\s*mountain\s*atv|rmatv'),
tgt as materialized (
  select id, upper(regexp_replace(sku, '[^A-Za-z0-9]', '', 'g')) as sku_n, mfr_number_norm as mfr_n,
         case when length(coalesce(upc_norm, '')) >= 8 then upc_norm end as upc_n, manufacturer as brand
  from supplier_items where supplier = :s and active
),
src as (
  select k.id as kid, k.product_id, 'saved part' as kind, m.part_number as old_number, m.manufacturer as brand,
         nullif(upper(regexp_replace(coalesce(m.part_number, ''), '[^A-Za-z0-9]', '', 'g')), '') as sku_n,
         nullif(upper(regexp_replace(coalesce(m.mfr_number, ''), '[^A-Za-z0-9]', '', 'g')), '') as mfr_n,
         cast(null as text) as upc_n
  from product_kit_parts k join manual_catalog_items m on m.id = k.manual_item_id
  where k.supplier_item_id is null and m.distributor_id in (select id from dist)
  union all
  select k.id, k.product_id, 'dropped part', o.sku, o.manufacturer,
         nullif(upper(regexp_replace(o.sku, '[^A-Za-z0-9]', '', 'g')), ''), o.mfr_number_norm,
         case when length(coalesce(o.upc_norm, '')) >= 8 then o.upc_norm end
  from product_kit_parts k join supplier_items o on o.id = k.supplier_item_id and o.supplier = :s and not o.active
),
by_sku as (select s.kid, array_agg(t.id order by t.id) as ids from src s join tgt t on s.sku_n is not null and t.sku_n = s.sku_n group by s.kid),
by_mfr as (
  select s.kid, array_agg(t.id order by t.id) as ids,
         bool_and(s.brand is null or t.brand is null or lower(btrim(s.brand)) = lower(btrim(t.brand))) as agree
  from src s join tgt t on s.mfr_n is not null and t.mfr_n = s.mfr_n group by s.kid),
by_upc as (select s.kid, array_agg(t.id order by t.id) as ids from src s join tgt t on s.upc_n is not null and t.upc_n = s.upc_n group by s.kid)
select s.kid, s.product_id, s.kind, s.old_number,
       case when cardinality(bs.ids) = 1 then bs.ids[1]
            when bs.ids is null and cardinality(bm.ids) = 1 then bm.ids[1]
            when bs.ids is null and bm.ids is null and cardinality(bu.ids) = 1 then bu.ids[1] end as target_id,
       case when cardinality(bs.ids) = 1 then 'part number'
            when bs.ids is null and cardinality(bm.ids) = 1 then case when bm.agree then 'manufacturer number' else 'manufacturer number, brand differs' end
            when bs.ids is null and bm.ids is null and cardinality(bu.ids) = 1 then 'barcode' end as matched_by,
       coalesce(cardinality(bs.ids), cardinality(bm.ids), cardinality(bu.ids), 0) as candidates
from src s left join by_sku bs on bs.kid = s.kid left join by_mfr bm on bm.kid = s.kid left join by_upc bu on bu.kid = s.kid
"""
_LOOSE = "manufacturer number, brand differs"


def _relink_plan(db: Session) -> list[dict]:
    rows = db.execute(
        text(
            f"with plan as ({_RELINK_SQL}) "
            "select r.kid, r.product_id, r.kind, r.old_number, r.target_id, r.matched_by, r.candidates, "
            "p.sku_display as product_sku, p.title as product_title, t.sku as new_sku, t.manufacturer as new_brand, t.name as new_name "
            "from plan r join products p on p.id = r.product_id left join supplier_items t on t.id = r.target_id order by r.kid"
        ),
        {"s": SUPPLIER},
    ).all()
    return [dict(r._mapping) for r in rows]


@router.get("/relink")
def relink_preview(db: Session = Depends(get_db)):
    """What carrying the product links over to the catalog would do. Changes nothing."""
    plan = _relink_plan(db)
    by: dict[str, int] = {}
    for r in plan:
        key = r["matched_by"] or ("several parts match" if r["candidates"] > 1 else "no matching part")
        by[key] = by.get(key, 0) + 1
    ready = [r for r in plan if r["target_id"] and r["matched_by"] != _LOOSE]
    loose = [r for r in plan if r["matched_by"] == _LOOSE]
    sample = sorted(plan, key=lambda r: (r["target_id"] is not None and r["matched_by"] != _LOOSE, r["kid"]))[:300]
    return {
        "total": len(plan), "from_saved_parts": sum(1 for r in plan if r["kind"] == "saved part"),
        "from_dropped_parts": sum(1 for r in plan if r["kind"] == "dropped part"),
        "ready": len(ready), "brand_differs": len(loose), "left_alone": len(plan) - len(ready) - len(loose),
        "by": by,
        "number_changes": sum(1 for r in ready if re.sub(r"[^A-Za-z0-9]", "", (r["old_number"] or "")).upper() != re.sub(r"[^A-Za-z0-9]", "", (r["new_sku"] or "")).upper()),
        "sample": [
            {"product_id": r["product_id"], "product_sku": r["product_sku"], "product_title": r["product_title"], "kind": r["kind"],
             "old_number": r["old_number"], "new_sku": r["new_sku"], "new_brand": r["new_brand"], "new_name": r["new_name"],
             "matched_by": r["matched_by"], "candidates": r["candidates"]}
            for r in sample
        ],
        "limited": len(plan) > len(sample),
    }


class RelinkIn(BaseModel):
    include_brand_differs: bool = False   # also move the matches made by manufacturer number alone where the brands differ


@router.post("/relink")
def relink_apply(body: RelinkIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Point each product's distributor part at the catalog part that is the same part (see the rules above). Safe to run again."""
    plan = _relink_plan(db)
    move = [r for r in plan if r["target_id"] and (r["matched_by"] != _LOOSE or body.include_brand_differs)]
    if move:
        try:
            db.execute(
                text(
                    "update product_kit_parts k set supplier_item_id = v.tid, manual_item_id = null "
                    "from unnest(cast(:kids as bigint[]), cast(:tids as bigint[])) as v(kid, tid) where k.id = v.kid"
                ),
                {"kids": [r["kid"] for r in move], "tids": [r["target_id"] for r in move]},
            )
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(500, f"Could not move the links: {str(e)[:300]}")
    # the moved products that have no photo get their new catalog part's photos, saved into our storage in the background
    photos = 0
    if move:
        from app.kits import copy_catalog_photos

        photos = copy_catalog_photos(db, SUPPLIER, sorted({r["product_id"] for r in move}))
        db.commit()
    return {"moved": len(move), "products": len({r["product_id"] for r in move}), "left_alone": len(plan) - len(move),
            "photos_added": photos, "photos_saving": _start_photos() if photos else False}


@router.post("/products/sync-prices")
def sync_product_prices(db: Session = Depends(get_db)):
    """Copy the catalog's list price and cost onto every single-part website product made from this catalog right now (this also
    runs by itself after every feed import). Kits are never touched. A blank catalog value never wipes a price already on a product."""
    from app.product_price_sync import sync_linked_product_prices

    try:
        res = sync_linked_product_prices(db, SUPPLIER)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Could not update the product prices: {str(e)[:300]}")
    return res
