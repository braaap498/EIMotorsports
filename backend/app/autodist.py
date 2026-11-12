"""
Automatic Distributors (Autodist) supplier data.

Mounted at /admin/autodist/* (owner/admin only).

  GET  /summary                what is stored, and the last imports
  POST /import/start           begin an import (the browser then sends the file in pieces)
  POST /import/chunk           one piece of the feed file
  POST /import/finish          check the whole file arrived, then retire parts that left the feed
  GET  /api/status             is the ordering API set up, and which server (live or test) it points at
  POST /api/avail              live availability and our price for one part from the ordering API
  POST /api/order/quote        price and shipping options for an order (creates nothing)
  POST /api/order/place        places an order (confirm required; real orders also need AUTOMATIC_DIST_ALLOW_LIVE_ORDERS)
  GET  /api/orders             the orders placed from this screen, with their status and tracking numbers
  POST /api/orders/{id}/tracking   ask Automatic Distributors for this order's status and tracking now
  POST /api/orders/{id}/cancel     cancel the order with Automatic Distributors (refused once it is invoiced)
  POST /api/orders/refresh-tracking  the same for every recent order that is not finished (runs from a button)
  GET  /api/invoices           stored invoices: search, filters, sort, paging (also /export CSV, /{id}, POST /sync)
  GET  /api/pos                open purchase orders to Automatic Distributors that can be ordered from here
  GET  /api/pos/{id}           one of them: lines matched to Automatic Distributors part numbers, ship-to split into fields
  GET  /opportunities          ranked lists of the parts most worth selling (margin, stock, our own sales)
  GET  /ftp/status             is the Automatic Distributors FTP login set up on the server (nothing is contacted)
  POST /ftp/check              log in to the Automatic Distributors FTP server, list the folders, preview the inventory file
  POST /ftp/peek               column names and sample lines of each Automatic Distributors data file (nothing is stored)
  POST /import/ftp-photos      read the photo links from the big All Products file (manual only; runs in the background)
  POST /import/ftp             pull the live inventory file, then our own prices (priceupdate.txt), from the FTP server (runs in the background)
  GET  /ftp/schedule           the automatic-sync setting (off by default) and what the last check found
  PUT  /ftp/schedule           turn automatic sync on/off and set how often the server checks (minutes); also the automatic order tracking check
  GET  /items                  browse / search / filter / sort the stored supplier parts (see items() for every filter)
  GET  /items/{id}             one part in full: every stored field, our matching products, same-barcode / same-number parts
  GET  /catalog/overview       the whole catalog at a glance: totals, data gaps, stock by warehouse, brands, price bands
  GET  /match-report           how many of OUR products match a supplier part (changes nothing)

HOW IT FITS
  * Supplier stock lives in its own tables (supplier_items). It is NEVER added to products.quantity_on_hand:
    that number drives reservations, picking and reordering (app/stock.py) and must only count parts on
    our shelf.
  * `upsert_rows` is the one door the data goes through. Today the rows come from the feed file; when the
    Automatic Distributors API is wired in, its adapter should build the same dicts (see clean_row) and call
    `upsert_rows` - nothing else here changes. The API key belongs in the server's settings
    (autodist_api_key in app/config.py), never in the browser.
  * The FTP login is read from Render environment variables (AUTODIST_FTP_USER, AUTODIST_FTP_PASSWORD, and
    optionally AUTODIST_FTP_HOST, AUTODIST_FTP_INVENTORY_FILE, AUTODIST_FTP_TLS), never stored in the database
    or sent to the browser.

LICENSE (Product Data License Agreement)
  This is licensed data. Dealer cost and stock are confidential (7.3) and must not be made available in
  bulk (4(c)). So: every route here is admin-only, nothing here is mounted on the public store API, and
  the tables are closed to the public Supabase roles (see sql/autodist_tables.sql). All Automatic Distributors data sits
  in supplier_items / supplier_import_runs so it can be deleted in one step if the agreement ends (11.2).
"""
import csv
import ftplib
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date as _date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
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

# Column names in Automatic Distributors' Inventory Feed file -> our names.
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


def _finish_run(db: Session, run_id: int, rows_sent: int, force: bool) -> dict:
    """Check the whole file arrived, then retire the parts that left the feed. Shared by the browser upload and the FTP pull."""
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
        text(
            "update supplier_import_runs set status = 'done', finished_at = now(), rows_deactivated = :d, note = null where id = :i"
        ),
        {"d": retired, "i": run_id},
    )
    db.commit()
    return {"ok": True, "needs_review": False, "stored": run.rows_seen, "new": run.rows_new, "skipped": run.rows_skipped, "retired": retired}


@router.post("/import/finish")
def import_finish(body: FinishIn, db: Session = Depends(get_db)):
    return _finish_run(db, body.run_id, body.rows_sent, body.force)


# --------------------------------------------------------------------------
# pulling the live inventory file from Automatic Distributors' FTP server
# --------------------------------------------------------------------------
# The file "all products price and inventory" lists stock per warehouse in columns F, G and J
# (Warehouse_1 = Maine, decommissioned; Warehouse_2 = Indiana; Warehouse_3 = Nevada). Header names are matched
# loosely (case, spaces and underscores ignored) so the older "Maine_Quantity" style also works.
_ALIASES = {
    "sku": ["sku", "autodistsku", "autodistnumber", "itemsku"],
    "mfr_number": ["manufacturernumber", "mfrnumber", "manufacturerpartnumber", "mfrpartnumber", "mfrpart"],
    "manufacturer": ["manufacturer", "manufacturername", "brand", "brandname"],
    "name": ["itemname", "name", "itemdescription", "description"],
    "unit_of_measure": ["unitofmeasure", "uom"],
    "list_price": ["listprice", "retailprice", "retail"],
    "dealer_cost": ["regulardealerprice", "standarddealerprice", "dealerprice", "dealercost"],
    "qty_maine": ["mainequantity", "warehouse1quantity", "warehouse1qty"],
    "qty_indiana": ["indianaquantity", "warehouse2quantity", "warehouse2qty"],
    "qty_nevada": ["nevadaquantity", "warehouse3quantity", "warehouse3qty"],
    "upc": ["upc", "upccode", "barcode"],
}
_FTP_BATCH = 2000
_FTP_TIMEOUT = 60


def _norm_header(h: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def map_headers(header: list[str], aliases: Optional[dict] = None) -> dict[str, int]:
    """Header cells -> {our field: column index}. Raises 400 when there is no SKU column."""
    by_norm = {}
    for i, h in enumerate(header):
        by_norm.setdefault(_norm_header(h), i)
    out: dict[str, int] = {}
    for field, names in (aliases or _ALIASES).items():
        for nm in names:
            if nm in by_norm:
                out[field] = by_norm[nm]
                break
    if "sku" not in out:
        raise HTTPException(
            400,
            "No SKU column found in the file. Its columns are: " + ", ".join(h for h in header if h)[:600],
        )
    return out


def _ftp_settings() -> tuple[str, str, str]:
    host = re.sub(r"^ftps?://", "", os.environ.get("AUTODIST_FTP_HOST", "ftp.autodist.com").strip()).strip("/")
    return host, os.environ.get("AUTODIST_FTP_USER", "").strip(), os.environ.get("AUTODIST_FTP_PASSWORD", "")


def _ftp_connect() -> ftplib.FTP:
    host, user, pw = _ftp_settings()
    if not user or not pw:
        raise HTTPException(400, "The Automatic Distributors FTP login is not set. Add AUTODIST_FTP_USER and AUTODIST_FTP_PASSWORD in the Render environment.")
    use_tls = os.environ.get("AUTODIST_FTP_TLS", "").strip().lower() in ("1", "true", "yes")
    ftp: ftplib.FTP = ftplib.FTP_TLS(timeout=_FTP_TIMEOUT) if use_tls else ftplib.FTP(timeout=_FTP_TIMEOUT)
    try:
        ftp.connect(host, 21)
        ftp.login(user, pw)
        if use_tls:
            ftp.prot_p()  # type: ignore[attr-defined]
        ftp.set_pasv(True)
    except ftplib.error_perm:
        raise HTTPException(502, "Automatic Distributors' FTP server refused the username or password.")
    except Exception as e:  # network, timeout, DNS
        log.warning("autodist ftp connect failed: %s", e)
        raise HTTPException(502, f"Could not reach the Automatic Distributors FTP server ({host}): {e}")
    return ftp


def _ftp_raw_list(ftp: ftplib.FTP, folder: str = "") -> list[str]:
    """The server's own directory listing lines, exactly as it sends them (for diagnosis and as a last fallback)."""
    lines: list[str] = []
    try:
        ftp.retrlines(("LIST " + folder).strip(), lines.append)
    except (ftplib.error_perm, ftplib.error_reply):
        pass
    return lines


def _ftp_list(ftp: ftplib.FTP, folder: str = "") -> list[tuple[str, Optional[bool], Optional[int]]]:
    """[(name, is_dir or None when unknown, size or None)] for a folder."""
    out: list[tuple[str, Optional[bool], Optional[int]]] = []
    try:
        for name, facts in ftp.mlsd(folder or ""):
            t = facts.get("type", "")
            if t in ("cdir", "pdir"):
                continue
            size = int(facts["size"]) if facts.get("size", "").isdigit() else None
            out.append((name, t == "dir", size))
        if out:
            return out
    except (ftplib.error_perm, ftplib.error_reply, AttributeError):
        pass
    try:
        for full in ftp.nlst(folder or ""):
            out.append((full.replace("\\", "/").rstrip("/").split("/")[-1], None, None))
    except ftplib.error_perm:  # "550 No files found" on an empty folder
        pass
    if out:
        return out
    for line in _ftp_raw_list(ftp, folder):  # unix "ls -l" style lines, or DOS style
        parts = line.split(None, 8)
        if len(parts) >= 9 and line[:1] in "d-l":
            name = parts[8].split(" -> ")[0]
            if name not in (".", ".."):
                out.append((name, line[0] == "d", int(parts[4]) if parts[4].isdigit() else None))
        elif len(parts) >= 4 and re.match(r"\d\d-\d\d-\d\d", parts[0]):  # "11-03-26 10:15AM <DIR> name"
            rest = line.split(None, 3)[3] if len(line.split(None, 3)) > 3 else ""
            is_dir = "<DIR>" in line
            name = rest.replace("<DIR>", "").strip() if is_dir else (line.split(None, 3)[3] if len(line.split(None, 3)) > 3 else "")
            if is_dir:
                name = line.split("<DIR>", 1)[1].strip()
            if name:
                out.append((name, is_dir, None))
    return out


def _join(folder: str, name: str) -> str:
    return f"{folder.rstrip('/')}/{name}" if folder else name


def _is_inventory_name(name: str) -> bool:
    k = _norm_header(name)
    return "priceupdate" not in k and "allproducts" in k and "inventory" in k and "price" in k


def _find_inventory_file(ftp: ftplib.FTP) -> str:
    """The path of the live inventory file: AUTODIST_FTP_INVENTORY_FILE if set, else the file called
    'all products price and inventory' (any spacing / underscores / extension) in the top folder or in a folder
    directly under it."""
    forced = os.environ.get("AUTODIST_FTP_INVENTORY_FILE", "").strip()
    if forced:
        return forced
    top = _ftp_list(ftp, "")
    hits = [n for n, d, _sz in top if d is not True and _is_inventory_name(n)]
    seen_folders = [n for n, d, _sz in top if d is not False and "." not in n]
    seen_files = [n for n, d, _sz in top if d is False or (d is None and "." in n)]
    if not hits:
        for folder in [n for n, d, _sz in top if d is True or (d is None and "." not in n)][:12]:
            try:
                sub = _ftp_list(ftp, folder)
            except Exception:
                continue
            for n, d, _sz in sub:
                if d is not True and _is_inventory_name(n):
                    hits.append(_join(folder, n))
    if not hits:
        raise HTTPException(
            404,
            "Could not find the 'all products price and inventory' file on the server. "
            f"Folders: {', '.join(seen_folders[:30]) or '(none)'}. Files in the top folder: {', '.join(seen_files[:30]) or '(none)'}. "
            "Set AUTODIST_FTP_INVENTORY_FILE in Render to its exact path.",
        )
    hits.sort(key=lambda n: (0 if n.lower().endswith((".csv", ".txt", ".zip")) else 1, len(n)))
    return hits[0]


def _preview_remote(ftp: ftplib.FTP, path: str, limit: int = 65536) -> bytes:
    """The first bytes of a remote file, without downloading all of it."""
    conn = ftp.transfercmd("RETR " + path)
    data = b""
    try:
        while len(data) < limit:
            chunk = conn.recv(min(8192, limit - len(data)))
            if not chunk:
                break
            data += chunk
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            ftp.voidresp()
        except Exception:
            pass
    return data


def _sniff_delimiter(first_line: str) -> str:
    return max([",", "\t", "|", ";"], key=lambda d: first_line.count(d))


def _rows_from_stream(text_stream, mapping: Optional[dict] = None, delimiter: Optional[str] = None,
                      aliases: Optional[dict] = None, cleaner=None, quoting: int = csv.QUOTE_MINIMAL):
    """Yield (clean_row | None) per data line of a feed (None = unreadable line)."""
    reader = csv.reader(text_stream, delimiter=delimiter or ",", quoting=quoting)
    header = next(reader, None)
    if header is None:
        return
    mp = mapping or map_headers(header, aliases)
    width = len(header)
    clean = cleaner or clean_row
    for rec in reader:
        if not rec or all(not c.strip() for c in rec):
            continue
        if len(rec) != width:
            yield None
            continue
        yield clean({f: rec[i] for f, i in mp.items()})


def _open_downloaded(tmp_path: str):
    """The downloaded file as (text stream, delimiter, closer). Unzips a .zip first."""
    if zipfile.is_zipfile(tmp_path):
        zf = zipfile.ZipFile(tmp_path)
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if not names:
            raise HTTPException(400, "The downloaded zip file is empty.")
        raw = zf.open(names[0])
        closer = lambda: (raw.close(), zf.close())  # noqa: E731
    else:
        raw = open(tmp_path, "rb")
        closer = raw.close
    text_stream = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
    first = text_stream.readline()
    delim = _sniff_delimiter(first)
    text_stream.seek(0)
    return text_stream, delim, closer


def _set_run_note(db: Session, run_id: int, status: str, note: str) -> None:
    db.execute(
        text("update supplier_import_runs set status = :st, finished_at = now(), note = :n where id = :i"),
        {"st": status, "n": note[:400], "i": run_id},
    )
    db.commit()


def _remote_signature(ftp: ftplib.FTP, path: str) -> str:
    """A cheap fingerprint of the remote file (its modified time and size) so a check can tell whether Automatic Distributors
    has published something new WITHOUT downloading it. '-|-' means the server told us neither."""
    mtime = size = None
    try:
        resp = ftp.sendcmd("MDTM " + path)
        if resp.startswith("213"):
            mtime = resp.split()[-1]
    except Exception:
        pass
    try:
        ftp.voidcmd("TYPE I")
        size = ftp.size(path)
    except Exception:
        pass
    return f"{mtime or '-'}|{size if size is not None else '-'}"


def _sync_note(db: Session, *, summary: Optional[str] = None, error: Optional[str] = None,
               signature: Optional[str] = None, price_signature: Optional[str] = None, changed: bool = False) -> None:
    """Record what the last check / import found. Never lets a missing settings table break an import."""
    try:
        db.execute(
            text(
                "update supplier_sync_settings set last_summary = coalesce(:sm, last_summary), last_error = :er, "
                "last_signature = coalesce(:sg, last_signature), last_price_signature = coalesce(:pg, last_price_signature), "
                "last_changed_at = case when :ch then now() else last_changed_at end, updated_at = now() "
                "where supplier = :s"
            ),
            {"sm": summary, "er": (error or None) and error[:400], "sg": signature, "pg": price_signature, "ch": changed, "s": SUPPLIER},
        )
        db.commit()
    except Exception:
        db.rollback()
        log.warning("could not record the Automatic Distributors sync result (is sql/autodist_sync_settings.sql applied?)")


def _ftp_job(run_id: int) -> bool:
    """Background: download the live file, store it in batches, then retire what left the feed."""
    gen = get_db()
    db = next(gen)
    tmp_path = None
    try:
        ftp = _ftp_connect()
        try:
            path = _find_inventory_file(ftp)
            signature = _remote_signature(ftp, path)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".feed") as tmp:
                tmp_path = tmp.name
                ftp.retrbinary("RETR " + path, tmp.write, blocksize=65536)
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

        stream, delim, closer = _open_downloaded(tmp_path)
        try:
            seen = skipped = new_total = 0
            batch: dict[str, dict] = {}

            def flush():
                nonlocal seen, new_total
                if not batch:
                    return
                rows = list(batch.values())
                new_rows = upsert_rows(db, run_id, rows)
                seen += len(rows)
                new_total += new_rows
                db.execute(
                    text(
                        "update supplier_import_runs set rows_seen = :n, rows_new = :w, rows_skipped = :k where id = :i"
                    ),
                    {"n": seen, "w": new_total, "k": skipped, "i": run_id},
                )
                db.commit()
                batch.clear()

            for row in _rows_from_stream(stream, delimiter=delim):
                if row is None:
                    skipped += 1
                    continue
                batch[row["sku"]] = row
                if len(batch) >= _FTP_BATCH:
                    flush()
            flush()
        finally:
            closer()
        result = _finish_run(db, run_id, seen + skipped, False)
        if result.get("needs_review"):
            _sync_note(db, summary=f"Stored {seen:,} parts; waiting for you to confirm retiring the missing ones.",
                       signature=signature, changed=True)
        else:
            _sync_note(db, summary=f"Imported {seen:,} parts ({new_total:,} new, {result.get('retired', 0):,} retired).",
                       signature=signature, changed=True)
        return True
    except HTTPException as e:
        db.rollback()
        _set_run_note(db, run_id, "failed", str(e.detail))
        _sync_note(db, error=str(e.detail))
    except Exception:
        db.rollback()
        log.exception("autodist ftp import failed (run %s)", run_id)
        _set_run_note(db, run_id, "failed", "The FTP import failed. See the server log. Nothing was retired.")
        _sync_note(db, error="The FTP import failed. See the server log.")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        gen.close()
    return False


@router.get("/ftp/status")
def ftp_status():
    """Is the login set up? Contacts nothing and never returns the password."""
    host, user, pw = _ftp_settings()
    return {
        "configured": bool(user and pw),
        "host": host,
        "user": user or None,
        "inventory_file": os.environ.get("AUTODIST_FTP_INVENTORY_FILE", "").strip() or None,
        "tls": os.environ.get("AUTODIST_FTP_TLS", "").strip().lower() in ("1", "true", "yes"),
    }


_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp")


def _ftp_tree(ftp: ftplib.FTP, max_entries: int = 400) -> list[dict]:
    """Every folder and file on the server, two folder levels deep, so we can see what Automatic Distributors publishes
    (data files, price files, images...). Capped so a huge image folder cannot stall the check."""
    out: list[dict] = []

    def walk(folder: str, depth: int) -> None:
        for n, is_dir, size in _ftp_list(ftp, folder):
            if len(out) >= max_entries:
                return
            path = _join(folder, n)
            out.append({"path": path, "dir": bool(is_dir), "size": size, "image": n.lower().endswith(_IMAGE_EXT)})
            if is_dir and depth < 2:
                walk(path, depth + 1)

    try:
        walk("", 1)
    except Exception as e:  # a folder we cannot open must not break the check
        log.warning("autodist ftp tree stopped: %s", e)
    return out


@router.post("/ftp/check")
def ftp_check():
    """Log in, list the top folder and the 'pricefile' folder, and preview the inventory file's columns.
    Changes nothing. When the file cannot be found it still returns what the server showed, to diagnose."""
    ftp = _ftp_connect()
    error = None
    path = None
    data = b""
    try:
        try:
            where = ftp.pwd()
        except Exception:
            where = None
        top = [{"name": n, "dir": d, "size": sz} for n, d, sz in _ftp_list(ftp, "")]
        raw = _ftp_raw_list(ftp, "")[:60]
        tree = _ftp_tree(ftp)
        price_folder = next((t["name"] for t in top if _norm_header(t["name"]) == "pricefile"), None)
        price_files = (
            [{"name": n, "size": sz} for n, d, sz in _ftp_list(ftp, price_folder) if d is not True] if price_folder else []
        )
        try:
            path = _find_inventory_file(ftp)
            data = _preview_remote(ftp, path)
        except HTTPException as e:
            error = str(e.detail)
        except Exception as e:
            error = f"Could not read {path}: {e}"
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    base = {"top": top, "price_files": price_files, "inventory_file": path, "login_folder": where, "raw_listing": raw, "error": error,
            "tree": tree, "image_files": sum(1 for t in tree if t["image"])}
    if error or not data:
        return base
    if data[:2] == b"PK":
        return {**base, "preview": None, "note": "The inventory file is a zip; it is unzipped during import."}
    txt = data.decode("utf-8-sig", errors="replace")
    lines = txt.splitlines()[:-1] if "\n" in txt else txt.splitlines()  # the last line is probably cut off
    if not lines:
        return {**base, "error": "The inventory file looks empty."}
    delim = _sniff_delimiter(lines[0])
    recs = list(csv.reader(lines[:4], delimiter=delim))
    header = recs[0]
    try:
        mapping = map_headers(header)
    except HTTPException as e:
        return {**base, "columns": header, "error": str(e.detail)}
    return {
        **base,
        "columns": header,
        "mapped": {f: header[i] for f, i in mapping.items()},
        "unmapped": [h for i, h in enumerate(header) if i not in mapping.values()],
        "sample": recs[1:4],
    }


_PEEK_FILES = [
    "PriceFile/priceupdate.txt",
    "DataFeed/All Products.txt",
    "DataFeed/DealerPriceInventoryWithShipWeight.txt",
    "DataFeed/Warehouse_Inventory.txt",
    "DataFeed/DealerInventoryUpdate.txt",
    "DataFeed/ActiveProducts.txt",
    "DataFeed/SuspendedProducts.txt",
    "DataFeed/Item Catalog Pages.txt",
    "DataFeed/DealerPriceInventory.txt",
]


@router.post("/ftp/peek")
def ftp_peek():
    """The column names and two sample lines of each Automatic Distributors data file (first 32 KB of each, nothing is stored).
    Each file gets its own login so one odd file cannot upset the rest."""
    out = []
    for path in _PEEK_FILES:
        item: dict = {"path": path, "columns": None, "sample": [], "error": None}
        try:
            ftp = _ftp_connect()
            try:
                data = _preview_remote(ftp, path, 32768)
            finally:
                try:
                    ftp.quit()
                except Exception:
                    ftp.close()
            txt = data.decode("utf-8-sig", errors="replace")
            lines = txt.splitlines()[:-1] if "\n" in txt else txt.splitlines()  # the last line is cut off
            if not lines:
                item["error"] = "empty"
            else:
                delim = _sniff_delimiter(lines[0])
                recs = list(csv.reader(lines[:3], delimiter=delim))
                item["columns"] = [c.strip() for c in recs[0]]
                item["sample"] = [" | ".join(r)[:400] for r in recs[1:3]]
        except HTTPException as e:
            item["error"] = str(e.detail)
        except Exception as e:
            item["error"] = str(e)[:200]
        out.append(item)
    return {"files": out}


@router.post("/import/ftp-photos")
def import_ftp_photos(db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Start reading the photo links out of the big All Products file. Returns at once; it runs in the background."""
    _, u, pw = _ftp_settings()
    if not u or not pw:
        raise HTTPException(400, "The Automatic Distributors FTP login is not set. Add AUTODIST_FTP_USER and AUTODIST_FTP_PASSWORD in the Render environment.")
    run_id = _begin_run(db, "ftp_photos", user.email or user.user_id)
    threading.Thread(target=_photo_job, args=(run_id,), daemon=True, name=f"autodist-photos-{run_id}").start()
    return {"run_id": run_id}


@router.post("/import/ftp")
def import_ftp(db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Start pulling the live inventory file from the FTP server. Returns at once; the import runs in the background
    and shows up in the list of imports."""
    _, u, pw = _ftp_settings()
    if not u or not pw:
        raise HTTPException(400, "The Automatic Distributors FTP login is not set. Add AUTODIST_FTP_USER and AUTODIST_FTP_PASSWORD in the Render environment.")
    run_id = _begin_run(db, "ftp", user.email or user.user_id)
    threading.Thread(target=_ftp_sync_all, args=(run_id,), daemon=True, name=f"autodist-ftp-{run_id}").start()
    return {"run_id": run_id}


# --------------------------------------------------------------------------
# your own prices: PriceFile/priceupdate.txt
# --------------------------------------------------------------------------
# Custom to our account: our real dealer cost (Dlr Cost), MAP, ship weight, size, surcharge. It only updates parts
# that are already stored from the inventory file; it never adds or retires parts.
PRICE_FILE_DEFAULT = "PriceFile/priceupdate.txt"
_PRICE_ALIASES = {
    "sku": ["itemsku", "sku"],
    "my_cost": ["dlrcost", "dealercost", "yourcost"],
    "map_price": ["mapprice", "mappricing", "map"],
    "weight_lb": ["actualweight", "itemweight"],
    "ship_weight_lb": ["shipweight", "shippingweight"],
    "length_in": ["length", "itemlength"],
    "width_in": ["width", "itemwidth"],
    "height_in": ["height", "itemheight"],
    "special_order": ["specialorder"],
    "surcharge": ["itemsurcharge", "surcharge"],
    "flat_rate_group": ["flatrategroup"],
}


def _num(value: Optional[str], places: int = 2, limit: int = 10_000_000) -> Optional[Decimal]:
    try:
        v = Decimal((value or "").strip()).quantize(Decimal(1).scaleb(-places))
    except InvalidOperation:
        return None
    return v if 0 <= v < limit else None


def clean_price_row(raw: dict) -> Optional[dict]:
    sku = (raw.get("sku") or "").strip()
    if not sku:
        return None
    so = (raw.get("special_order") or "").strip().upper()
    return {
        "sku": sku[:60],
        "my_cost": _num(raw.get("my_cost")),
        "map_price": _num(raw.get("map_price")) or None,  # 0.00 means "no MAP", not a MAP of zero
        "weight_lb": _num(raw.get("weight_lb"), 4, 100_000),
        "ship_weight_lb": _num(raw.get("ship_weight_lb"), 4, 100_000),
        "length_in": _num(raw.get("length_in"), 4, 100_000),
        "width_in": _num(raw.get("width_in"), 4, 100_000),
        "height_in": _num(raw.get("height_in"), 4, 100_000),
        "special_order": so[:1] in ("Y", "1", "T"),
        "surcharge": _num(raw.get("surcharge")),
        "flat_rate_group": (raw.get("flat_rate_group") or "").strip()[:40] or None,
    }


_PRICE_UPDATE = """
with incoming as (
  select * from unnest(
    cast(:sku as text[]), cast(:cost as numeric[]), cast(:map as numeric[]), cast(:wt as numeric[]),
    cast(:swt as numeric[]), cast(:len as numeric[]), cast(:wid as numeric[]), cast(:hgt as numeric[]),
    cast(:so as boolean[]), cast(:sur as numeric[]), cast(:frg as text[])
  ) as t(sku, my_cost, map_price, weight_lb, ship_weight_lb, length_in, width_in, height_in, special_order, surcharge, flat_rate_group)
), up as (
  update supplier_items s set
    my_cost = i.my_cost, map_price = i.map_price, weight_lb = i.weight_lb, ship_weight_lb = i.ship_weight_lb,
    length_in = i.length_in, width_in = i.width_in, height_in = i.height_in, special_order = i.special_order,
    surcharge = i.surcharge, flat_rate_group = i.flat_rate_group, price_synced_at = now(),
    updated_at = case when (s.my_cost, s.map_price, s.surcharge) is distinct from (i.my_cost, i.map_price, i.surcharge)
                      then now() else s.updated_at end
  from incoming i
  where s.supplier = :sup and s.sku = i.sku
  returning 1
)
select count(*) from up
"""


def update_prices(db: Session, rows: list[dict]) -> int:
    """Store these price rows on the parts we already hold. Returns how many parts matched. Does not commit."""
    if not rows:
        return 0
    col = lambda k: [r[k] for r in rows]  # noqa: E731
    return int(
        db.execute(
            text(_PRICE_UPDATE),
            {"sup": SUPPLIER, "sku": col("sku"), "cost": col("my_cost"), "map": col("map_price"), "wt": col("weight_lb"),
             "swt": col("ship_weight_lb"), "len": col("length_in"), "wid": col("width_in"), "hgt": col("height_in"),
             "so": col("special_order"), "sur": col("surcharge"), "frg": col("flat_rate_group")},
        ).scalar()
        or 0
    )


def _price_path() -> str:
    return os.environ.get("AUTODIST_FTP_PRICE_FILE", "").strip() or PRICE_FILE_DEFAULT


def _update_job(run_id: int, path: str, aliases: dict, cleaner, updater, summarize, price_signature: bool = False) -> bool:
    """Background: download a feed file and use its lines to update parts we already hold (never adds or retires)."""
    gen = get_db()
    db = next(gen)
    tmp_path = None
    try:
        ftp = _ftp_connect()
        try:
            signature = _remote_signature(ftp, path)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".feed") as tmp:
                tmp_path = tmp.name
                ftp.retrbinary("RETR " + path, tmp.write, blocksize=65536)
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
        stream, delim, closer = _open_downloaded(tmp_path)
        try:
            seen = skipped = matched = 0
            batch: dict[str, dict] = {}

            def flush():
                nonlocal seen, matched
                if not batch:
                    return
                rows = list(batch.values())
                matched += updater(db, rows)
                seen += len(rows)
                db.execute(
                    text("update supplier_import_runs set rows_seen = :n, rows_new = :m, rows_skipped = :k where id = :i"),
                    {"n": seen, "m": matched, "k": skipped, "i": run_id},
                )
                db.commit()
                batch.clear()

            quoting = csv.QUOTE_MINIMAL if delim == "," else csv.QUOTE_NONE
            for row in _rows_from_stream(stream, delimiter=delim, aliases=aliases, cleaner=cleaner, quoting=quoting):
                if row is None:
                    skipped += 1
                    continue
                batch[row["sku"]] = row
                if len(batch) >= _FTP_BATCH:
                    flush()
            flush()
        finally:
            closer()
        if seen == 0:
            raise HTTPException(400, "That file held no usable lines.")
        db.execute(
            text("update supplier_import_runs set status = 'done', finished_at = now(), rows_skipped = :k, note = null where id = :i"),
            {"k": skipped, "i": run_id},
        )
        db.commit()
        _sync_note(db, summary=summarize(matched, seen, skipped),
                   price_signature=signature if price_signature else None, changed=price_signature)
        return True
    except HTTPException as e:
        db.rollback()
        _set_run_note(db, run_id, "failed", str(e.detail))
        _sync_note(db, error=str(e.detail))
    except Exception:
        db.rollback()
        log.exception("autodist file update failed (run %s, %s)", run_id, path)
        _set_run_note(db, run_id, "failed", "The import failed. See the server log.")
        _sync_note(db, error="The import failed. See the server log.")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        gen.close()
    return False


def _price_job(run_id: int) -> bool:
    return _update_job(
        run_id, _price_path(), _PRICE_ALIASES, clean_price_row, update_prices,
        lambda matched, seen, skipped: f"Prices updated: {matched:,} of the {seen:,} lines in the price file matched a stored part.",
        price_signature=True,
    )


# --------------------------------------------------------------------------
# photo links: DataFeed/All Products.txt (Image_URL_1 .. Image_URL_6)
# --------------------------------------------------------------------------
# A 140 MB file, so it only runs when someone clicks the button (photos rarely change). Only the photo links are
# kept; the photos themselves stay on Automatic Distributors' image server until a part is actually listed.
DETAILS_FILE_DEFAULT = "DataFeed/All Products.txt"
_DETAIL_ALIASES = {
    "sku": ["sku"],
    **{f"image_{i}": [f"imageurl{i}"] for i in range(1, 7)},
}


def clean_detail_row(raw: dict) -> Optional[dict]:
    sku = (raw.get("sku") or "").strip()
    if not sku:
        return None
    urls = []
    for i in range(1, 7):
        u = (raw.get(f"image_{i}") or "").strip().replace(" ", "%20")
        if u.lower().startswith(("http://", "https://")) and u not in urls:
            urls.append(u[:500])
    return {"sku": sku[:60], "images": "\n".join(urls)}


_DETAIL_UPDATE = """
with incoming as (
  select * from unnest(cast(:sku as text[]), cast(:imgs as text[])) as t(sku, imgs)
), up as (
  update supplier_items s set
    images = case when i.imgs = '' then null else string_to_array(i.imgs, chr(10)) end,
    images_synced_at = now()
  from incoming i
  where s.supplier = :sup and s.sku = i.sku
  returning 1
)
select count(*) from up
"""


def update_details(db: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    return int(
        db.execute(text(_DETAIL_UPDATE), {"sup": SUPPLIER, "sku": [r["sku"] for r in rows], "imgs": [r["images"] for r in rows]}).scalar()
        or 0
    )


def _photo_job(run_id: int) -> bool:
    return _update_job(
        run_id, os.environ.get("AUTODIST_FTP_DETAILS_FILE", "").strip() or DETAILS_FILE_DEFAULT,
        _DETAIL_ALIASES, clean_detail_row, update_details,
        lambda matched, seen, skipped: f"Photo links updated for {matched:,} parts ({seen:,} lines read).",
    )


def _ftp_sync_all(run_id: int) -> None:
    """Manual sync: the inventory file first, then our prices."""
    if not _ftp_job(run_id):
        return
    gen = get_db()
    db = next(gen)
    try:
        price_run = _begin_run(db, "ftp_price", "sync")
    except HTTPException:
        return
    finally:
        gen.close()
    _price_job(price_run)


# --------------------------------------------------------------------------
# automatic sync (off until someone turns it on)
# --------------------------------------------------------------------------
# FTP cannot tell us when a file changes, so every N minutes the server logs in and asks for the file's modified
# time and size (a few bytes). It downloads and imports only when those differ from the last import. The setting
# lives in supplier_sync_settings (sql/autodist_sync_settings.sql). The checker is one background thread started
# when this module loads; the "claim" below makes it safe when the server runs several copies.
SCHEDULER_TICK_SECONDS = 30


class ScheduleIn(BaseModel):
    enabled: Optional[bool] = None
    interval_minutes: Optional[int] = Field(None, ge=1, le=1440)
    tracking_enabled: Optional[bool] = None  # automatic order tracking check (separate from the feed check)
    tracking_interval_minutes: Optional[int] = Field(None, ge=30, le=1440)


def _auto_tick() -> None:
    gen = get_db()
    db = next(gen)
    try:
        claimed = db.execute(
            text(
                "update supplier_sync_settings set last_checked_at = now() where supplier = :s and auto_enabled "
                "and (last_checked_at is null or last_checked_at <= now() - make_interval(mins => interval_minutes)) "
                "returning last_signature, last_price_signature"
            ),
            {"s": SUPPLIER},
        ).first()
        db.commit()
        if claimed is None:
            return  # off, not due yet, or another copy of the server took this check
        last_sig, last_price_sig = claimed[0], claimed[1]
        try:
            ftp = _ftp_connect()
            try:
                sig = _remote_signature(ftp, _find_inventory_file(ftp))
                price_sig = _remote_signature(ftp, _price_path())
            finally:
                try:
                    ftp.quit()
                except Exception:
                    ftp.close()
        except HTTPException as e:
            _sync_note(db, error=str(e.detail))
            return
        inv_changed = sig == "-|-" or sig != last_sig
        price_changed = price_sig == "-|-" or price_sig != last_price_sig
        if not inv_changed and not price_changed:
            _sync_note(db, summary="Checked: Automatic Distributors' files have not changed since the last import.")
            return
        if inv_changed:
            try:
                run_id = _begin_run(db, "ftp_auto", "automatic sync")
            except HTTPException:
                return  # an import is already running
            if not _ftp_job(run_id):
                return
        if price_changed:
            try:
                price_run = _begin_run(db, "ftp_price", "automatic sync")
            except HTTPException:
                return
            _price_job(price_run)
    except Exception:
        db.rollback()
        log.exception("autodist automatic sync check failed")
    finally:
        gen.close()


def _scheduler_loop() -> None:
    time.sleep(45)  # let the server finish starting
    while True:
        try:
            _auto_tick()
        except Exception:
            log.exception("autodist scheduler")
        time.sleep(SCHEDULER_TICK_SECONDS)


def _start_scheduler() -> None:
    if os.environ.get("AUTODIST_SCHEDULER", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    threading.Thread(target=_scheduler_loop, daemon=True, name="autodist-auto-sync").start()


_start_scheduler()


_tracking_settings_ready = False


def _ensure_tracking_settings(db: Session) -> bool:
    """The automatic tracking check keeps its setting beside the feed setting. The four columns are added the first time
    they are needed, so there is no SQL to run. Returns False if they could not be added."""
    global _tracking_settings_ready
    if _tracking_settings_ready:
        return True
    try:
        for ddl in (
            "alter table supplier_sync_settings add column if not exists tracking_enabled boolean not null default true",
            "alter table supplier_sync_settings add column if not exists tracking_interval_minutes integer not null default 180",
            "alter table supplier_sync_settings add column if not exists tracking_last_run_at timestamptz",
            "alter table supplier_sync_settings add column if not exists tracking_last_summary text",
            "alter table supplier_sync_settings add column if not exists invoices_last_sync_at timestamptz",
        ):
            db.execute(text(ddl))
        db.commit()
        _tracking_settings_ready = True
        return True
    except Exception:
        db.rollback()
        log.exception("could not add the tracking settings columns")
        return False


def _schedule_row(db: Session) -> dict:
    extra = (
        ", tracking_enabled, tracking_interval_minutes, tracking_last_run_at, tracking_last_summary"
        if _ensure_tracking_settings(db)
        else ""
    )
    row = db.execute(
        text(
            "select auto_enabled, interval_minutes, last_checked_at, last_changed_at, last_summary, last_error" + extra +
            " from supplier_sync_settings where supplier = :s"
        ),
        {"s": SUPPLIER},
    ).first()
    if row is None:
        db.execute(text("insert into supplier_sync_settings (supplier) values (:s) on conflict do nothing"), {"s": SUPPLIER})
        db.commit()
        return _schedule_row(db)
    d = dict(row._mapping)
    d["running"] = bool(
        db.execute(
            text("select 1 from supplier_import_runs where supplier = :s and status = 'running' and started_at > now() - interval '30 minutes' limit 1"),
            {"s": SUPPLIER},
        ).scalar()
    )
    return d


@router.get("/ftp/schedule")
def ftp_schedule(db: Session = Depends(get_db)):
    return _schedule_row(db)


@router.put("/ftp/schedule")
def ftp_schedule_set(body: ScheduleIn, db: Session = Depends(get_db)):
    _schedule_row(db)  # makes sure the row exists
    if body.enabled is not None:
        if body.enabled:
            _, u, pw = _ftp_settings()
            if not u or not pw:
                raise HTTPException(400, "Add AUTODIST_FTP_USER and AUTODIST_FTP_PASSWORD in the Render environment first.")
        # turning it on makes the first check happen within a minute
        db.execute(
            text(
                "update supplier_sync_settings set auto_enabled = :e, "
                "last_checked_at = case when :e and not auto_enabled then null else last_checked_at end, "
                "updated_at = now() where supplier = :s"
            ),
            {"e": body.enabled, "s": SUPPLIER},
        )
    if body.interval_minutes is not None:
        db.execute(
            text("update supplier_sync_settings set interval_minutes = :m, updated_at = now() where supplier = :s"),
            {"m": body.interval_minutes, "s": SUPPLIER},
        )
    if body.tracking_enabled is not None or body.tracking_interval_minutes is not None:
        if not _ensure_tracking_settings(db):
            raise HTTPException(503, "The tracking setting could not be saved (the settings table could not be changed).")
        if body.tracking_enabled is not None:
            db.execute(
                text("update supplier_sync_settings set tracking_enabled = :e, updated_at = now() where supplier = :s"),
                {"e": body.tracking_enabled, "s": SUPPLIER},
            )
        if body.tracking_interval_minutes is not None:
            db.execute(
                text("update supplier_sync_settings set tracking_interval_minutes = :m, updated_at = now() where supplier = :s"),
                {"m": body.tracking_interval_minutes, "s": SUPPLIER},
            )
    db.commit()
    return _schedule_row(db)


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
                   count(*) filter (where active and qty_indiana + qty_nevada > 0) as in_stock,
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
            "from supplier_import_runs where supplier = :s order by id desc limit 300"
        ),
        {"s": SUPPLIER},
    ).all()
    return {"counts": dict(s._mapping), "runs": [dict(r._mapping) for r in runs]}


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


_LABEL_MATCH_SQL = """
with prod as (
  select p.id, p.sku_display as sku, p.title, btrim(p.custom_label_sku) as label,
         regexp_split_to_array(btrim(p.custom_label_sku), 'xx', 'i') as toks
  from products p
  where p.id = any(cast(:ids as bigint[])) and nullif(btrim(p.custom_label_sku), '') is not null
), tok as (
  select prod.id, prod.sku, prod.title, prod.label, array_length(prod.toks, 1) as pieces, t.tok
  from prod cross join lateral unnest(prod.toks) as t(tok)
  where btrim(t.tok) <> ''
)
select tok.id, tok.sku, tok.title, tok.label, tok.pieces, s.sku as supplier_sku, s.manufacturer,
       s.name as supplier_name, s.effective_cost, 'part number' as via
from tok
join supplier_items s on s.supplier = :s and s.active and s.sku in (tok.tok, upper(tok.tok))
union all
select tok.id, tok.sku, tok.title, tok.label, tok.pieces, s.sku, s.manufacturer,
       s.name, s.effective_cost, 'manufacturer number'
from tok
join supplier_items s on s.supplier = :s and s.active and length(tok.tok) >= 4
 and s.mfr_number_norm = upper(regexp_replace(tok.tok, '[^A-Za-z0-9]', '', 'g'))
where not exists (select 1 from supplier_items x where x.supplier = :s and x.active and x.sku in (tok.tok, upper(tok.tok)))
order by 1, 6
limit 1000
"""


def label_tokens(label: Optional[str]) -> list[str]:
    """A custom label SKU as its pieces: '182884xx182951' -> ['182884', '182951']."""
    return [t for t in re.split(r"xx", (label or "").strip(), flags=re.I) if t.strip()]


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
                       s.sku as supplier_sku, s.manufacturer, s.name as supplier_name, s.effective_cost
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
    # Products with no barcode or part-number match: try the eBay custom label SKU. A label is the distributor's part
    # numbers joined by "xx" (see kits.py), e.g. 182884xx182951; a single-part label is just the number.
    none_ids = [r.id for r in rows if r.kind == "none"]
    label_rows: list[dict] = []
    label_products = 0
    if none_ids:
        found = db.execute(text(_LABEL_MATCH_SQL), {"s": SUPPLIER, "ids": none_ids}).all()
        label_rows = [dict(f._mapping) for f in found]
        label_products = len({f["id"] for f in label_rows})
    counts["label"] = label_products
    return {
        "total_products": len(rows), "counts": counts, "review": review, "review_limited": len(review_ids) == 300,
        "label_review": label_rows[:400], "label_limited": len(label_rows) > 400,
    }


# --------------------------------------------------------------------------
# browsing the catalog in detail (read only)
# --------------------------------------------------------------------------
# What we hold for our own products, as comparable keys (barcode digits, part number letters+digits).
_OURS_CTE = f"""
ours as materialized (
  select nullif(ltrim(regexp_replace(coalesce(p.barcode, ''), '[^0-9]', '', 'g'), '0'), '') as upc_n,
         nullif(upper(regexp_replace(coalesce({_MPN}, ''), '[^A-Za-z0-9]', '', 'g')), '') as mpn_n
  from products p
)
"""
_IS_OURS = (
    "(s.upc_norm in (select upc_n from ours where upc_n is not null and length(upc_n) >= 8) "
    "or s.mfr_number_norm in (select mpn_n from ours where mpn_n is not null))"
)
# Maine (Warehouse_1) is decommissioned: its column is still stored but never counted or shown.
_TOTAL_QTY = "(s.qty_indiana + s.qty_nevada)"
_SORTS = {
    "brand": "s.manufacturer",
    "sku": "s.sku",
    "mfr": "s.mfr_number",
    "name": "s.name",
    "uom": "s.unit_of_measure",
    "list": "s.list_price",
    "cost": "s.effective_cost",
    "margin": "(s.list_price - s.effective_cost)",
    "margin_pct": "((s.list_price - s.effective_cost) / nullif(s.list_price, 0))",
    "stock": _TOTAL_QTY,
    "indiana": "s.qty_indiana",
    "nevada": "s.qty_nevada",
    "updated": "s.updated_at",
    "seen": "s.last_seen_at",
}
_ITEM_COLS = (
    "s.id, s.sku, s.mfr_number, s.mfr_number_norm, s.manufacturer, s.name, s.unit_of_measure, s.list_price, "
    "s.effective_cost as dealer_cost, s.dealer_cost as standard_cost, s.my_cost, s.map_price, s.weight_lb, "
    "s.ship_weight_lb, s.length_in, s.width_in, s.height_in, s.special_order, s.surcharge, "
    "s.qty_indiana, s.qty_nevada, s.upc, s.upc_norm, s.images, s.active, s.updated_at, s.last_seen_at"
)


def _filters(
    q, field, brand, status, stock, warehouse, min_qty, max_qty, min_cost, max_cost, min_list, max_list,
    min_margin_pct, max_margin_pct, uom, missing, ours, changed_days, seen_days,
) -> tuple[str, str, dict]:
    """Builds (with-clause, where-clause, params) for the catalog. Every value is bound, never pasted in."""
    where = ["s.supplier = :sup"]
    params: dict = {"sup": SUPPLIER}
    if status == "active":
        where.append("s.active")
    elif status == "inactive":
        where.append("not s.active")

    cols = {
        "sku": ["s.sku"],
        "mfr": ["s.mfr_number"],
        "name": ["s.name"],
        "brand": ["s.manufacturer"],
        "upc": ["s.upc"],
        "all": ["s.sku", "s.mfr_number", "s.name", "s.manufacturer", "s.upc"],
    }[field]
    for i, w in enumerate((q or "").split()[:8]):
        params[f"w{i}"] = f"%{w}%"
        parts = [f"{c} ilike :w{i}" for c in cols]
        # a part number typed without dashes or spaces still finds 'AB-14 1001'
        wn = norm_part(w)
        if wn and len(wn) >= 3 and field in ("all", "mfr", "sku"):
            params[f"n{i}"] = f"%{wn}%"
            parts.append(f"s.mfr_number_norm like :n{i}")
            parts.append(f"upper(regexp_replace(s.sku, '[^A-Za-z0-9]', '', 'g')) like :n{i}")
        where.append("(" + " or ".join(parts) + ")")

    if brand:
        params["brand"] = brand
        where.append("s.manufacturer = :brand")
    if uom:
        params["uom"] = uom
        where.append("s.unit_of_measure = :uom")
    if stock == "in":
        where.append(f"{_TOTAL_QTY} > 0")
    elif stock == "out":
        where.append(f"{_TOTAL_QTY} = 0")
    if warehouse:
        where.append({"indiana": "s.qty_indiana", "nevada": "s.qty_nevada"}[warehouse] + " > 0")
    for key, expr, op, val in (
        ("min_qty", _TOTAL_QTY, ">=", min_qty), ("max_qty", _TOTAL_QTY, "<=", max_qty),
        ("min_cost", "s.effective_cost", ">=", min_cost), ("max_cost", "s.effective_cost", "<=", max_cost),
        ("min_list", "s.list_price", ">=", min_list), ("max_list", "s.list_price", "<=", max_list),
    ):
        if val is not None:
            params[key] = val
            where.append(f"{expr} {op} :{key}")
    pct = "((s.list_price - s.effective_cost) / nullif(s.list_price, 0) * 100)"
    if min_margin_pct is not None:
        params["min_mp"] = min_margin_pct
        where.append(f"{pct} >= :min_mp")
    if max_margin_pct is not None:
        params["max_mp"] = max_margin_pct
        where.append(f"{pct} <= :max_mp")
    gaps = {
        "upc": "s.upc_norm is null",
        "mfr": "s.mfr_number is null",
        "name": "s.name is null",
        "cost": "s.effective_cost is null",
        "list": "s.list_price is null",
        "below_cost": "s.list_price is not null and s.effective_cost is not null and s.list_price < s.effective_cost",
        "photo": "s.images is null",
    }
    if missing:
        where.append(gaps[missing])
    with_clause = ""
    if ours:
        with_clause = "with " + _OURS_CTE
        where.append(_IS_OURS if ours == "yes" else f"not {_IS_OURS}")
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
    stock: Optional[str] = Query(None, pattern="^(in|out)$", description="in = stock at any warehouse, out = none anywhere"),
    warehouse: Optional[str] = Query(None, pattern="^(indiana|nevada)$", description="stock at that warehouse"),
    min_qty: Optional[int] = Query(None, ge=0),
    max_qty: Optional[int] = Query(None, ge=0),
    min_cost: Optional[float] = Query(None, ge=0),
    max_cost: Optional[float] = Query(None, ge=0),
    min_list: Optional[float] = Query(None, ge=0),
    max_list: Optional[float] = Query(None, ge=0),
    min_margin_pct: Optional[float] = Query(None, description="(list - cost) / list, in percent"),
    max_margin_pct: Optional[float] = Query(None),
    uom: Optional[str] = Query(None, max_length=40),
    missing: Optional[str] = Query(None, pattern="^(upc|mfr|name|cost|list|below_cost|photo)$", description="data gaps to find"),
    ours: Optional[str] = Query(None, pattern="^(yes|no)$", description="we already sell it (barcode or part number matches one of our products)"),
    changed_days: Optional[int] = Query(None, ge=1, le=3650, description="price / stock / details changed within N days"),
    seen_days: Optional[int] = Query(None, ge=1, le=3650, description="still in the feed as of N days ago"),
    sort: str = Query("brand", pattern="^(" + "|".join(_SORTS) + ")$"),
    dir: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """The catalog browser. Any mix of filters; the totals describe everything that matches, not just this page."""
    with_clause, clause, params = _filters(
        q, field, brand, status, stock, warehouse, min_qty, max_qty, min_cost, max_cost, min_list, max_list,
        min_margin_pct, max_margin_pct, uom, missing, ours, changed_days, seen_days,
    )
    agg = db.execute(
        text(
            f"{with_clause} select count(*) as total, count(*) filter (where {_TOTAL_QTY} > 0) as in_stock, "
            f"count(distinct s.manufacturer) as brands, min(s.effective_cost) as min_cost, max(s.effective_cost) as max_cost, "
            f"avg(s.effective_cost) as avg_cost, coalesce(sum(s.effective_cost * {_TOTAL_QTY}), 0) as stock_value_at_cost, "
            f"coalesce(sum({_TOTAL_QTY}), 0) as units from supplier_items s where {clause}"
        ),
        params,
    ).first()
    page = db.execute(
        text(
            f"{with_clause} select {_ITEM_COLS}, {_TOTAL_QTY} as qty_total, (s.list_price - s.effective_cost) as margin, "
            f"((s.list_price - s.effective_cost) / nullif(s.list_price, 0) * 100) as margin_pct "
            f"from supplier_items s where {clause} "
            f"order by {_SORTS[sort]} {'desc' if dir == 'desc' else 'asc'} nulls last, s.sku, s.id limit :n offset :o"
        ),
        {**params, "n": limit, "o": offset},
    ).all()
    rows = [dict(r._mapping) for r in page]

    # Which of OUR products each part on this page matches (one pass over our products).
    upcs = sorted({r["upc_norm"] for r in rows if r["upc_norm"] and len(r["upc_norm"]) >= 8})
    mpns = sorted({r["mfr_number_norm"] for r in rows if r["mfr_number_norm"]})
    ours_by_upc: dict[str, list] = {}
    ours_by_mpn: dict[str, list] = {}
    if upcs or mpns:
        found = db.execute(
            text(
                f"""
                select * from (
                  select p.id, p.sku_display as sku, p.title, coalesce(p.quantity_on_hand, 0) as on_hand,
                         nullif(ltrim(regexp_replace(coalesce(p.barcode, ''), '[^0-9]', '', 'g'), '0'), '') as upc_n,
                         nullif(upper(regexp_replace(coalesce({_MPN}, ''), '[^A-Za-z0-9]', '', 'g')), '') as mpn_n
                  from products p
                ) x where x.upc_n = any(cast(:u as text[])) or x.mpn_n = any(cast(:m as text[]))
                """
            ),
            {"u": upcs, "m": mpns},
        ).all()
        for f in found:
            if f.upc_n:
                ours_by_upc.setdefault(f.upc_n, []).append(f)
            if f.mpn_n:
                ours_by_mpn.setdefault(f.mpn_n, []).append(f)
    for r in rows:
        mine: dict[int, dict] = {}
        for f in ours_by_upc.get(r["upc_norm"] or "", []) if r["upc_norm"] and len(r["upc_norm"]) >= 8 else []:
            mine[f.id] = {"id": f.id, "sku": f.sku, "title": f.title, "on_hand": f.on_hand, "by": "barcode"}
        for f in ours_by_mpn.get(r["mfr_number_norm"] or "", []):
            mine.setdefault(f.id, {"id": f.id, "sku": f.sku, "title": f.title, "on_hand": f.on_hand, "by": "part number"})
        r["ours"] = list(mine.values())
        r.pop("upc_norm", None)
        r.pop("mfr_number_norm", None)
    return {"total": agg.total, "rows": rows, "summary": dict(agg._mapping)}


@router.get("/items/{item_id}")
def item_detail(item_id: int, db: Session = Depends(get_db)):
    """Everything stored about one supplier part, what we sell that matches it, and other parts that share its
    barcode or manufacturer number (the same thing under another SKU or brand)."""
    row = db.execute(
        text("select * from supplier_items where id = :id and supplier = :s"), {"id": item_id, "s": SUPPLIER}
    ).first()
    if not row:
        raise HTTPException(404, "That supplier part isn't stored (it may have been removed).")
    item = dict(row._mapping)
    item["standard_cost"] = item["dealer_cost"]
    if item.get("effective_cost") is not None:
        item["dealer_cost"] = item["effective_cost"]
    item.pop("effective_cost", None)
    item.pop("qty_maine", None)
    run = None
    if item.get("last_run_id"):
        r = db.execute(
            text("select id, source, status, started_at, finished_at, rows_seen from supplier_import_runs where id = :id"),
            {"id": item["last_run_id"]},
        ).first()
        run = dict(r._mapping) if r else None

    upc_n = item.get("upc_norm") if item.get("upc_norm") and len(item["upc_norm"]) >= 8 else None
    mpn_n = item.get("mfr_number_norm")
    mine = []
    if upc_n or mpn_n:
        found = db.execute(
            text(
                f"""
                select * from (
                  select p.id, p.sku_display as sku, p.title, coalesce(p.quantity_on_hand, 0) as on_hand,
                         p.list_price, p.cost, coalesce(p.is_active, true) as is_active,
                         nullif(ltrim(regexp_replace(coalesce(p.barcode, ''), '[^0-9]', '', 'g'), '0'), '') as upc_n,
                         nullif(upper(regexp_replace(coalesce({_MPN}, ''), '[^A-Za-z0-9]', '', 'g')), '') as mpn_n
                  from products p
                ) x where (cast(:u as text) is not null and x.upc_n = :u) or (cast(:m as text) is not null and x.mpn_n = :m)
                order by x.id limit 25
                """
            ),
            {"u": upc_n, "m": mpn_n},
        ).all()
        for f in found:
            d = dict(f._mapping)
            d["by"] = "barcode" if upc_n and d["upc_n"] == upc_n else "part number"
            d.pop("upc_n", None)
            d.pop("mpn_n", None)
            mine.append(d)

    cols = "id, sku, mfr_number, manufacturer, name, list_price, effective_cost as dealer_cost, qty_indiana, qty_nevada, upc, active"
    same_upc = []
    if upc_n:
        same_upc = db.execute(
            text(f"select {cols} from supplier_items where supplier = :s and upc_norm = :u and id <> :id order by manufacturer, sku limit 50"),
            {"s": SUPPLIER, "u": upc_n, "id": item_id},
        ).all()
    same_mpn = []
    if mpn_n:
        same_mpn = db.execute(
            text(f"select {cols} from supplier_items where supplier = :s and mfr_number_norm = :m and id <> :id order by manufacturer, sku limit 50"),
            {"s": SUPPLIER, "m": mpn_n, "id": item_id},
        ).all()
    same_brand = 0
    if item.get("manufacturer"):
        same_brand = db.execute(
            text("select count(*) from supplier_items where supplier = :s and manufacturer = :b and active"),
            {"s": SUPPLIER, "b": item["manufacturer"]},
        ).scalar()
    return {
        "item": item,
        "last_run": run,
        "ours": mine,
        "same_barcode": [dict(r._mapping) for r in same_upc],
        "same_part_number": [dict(r._mapping) for r in same_mpn],
        "brand_part_count": same_brand,
    }


# --------------------------------------------------------------------------
# the ordering API (live availability, quotes and orders)
# --------------------------------------------------------------------------
# Settings come from Render, never from the browser: AUTOMATIC_DIST_API_KEY (sent as the Authorization header,
# exactly as issued) and AUTOMATIC_DIST_API_URL. With no URL set the live server is used; Automatic Distributors'
# test server is https://testapi.autodist.com:8443 (its key may differ from the live one).
API_DEFAULT_URL = "https://api.autodist.com"
API_TIMEOUT = 20
# Warehouse numbers in the API. "1" is Maine, decommissioned: it is never shown or counted.
API_WAREHOUSES = {"2": "Indiana", "3": "Nevada"}


def _api_settings() -> tuple[str, str]:
    url = os.environ.get("AUTOMATIC_DIST_API_URL", "").strip().rstrip("/") or API_DEFAULT_URL
    return url, os.environ.get("AUTOMATIC_DIST_API_KEY", "").strip()


def _api_mode(url: str) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    return "test" if host.startswith("test") else "live"


def _api_get(path: str, params: Optional[dict] = None, not_found: Optional[str] = None):
    """GET a JSON endpoint. Raises HTTPException with a plain-English message; the key is never put in a message."""
    url, key = _api_settings()
    if not key:
        raise HTTPException(400, "The API key is not set. Add AUTOMATIC_DIST_API_KEY in the Render environment.")
    full = url + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    req = urllib.request.Request(full, headers={"Authorization": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace") or "null")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read().decode("utf-8", errors="replace"))
            detail = str(body.get("message") or "")[:200]
        except Exception:
            pass
        if e.code == 404:
            raise HTTPException(404, (not_found or "Automatic Distributors does not know that part number.") + (f" ({detail})" if detail else ""))
        if e.code == 422:
            raise HTTPException(422, "That part is restricted for our account." + (f" ({detail})" if detail else ""))
        if e.code == 429:
            wait = e.headers.get("Retry-After") if e.headers else None
            raise HTTPException(429, "Too many requests to Automatic Distributors" + (f"; wait {wait} seconds and try again." if wait else "; wait a moment and try again."))
        if e.code in (401, 403):
            raise HTTPException(502, f"The {_api_mode(url)} server refused the API key. Check AUTOMATIC_DIST_API_KEY, and that it belongs to that server.")
        raise HTTPException(502, f"The API answered with an error ({e.code}). {detail}".strip())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("autodist api unreachable: %s", e)
        raise HTTPException(502, "Could not reach the Automatic Distributors API. Try again in a moment.")


@router.get("/api/status")
def api_status():
    """Is the API set up, and which server does it point at? Calls nothing and never returns the key."""
    url, key = _api_settings()
    return {"configured": bool(key), "server": url, "mode": _api_mode(url)}


@router.post("/api/avail")
def api_avail(
    number: Optional[str] = Query(None, max_length=60, description="Automatic Distributors SKU; a stored in-stock part is used when empty"),
    quantity: int = Query(1, ge=1, le=100000),
    db: Session = Depends(get_db),
):
    """Live availability and our price for one part, straight from Automatic Distributors."""
    sku = (number or "").strip()
    if not sku:
        row = db.execute(
            text(f"select s.sku from supplier_items s where s.supplier = :s and s.active and {_TOTAL_QTY} > 0 order by s.id limit 1"),
            {"s": SUPPLIER},
        ).first()
        if row is None:
            raise HTTPException(400, "No stored part to test with. Enter a part number.")
        sku = row[0]
    data = _api_get("/item/avail/" + urllib.parse.quote(sku, safe=""), {"quantity": quantity})
    if not isinstance(data, dict):
        raise HTTPException(502, "The API sent something unexpected.")
    rows = []
    for a in data.get("availability") or []:
        w = str(a.get("warehouse"))
        if w in API_WAREHOUSES:
            rows.append(
                {"warehouse": API_WAREHOUSES[w], "quantity": a.get("quantity"), "price": a.get("price"), "extended_price": a.get("extendedPrice")}
            )
    url, _key = _api_settings()
    return {
        "server": url,
        "mode": _api_mode(url),
        "number": data.get("number") or sku,
        "quantity": quantity,
        "image": data.get("image"),
        "warehouses": rows,
        "total": sum(int(r["quantity"] or 0) for r in rows),
    }


# --------------------------------------------------------------------------
# orders: quote (POST /order/calculate) and place (POST /order/create)
# --------------------------------------------------------------------------
# A quote creates nothing. Placing an order is guarded three ways: the browser must send confirm=true, a real (live
# server) order is refused unless AUTOMATIC_DIST_ALLOW_LIVE_ORDERS=yes is set in Render, and the same PO number is never
# sent twice to the same server. Every placed order is written to supplier_orders (sql/autodist_orders.sql).
ORDER_CARRIERS = ("UPS", "FEDEX")


class OrderAddress(BaseModel):
    name: str = Field("", max_length=100)
    contact: str = Field("", max_length=100)
    addressLine1: str = Field("", max_length=100)
    addressLine2: str = Field("", max_length=100)
    addressLine3: str = Field("", max_length=100)
    city: str = Field("", max_length=60)
    state: str = Field("", max_length=30)
    zipCode: str = Field("", max_length=20)
    countryCode: str = Field("US", max_length=3)
    phone: str = Field("", max_length=30)
    classification: str = Field("", max_length=30)


class OrderLine(BaseModel):
    number: str = Field(..., min_length=1, max_length=60)
    quantity: int = Field(..., ge=1, le=100000)


class OrderIn(BaseModel):
    po_number: str = Field("", max_length=40)
    comment: str = Field("", max_length=200)
    shipping_method: str = Field("", max_length=60)
    ship_to: OrderAddress
    items: list[OrderLine] = Field(..., min_length=1, max_length=100)
    carrier: str = Field("", max_length=10)  # UPS or FEDEX: bill shipping to our own carrier account
    carrier_account: str = Field("", max_length=40)
    promo_code: str = Field("", max_length=40)
    signature_required: bool = False
    backorder: bool = False
    po_id: Optional[int] = None  # the purchase order this order comes from; it is marked sent once the order is placed
    confirm: bool = False


def _order_payload(b: OrderIn) -> dict:
    """Build the OrderReq the API expects, with plain-English errors for anything missing."""
    keys = ("name", "contact", "addressLine1", "addressLine2", "addressLine3", "city", "state", "zipCode", "countryCode", "phone", "classification")
    addr = {k: (getattr(b.ship_to, k) or "").strip() for k in keys}
    labels = (("name", "name"), ("addressLine1", "street address"), ("city", "city"), ("state", "state"), ("zipCode", "ZIP code"))
    missing = [label for k, label in labels if not addr[k]]
    if missing:
        raise HTTPException(422, "The ship-to address is missing: " + ", ".join(missing) + ".")
    addr["countryCode"] = addr["countryCode"] or "US"
    addr = {k: v for k, v in addr.items() if v}
    items = []
    for it in b.items:
        sku = it.number.strip()
        if not sku:
            raise HTTPException(422, "Every line needs a part number.")
        items.append({"number": sku, "quantity": it.quantity})
    payload: dict = {
        "PONumber": b.po_number.strip(),
        "placeOrderOnBackorder": b.backorder,
        "comment": b.comment.strip(),
        "header": {"shippingMethod": b.shipping_method.strip(), "shippingAddress": addr},
        "items": items,
        "signatureRequired": b.signature_required,
    }
    if b.promo_code.strip():
        payload["promoCode"] = b.promo_code.strip()
    carrier, acct = b.carrier.strip().upper(), b.carrier_account.strip()
    if carrier or acct:
        if carrier not in ORDER_CARRIERS:
            raise HTTPException(422, "Our own carrier account must be UPS or FEDEX.")
        if carrier == "UPS" and not re.fullmatch(r"[A-Za-z0-9]{6,}", acct):
            raise HTTPException(422, "A UPS account number is at least 6 letters or digits.")
        if carrier == "FEDEX" and not re.fullmatch(r"[0-9]{9,}", acct):
            raise HTTPException(422, "A FedEx account number is at least 9 digits.")
        payload["shippingAccount"] = {"carrier": carrier, "number": acct}
    return payload


def _api_post(path: str, body: dict, may_have_happened: bool = False):
    """POST JSON. Raises HTTPException in plain English; the key is never put in a message."""
    url, key = _api_settings()
    if not key:
        raise HTTPException(400, "The API key is not set. Add AUTOMATIC_DIST_API_KEY in the Render environment.")
    req = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Authorization": key, "Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace") or "null")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
                detail = str(parsed.get("message") or parsed.get("error") or raw) if isinstance(parsed, dict) else raw
            except Exception:
                detail = raw
        except Exception:
            pass
        detail = detail.strip()[:300]
        if e.code in (401, 403):
            raise HTTPException(502, f"The {_api_mode(url)} server refused the API key. Check AUTOMATIC_DIST_API_KEY, and that it belongs to that server.")
        if e.code == 429:
            raise HTTPException(429, "Too many requests to Automatic Distributors; wait a moment and try again.")
        raise HTTPException(502, f"Automatic Distributors answered with an error ({e.code}). {detail}".strip())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("autodist api unreachable: %s", e)
        if may_have_happened:
            raise HTTPException(
                504,
                "No answer came back, so the order MAY have been placed. Do not send it again until you have checked "
                "with Automatic Distributors (or the Orders list below).",
            )
        raise HTTPException(502, "Could not reach the Automatic Distributors API. Try again in a moment.")


def _order_totals(t: Optional[dict]) -> dict:
    t = t or {}
    return {
        "message": t.get("message"),
        "done": t.get("done"),
        "sales_amount": t.get("salesAmount"),
        "sales_tax": t.get("salesTaxAmount"),
        "excise": t.get("federalExciseAmount"),
        "special_charge": t.get("totalSpecialCharge"),
        "container_charge": t.get("totContnChrg"),
        "trade_discount": t.get("tradeDiscountAmount"),
        "order_value": t.get("totalOrderValue"),
        "invoice_total": t.get("totalInvoiceAmount"),
        "weight": t.get("totalOrderWeight"),
        "dim_weight": t.get("totalOrderDimWeight"),
        "shipping_options": [
            {"name": o.get("name"), "carrier_code": o.get("carrierCode"), "date": o.get("date"), "cost": o.get("cost")}
            for o in (t.get("shippingOptions") or [])
            if isinstance(o, dict)
        ],
    }


@router.post("/api/order/quote")
def api_order_quote(body: OrderIn):
    """Price and shipping options for an order. Creates nothing."""
    data = _api_post("/order/calculate", _order_payload(body))
    if not isinstance(data, dict):
        raise HTTPException(502, "The API sent something unexpected.")
    url, _key = _api_settings()
    return {"server": url, "mode": _api_mode(url), "totals": _order_totals(data.get("total") if isinstance(data.get("total"), dict) else data)}


def _order_result(data: dict) -> dict:
    h = data.get("header") or {}
    lines = []
    for i in (data.get("details") or {}).get("item") or []:
        w = str(i.get("warehouseId") or "")
        lines.append(
            {
                "number": i.get("itemNumber"),
                "description": i.get("description"),
                "ordered": i.get("orderQty"),
                "shipped": i.get("shipQty"),
                "backordered": i.get("backOrdQty"),
                "price": i.get("actualSellPrice"),
                "amount": i.get("lineAmount"),
                "warehouse": API_WAREHOUSES.get(w),  # Maine (1) is never shown
            }
        )
    related = []
    for r in data.get("relatedOrders") or []:
        rh = r.get("header") or {}
        related.append(
            {
                "order_number": rh.get("orderNumber"),
                "parent": rh.get("parentOrderNumber"),
                "po": rh.get("poNumber"),
                "pro_number": rh.get("proNumber"),
                "status": rh.get("orderStatus"),
                "carrier": rh.get("carrierCode"),
                "invoice_number": rh.get("invoiceNumber"),
                "invoice_date": rh.get("invoiceDate"),
                "invoice_amount": rh.get("invoiceAmount"),
                "order_value": rh.get("totalOrderValue"),
            }
        )
    return {
        "order_number": h.get("orderNumber") or "",
        "completion_code": h.get("completionCode"),
        "message": h.get("message"),
        "po": h.get("ponumber"),
        "invoice_amount": h.get("invoiceAmount"),
        "lines": lines,
        "totals": _order_totals(data.get("total")),
        "related": related,
    }


@router.post("/api/order/place")
def api_order_place(body: OrderIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """PLACES A REAL ORDER on whichever server is configured. Needs confirm=true; live orders also need the Render switch."""
    if not body.confirm:
        raise HTTPException(400, "Not placed: the order has to be confirmed first.")
    po = body.po_number.strip()
    if not po:
        raise HTTPException(422, "Enter a PO number before placing the order.")
    url, _key = _api_settings()
    mode = _api_mode(url)
    if mode == "live" and os.environ.get("AUTOMATIC_DIST_ALLOW_LIVE_ORDERS", "").strip().lower() not in ("1", "true", "yes"):
        raise HTTPException(
            403,
            "Real orders are switched off. Point AUTOMATIC_DIST_API_URL at the test server to practice, or set "
            "AUTOMATIC_DIST_ALLOW_LIVE_ORDERS=yes in Render when you are ready for real ones.",
        )
    payload = _order_payload(body)
    try:
        dup = db.execute(
            text("select order_number from supplier_orders where supplier = :s and mode = :m and po_number = :p and order_number <> '' limit 1"),
            {"s": SUPPLIER, "m": mode, "p": po},
        ).scalar()
    except Exception:
        db.rollback()
        raise HTTPException(503, "Not placed: the orders table is missing. Run sql/autodist_orders.sql in Supabase, then try again.")
    if dup:
        raise HTTPException(409, f"Not placed: PO {po} was already sent to the {mode} server as order {dup}. Use a new PO number.")

    data = _api_post("/order/create", payload, may_have_happened=True)
    if not isinstance(data, dict):
        raise HTTPException(502, "The API answered, but not in a form I understand. Check with Automatic Distributors before sending again.")
    result = _order_result(data)
    saved = True
    try:
        db.execute(
            text(
                "insert into supplier_orders (supplier, mode, po_number, order_number, message, invoice_amount, request, response, created_by) "
                "values (:s, :m, :p, :o, :msg, :inv, cast(:rq as jsonb), cast(:rs as jsonb), :u)"
            ),
            {
                "s": SUPPLIER, "m": mode, "p": po, "o": result["order_number"], "msg": (result["message"] or "")[:300],
                "inv": str(result["invoice_amount"] or ""), "rq": json.dumps(payload), "rs": json.dumps(data),
                "u": user.email or user.user_id,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        saved = False
        log.exception("autodist order %s placed but not saved", result["order_number"])
    log.info("autodist order placed on %s server: PO %s -> order %s", mode, po, result["order_number"] or "(none)")
    po_updated = None
    if body.po_id and result["order_number"]:
        po_updated = _mark_po_placed(db, body.po_id, po, result["order_number"])
    return {"server": url, "mode": mode, "placed": bool(result["order_number"]), "saved": saved, "po_updated": po_updated, **result}


@router.get("/api/orders")
def api_orders(db: Session = Depends(get_db)):
    """The orders placed from this screen, newest first, with status and tracking once they have been checked."""
    base = "id, mode, po_number, order_number, message, invoice_amount, created_at, created_by"
    try:
        try:
            rows = db.execute(
                text(f"select {base}, status, tracking, tracking_checked_at from supplier_orders where supplier = :s order by id desc limit 50"),
                {"s": SUPPLIER},
            ).all()
            tracking_missing = False
        except Exception:
            db.rollback()  # the tracking columns are not there yet (sql/autodist_tracking.sql)
            rows = db.execute(
                text(f"select {base} from supplier_orders where supplier = :s order by id desc limit 50"), {"s": SUPPLIER}
            ).all()
            tracking_missing = True
    except Exception:
        db.rollback()
        return {"table_missing": True, "tracking_missing": False, "orders": []}
    out = []
    for r in rows:
        d = dict(r._mapping)
        d.setdefault("status", None)
        d.setdefault("tracking_checked_at", None)
        d["tracking"] = d.get("tracking") or []
        out.append(d)
    return {"table_missing": False, "tracking_missing": tracking_missing, "orders": out}


# --------------------------------------------------------------------------
# tracking: GET /order/number/{order} -> status + tracking / pro numbers
# --------------------------------------------------------------------------
# Automatic Distributors splits trackingNumber (parcel) from proNumber (freight) and may split one order into related
# orders, each with its own. The reply is read tolerantly (any header-like block that carries a number), de-duplicated,
# and stored on supplier_orders.tracking as a list of {number, kind, carrier, order_number, status}. Nothing here changes
# an order. Checking is by button, one call per order, so it stays well inside the API's rate limit.
TRACK_BATCH = 25
TRACK_WINDOW_DAYS = 60


def _collect_tracking(data) -> list[dict]:
    found: list[dict] = []
    seen: set[tuple] = set()

    def add(h: dict) -> None:
        carrier, status, onum = h.get("carrierCode"), h.get("orderStatus"), h.get("orderNumber")
        for key, kind in (("trackingNumber", "tracking"), ("trackingNumbers", "tracking"), ("proNumber", "pro")):
            raw = h.get(key)
            for v in raw if isinstance(raw, list) else [raw]:
                if isinstance(v, (dict, list)):
                    continue  # a wrapper holding the parcels, not a number: walk() finds the parcels inside it
                v = str(v or "").strip()
                if v and (v, kind) not in seen:
                    seen.add((v, kind))
                    found.append({"number": v[:60], "kind": kind, "carrier": carrier, "order_number": onum, "status": status})

    def walk(o) -> None:
        if isinstance(o, dict):
            if any(k in o for k in ("trackingNumber", "trackingNumbers", "proNumber")):
                add(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return found


# A drop ship PO is closed out when the distributor sends back a tracking number for the order placed from it: the parts
# go straight to the customer, so nothing is received into stock. Not closed while the order is cancelled, on hold,
# backordered or only partly shipped (the status words are read loosely, see the note on _collect_tracking).
_NOT_SHIPPED_YET = re.compile(r"partial|back ?order|hold|cancel", re.I)


def _close_drop_ship_po(db: Session, po_number: Optional[str], order_number: str, status: Optional[str], items: list[dict]) -> Optional[int]:
    """Close the open DROP SHIP purchase order this order was placed from. Its lines are marked done (received_by says why),
    NOTHING is added to stock, and the PO becomes 'received' so it leaves the open lists. Returns the PO number, or None."""
    if not po_number or not any(i.get("number") for i in items):
        return None
    if status and _NOT_SHIPPED_YET.search(status):
        return None
    try:
        cols = _po_columns(db)
        if "po_type" not in cols:
            return None
        extra = " and (h.supplier_order_number is null or h.supplier_order_number = :on)" if "supplier_order_number" in cols else ""
        h = db.execute(
            text(
                "select h.id, h.po_number from po_headers h where cast(h.po_number as text) = :pn and " + _PO_VENDOR +
                " and h.po_type = 'drop_ship' and h.status = 'open' and h.sent_at is not null" + extra
            ),
            {"pn": po_number, "on": order_number},
        ).first()
        if not h:
            return None
        db.execute(
            text("update po_lines set received_at = now(), received_qty = quantity, received_by = :by where po_id = :i and received_at is null"),
            {"by": "Automatic Distributors tracking", "i": h.id},
        )
        db.execute(
            text("update po_headers set status = 'received', received_at = now(), updated_at = now() where id = :i and status = 'open'"),
            {"i": h.id},
        )
        db.commit()
        return int(h.po_number)
    except Exception:
        db.rollback()
        log.exception("tracking came back for order %s but PO %s could not be closed", order_number, po_number)
        return None


# When the drop ship PO came from an eBay order (its source is "ebay:<order id>"), the first parcel tracking number is
# copied onto that eBay order so Pick and Ship opens "Mark shipped" with it already filled in. We do NOT tell eBay
# here: a person still presses Mark shipped, which is what sends it to eBay and notifies the buyer. A freight PRO number
# is not copied (eBay wants a parcel carrier + number). An order already shipped on eBay, or one that already has a
# tracking number, is left alone.
def _guess_carrier(number: str, code: Optional[str]) -> Optional[str]:
    c = (code or "").upper()
    if "UPS" in c or number.upper().startswith("1Z"):
        return "UPS"
    if "FED" in c or "FDX" in c:
        return "FedEx"
    if "USPS" in c or "POST" in c:
        return "USPS"
    if "DHL" in c:
        return "DHL"
    return None


def _hand_off_to_ebay(db: Session, po_number: Optional[str], status: Optional[str], items: list[dict]) -> Optional[str]:
    """Returns the eBay order id that was filled in, or None."""
    parcel = next((i for i in items if i.get("kind") == "tracking" and i.get("number")), None)
    if not po_number or not parcel or (status and _NOT_SHIPPED_YET.search(status)):
        return None
    try:
        src = db.execute(
            text(
                "select h.source from po_headers h where cast(h.po_number as text) = :pn and " + _PO_VENDOR +
                " and h.po_type = 'drop_ship' and h.status <> 'cancelled' and h.source like 'ebay:%' order by h.id desc limit 1"
            ),
            {"pn": po_number},
        ).scalar()
        if not src:
            return None
        ebay_id = str(src)[5:]
        from app import ebay_sales  # imported here so a problem in that module cannot stop this one loading

        ebay_sales._ensure_tracking_cols(db)
        num = re.sub(r"\s+", "", parcel["number"])
        if not ebay_sales._TRACK_OK.match(num):
            return None
        got = db.execute(
            text(
                "update ebay_orders set tracking_number = :tn, tracking_carrier = :tc, updated_at = now() "
                "where ebay_order_id = :o and not cancelled and shipped_at_ebay is null "
                "and coalesce(tracking_number, '') = '' returning ebay_order_id"
            ),
            {"tn": num, "tc": _guess_carrier(num, parcel.get("carrier")), "o": ebay_id},
        ).first()
        db.commit()
        return str(got[0]) if got else None
    except Exception:
        db.rollback()
        log.exception("tracking came back for PO %s but the eBay order could not be filled in", po_number)
        return None



def _has_unshipped_lines(data: dict) -> bool:
    """True when the order's own lines show something backordered or shipped short. Unknown or missing numbers count as
    fine. Reads both shapes the API uses: details.item[] with orderQty/shipQty/backOrdQty (order create and lookup) and
    details[] with quantityOrdered/quantityShipped/quantityBackOrdered (the find endpoints)."""
    def num(v):
        try:
            return Decimal(str(v))
        except (InvalidOperation, TypeError, ValueError):
            return None

    def pick(i: dict, *keys):
        for k in keys:
            if k in i:
                return num(i[k])
        return None

    d = data.get("details")
    if isinstance(d, dict):
        d = d.get("item") or d.get("items") or []
    for i in d or []:
        if not isinstance(i, dict):
            continue
        back = pick(i, "backOrdQty", "quantityBackOrdered")
        ordered = pick(i, "orderQty", "quantityOrdered")
        shipped = pick(i, "shipQty", "quantityShipped")
        if back is not None and back > 0:
            return True
        if ordered is not None and shipped is not None and shipped < ordered:
            return True
    return False


# GET /order/number/{order}/tracking-numbers lists every parcel (number, carrier, service, ship date, weight); the order's
# own header carries only one tracking number. Asked only for an order that already has a number, so an order that has
# not shipped costs one call, not two. A 429 is passed on so the round stops; anything else just means "no extra parcels".
def _parcels_for(order_number: str) -> list[dict]:
    try:
        data = _api_get("/order/number/" + urllib.parse.quote(order_number, safe="") + "/tracking-numbers", not_found="No parcels listed.")
    except HTTPException as e:
        if e.status_code == 429:
            raise
        return []
    out = _collect_tracking(data)
    for i in out:
        i["order_number"] = i.get("order_number") or order_number
    return out


_tracking_done_ready = False


def _ensure_tracking_done_col(db: Session) -> None:
    """supplier_orders.tracking_done: set once an order has its tracking and is fully shipped, so it is not asked about again."""
    global _tracking_done_ready
    if _tracking_done_ready:
        return
    try:
        db.execute(text("alter table supplier_orders add column if not exists tracking_done boolean not null default false"))
        db.commit()
        _tracking_done_ready = True
    except Exception:
        db.rollback()


def _refresh_order_tracking(db: Session, row_id: int, order_number: str) -> dict:
    _ensure_tracking_done_col(db)
    data = _api_get(
        "/order/number/" + urllib.parse.quote(order_number, safe=""),
        not_found="Automatic Distributors does not have this order on the server the app points at (a test order only exists on the test server).",
    )
    if not isinstance(data, dict):
        raise HTTPException(502, "The API sent something unexpected for that order.")
    head = data.get("header") or {}
    status = str(head.get("orderStatus") or "").strip() or None
    items = _collect_tracking(data)
    if items:
        seen = {(i["number"], i["kind"]) for i in items}
        for extra in _parcels_for(order_number):
            if (extra["number"], extra["kind"]) not in seen:
                seen.add((extra["number"], extra["kind"]))
                items.append(extra)
    # The API's status is a code ("H", "9"...), not a word, so the words in _NOT_SHIPPED_YET rarely match it. The lines are
    # the better evidence: if any line is backordered or shipped short, treat the order as partial and do not close or hand off.
    partial = _has_unshipped_lines(data)
    judged = "partial" if partial else status
    done = bool(items) and not partial and status != "9"
    try:
        db.execute(
            text(
                "update supplier_orders set status = :st, tracking = cast(:t as jsonb), tracking_checked_at = now()"
                + (", tracking_done = :d" if _tracking_done_ready else "")
                + " where id = :i"
            ),
            {"st": status, "t": json.dumps(items), "i": row_id, **({"d": done} if _tracking_done_ready else {})},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(503, "The tracking columns are missing. Run sql/autodist_tracking.sql in Supabase, then try again.")
    po_no = db.execute(text("select po_number from supplier_orders where id = :i"), {"i": row_id}).scalar()
    closed = _close_drop_ship_po(db, po_no, order_number, judged, items)
    ebay_filled = _hand_off_to_ebay(db, po_no, judged, items)
    return {"order_number": order_number, "status": status, "tracking": items, "po_closed": closed, "ebay_filled": ebay_filled}


@router.post("/api/orders/refresh-tracking")
def api_orders_refresh_tracking(db: Session = Depends(get_db)):
    """Status and tracking for recent orders on the server we point at that are not finished. Oldest-checked first."""
    url, _key = _api_settings()
    mode = _api_mode(url)
    _ensure_tracking_done_col(db)
    try:
        rows = db.execute(
            text(
                "select id, order_number from supplier_orders where supplier = :s and mode = :m and order_number <> '' "
                "and created_at > now() - make_interval(days => :d) "
                "and coalesce(lower(status), '') not similar to '%(cancel|deliver|complete)%' "
                "and coalesce(status, '') <> '9' and not coalesce(tracking_done, false) "
                "order by tracking_checked_at asc nulls first, id desc limit :n"
            ),
            {"s": SUPPLIER, "m": mode, "d": TRACK_WINDOW_DAYS, "n": TRACK_BATCH},
        ).all()
    except Exception:
        db.rollback()
        raise HTTPException(503, "The tracking columns are missing. Run sql/autodist_tracking.sql in Supabase, then try again.")
    checked, with_tracking, problems, closed_pos, ebay_filled = 0, 0, [], [], []
    for r in rows:
        try:
            res = _refresh_order_tracking(db, r.id, r.order_number)
            checked += 1
            with_tracking += 1 if res["tracking"] else 0
            if res.get("po_closed"):
                closed_pos.append(res["po_closed"])
            if res.get("ebay_filled"):
                ebay_filled.append(res["ebay_filled"])
        except HTTPException as e:
            problems.append(f"{r.order_number}: {e.detail}")
            if e.status_code in (429, 503):  # rate limited, or the columns are missing: stop instead of hammering
                break
    return {"mode": mode, "checked": checked, "with_tracking": with_tracking, "problems": problems, "closed_pos": closed_pos, "ebay_filled": ebay_filled}


@router.post("/api/orders/{order_id}/tracking")
def api_order_tracking(order_id: int, db: Session = Depends(get_db)):
    row = db.execute(
        text("select id, order_number from supplier_orders where supplier = :s and id = :i"), {"s": SUPPLIER, "i": order_id}
    ).first()
    if not row or not row.order_number:
        raise HTTPException(404, "That order is not in the list, or it has no order number yet.")
    return _refresh_order_tracking(db, row.id, row.order_number)


# --------------------------------------------------------------------------
# automatic tracking check (no button; set on the Distributor data feed tab)
# --------------------------------------------------------------------------
# On or off, and how often, is set on the Distributor data feed tab (saved in supplier_sync_settings). Each round looks at
# the orders that are not finished, a few at a time, with the same code the button uses. Each order is claimed in the
# database before it is asked about, so two copies of the server never ask twice, and an order is asked about at most
# once per interval. The batch is small and spaced out so it stays inside the API's rate limit; a 429 stops the round.
def _auto_tracking_tick() -> None:
    url, key = _api_settings()
    if not key:
        return
    mode = _api_mode(url)
    gen = get_db()
    db = next(gen)
    try:
        if not _ensure_tracking_settings(db):
            return
        cfg = db.execute(
            text("select tracking_enabled, tracking_interval_minutes from supplier_sync_settings where supplier = :s"), {"s": SUPPLIER}
        ).first()
        if cfg is None or not cfg.tracking_enabled:
            return
        _ensure_tracking_done_col(db)
        mins = int(cfg.tracking_interval_minutes or 180)
        parts: list[str] = []

        # invoices: the last 3 days, once per interval (claimed first so two copies of the server never both do it)
        claimed_inv = db.execute(
            text(
                "update supplier_sync_settings set invoices_last_sync_at = now() where supplier = :s "
                "and (invoices_last_sync_at is null or invoices_last_sync_at <= now() - make_interval(mins => :m)) returning 1"
            ),
            {"s": SUPPLIER, "m": mins},
        ).first()
        db.commit()
        if claimed_inv is not None:
            done, stored, why = _sync_invoice_days(db, 3)
            parts.append(f"{stored} invoice{'' if stored == 1 else 's'} saved")
            if why:
                parts.append(f"invoice sync stopped: {why[:120]}")

        rows = db.execute(
            text(
                "select id, order_number from supplier_orders where supplier = :s and mode = :m and order_number <> '' "
                "and created_at > now() - make_interval(days => :d) "
                "and coalesce(lower(status), '') not similar to '%(cancel|deliver|complete)%' "
                "and coalesce(status, '') <> '9' and not coalesce(tracking_done, false) "
                "and (tracking_checked_at is null or tracking_checked_at <= now() - make_interval(mins => :i)) "
                "order by tracking_checked_at asc nulls first, id desc limit :n"
            ),
            {"s": SUPPLIER, "m": mode, "d": TRACK_WINDOW_DAYS, "i": mins, "n": TRACK_BATCH},
        ).all()
        checked = with_tracking = problems = 0
        closed: list = []
        filled: list = []
        for r in rows:
            claimed = db.execute(
                text(
                    "update supplier_orders set tracking_checked_at = now() where id = :i "
                    "and (tracking_checked_at is null or tracking_checked_at <= now() - make_interval(mins => :m)) returning id"
                ),
                {"i": r.id, "m": mins},
            ).first()
            db.commit()
            if claimed is None:
                continue  # another copy of the server took it
            try:
                res = _refresh_order_tracking(db, r.id, r.order_number)
                checked += 1
                with_tracking += 1 if res["tracking"] else 0
                if res.get("po_closed"):
                    closed.append(res["po_closed"])
                if res.get("ebay_filled"):
                    filled.append(res["ebay_filled"])
            except HTTPException as e:
                problems += 1
                log.warning("autodist auto tracking %s: %s", r.order_number, e.detail)
                if e.status_code in (429, 503):
                    break
            time.sleep(1.5)
        if checked or problems:
            t = f"Checked {checked} order{'' if checked == 1 else 's'}, {with_tracking} with tracking"
            if closed:
                t += f"; closed drop ship PO {', '.join(str(c) for c in closed)}"
            if filled:
                t += f"; tracking added to eBay order {', '.join(filled)}"
            if problems:
                t += f"; {problems} problem{'' if problems == 1 else 's'} (see the server log)"
            parts.insert(0, t)
        if parts:
            db.execute(
                text("update supplier_sync_settings set tracking_last_run_at = now(), tracking_last_summary = :m where supplier = :s"),
                {"m": "; ".join(parts) + ".", "s": SUPPLIER},
            )
            db.commit()
    except Exception:
        db.rollback()
        log.exception("autodist automatic tracking check failed")
    finally:
        gen.close()


def _tracking_loop() -> None:
    time.sleep(90)
    while True:
        try:
            _auto_tracking_tick()
        except Exception:
            log.exception("autodist tracking scheduler")
        time.sleep(300)  # wake every 5 minutes; each order is only asked about once per interval


if os.environ.get("AUTODIST_SCHEDULER", "1").strip().lower() not in ("0", "false", "no", "off"):
    threading.Thread(target=_tracking_loop, daemon=True, name="autodist-auto-tracking").start()


# --------------------------------------------------------------------------
# cancel: POST /order/number/cancel/{order}
# --------------------------------------------------------------------------
# Cancelling is immediate (not a request): the API answers OrderStatus "9", which is what "cancelled" looks like in its
# data. It is refused (503) once the order has been invoiced, because the parts have shipped; after that it is a return,
# which has to be arranged with the distributor. The order is looked up first so that split (related) orders that were
# made from it are cancelled too, and so an already-invoiced order is refused before anything is sent. Only this order
# and its own related orders are touched: never "everything with this PO number", because a short number like 14 could
# also be on an unrelated order.
def _invoiced(v) -> bool:
    return str(v or "").strip() not in ("", "0")


def _cancel_call(order_number: str) -> None:
    try:
        _api_post("/order/number/cancel/" + urllib.parse.quote(order_number, safe=""), {})
    except HTTPException as e:
        d = str(e.detail)
        if "(503)" in d:
            raise HTTPException(
                409,
                f"Order {order_number} was not cancelled: Automatic Distributors says it has already been invoiced (it has shipped), "
                "or their system had a problem. Press Recheck to see its status. A shipped order can only be handled as a return with them.",
            )
        if "(404)" in d:
            raise HTTPException(404, f"Automatic Distributors does not have order {order_number} on the server the app points at.")
        if "Could not reach" in d:
            raise HTTPException(
                504,
                f"No answer came back for order {order_number}, so it MAY be cancelled. Press Recheck to see its status before trying again.",
            )
        raise


class CancelOrderIn(BaseModel):
    confirm: bool = False


@router.post("/api/orders/{order_id}/cancel")
def api_order_cancel(order_id: int, body: CancelOrderIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Cancel an order with Automatic Distributors (and its split orders). Needs confirm=true. The purchase order is NOT
    changed here; the answer says which PO it was, so the screen can offer to cancel it too."""
    if not body.confirm:
        raise HTTPException(400, "Not cancelled: it has to be confirmed first.")
    url, _key = _api_settings()
    mode = _api_mode(url)
    row = db.execute(
        text("select id, order_number, po_number, mode, status from supplier_orders where supplier = :s and id = :i"), {"s": SUPPLIER, "i": order_id}
    ).first()
    if not row or not row.order_number:
        raise HTTPException(404, "That order is not in the list, or it has no order number yet.")
    if row.mode != mode:
        raise HTTPException(409, f"That order was placed on the {row.mode} server, but the app now points at the {mode} server.")
    data = _api_get(
        "/order/number/" + urllib.parse.quote(row.order_number, safe=""),
        not_found="Automatic Distributors does not have this order on the server the app points at.",
    )
    if not isinstance(data, dict):
        raise HTTPException(502, "The API sent something unexpected for that order.")
    head = data.get("header") or {}
    main_cancelled = str(head.get("orderStatus") or "").strip() == "9"
    if not main_cancelled and _invoiced(head.get("invoiceNumber")):
        raise HTTPException(409, f"Order {row.order_number} has already been invoiced, so it has shipped and can't be cancelled. Arrange a return with Automatic Distributors.")
    cancelled: list[str] = []
    failed: list[str] = []
    if not main_cancelled:
        _cancel_call(row.order_number)  # refusing here stops everything: nothing else was touched
        cancelled.append(row.order_number)
    for r in data.get("relatedOrders") or []:
        rh = (r or {}).get("header") or {}
        n = str(rh.get("orderNumber") or "").strip()
        if not n or n == row.order_number or str(rh.get("orderStatus") or "").strip() == "9" or _invoiced(rh.get("invoiceNumber")):
            continue
        try:
            _cancel_call(n)
            cancelled.append(n)
        except HTTPException as e:
            failed.append(f"{n}: {e.detail}")
            if e.status_code == 429:
                break
    try:
        db.execute(text("update supplier_orders set status = '9', tracking_checked_at = now() where id = :i"), {"i": row.id})
        db.commit()
    except Exception:
        db.rollback()
        log.exception("order %s cancelled but the list could not be updated", row.order_number)
    log.info("autodist order cancelled by %s: PO %s -> order %s (+%s related)", user.email or user.user_id, row.po_number, row.order_number, len(cancelled) - (0 if main_cancelled else 1))
    po_id = ebay_id = None
    try:
        cols = _po_columns(db)
        src = "h.source" if "source" in cols else "null::text"
        po = db.execute(
            text(f"select h.id, h.status, {src} as source from po_headers h where cast(h.po_number as text) = :pn and {_PO_VENDOR} order by h.id desc limit 1"),
            {"pn": row.po_number},
        ).first()
        if po:
            po_id = po.id if po.status == "open" else None
            ebay_id = str(po.source)[5:] if po.source and str(po.source).startswith("ebay:") else None
    except Exception:
        db.rollback()
    return {
        "order_number": row.order_number, "already_cancelled": main_cancelled, "cancelled": cancelled, "failed": failed,
        "po_number": row.po_number, "po_id": po_id, "ebay_order_id": ebay_id,
    }


# --------------------------------------------------------------------------
# invoices: stored from GET /order/find/range/details (dateType I = by invoice date), one day at a time
# --------------------------------------------------------------------------
# Every order Automatic Distributors invoices on our account is kept in supplier_invoices (the table is created the first
# time it is needed), whether or not it was placed from this app. Asking one day at a time means the day asked is the
# invoice date, because that endpoint's reply does not always carry one. A order invoiced again later (a backorder that
# ships) comes back as another row of the same order, so the row key is the order number plus its generation number.
# "special charges" is the distributor's shipping and handling line; confirm that on one real invoice.
_invoices_ready = False


def _ensure_invoices_table(db: Session) -> bool:
    global _invoices_ready
    if _invoices_ready:
        return True
    try:
        db.execute(
            text(
                "create table if not exists supplier_invoices ("
                " id bigserial primary key, supplier text not null, mode text not null, order_number text not null,"
                " generation text not null default '', parent_order_number text, po_number text, invoice_number text,"
                " invoice_date date, invoice_amount numeric(12,2), order_value numeric(12,2), item_amount numeric(12,2),"
                " special_charges numeric(12,2), carrier_code text, tracking_number text, order_status text,"
                " ship_to_name text, ship_to_city text, ship_to_state text, ship_to_zip text,"
                " lines jsonb not null default '[]'::jsonb, first_seen_at timestamptz not null default now(),"
                " synced_at timestamptz not null default now(), unique (supplier, mode, order_number, generation))"
            )
        )
        db.execute(text("create index if not exists supplier_invoices_date_idx on supplier_invoices (invoice_date desc)"))
        db.execute(text("create index if not exists supplier_invoices_po_idx on supplier_invoices (po_number)"))
        db.commit()
        _invoices_ready = True
        return True
    except Exception:
        db.rollback()
        log.exception("could not create supplier_invoices")
        return False


def _money(v) -> Optional[Decimal]:
    try:
        d = Decimal(str(v).strip())
        return d.quantize(Decimal("0.01"))
    except (InvalidOperation, AttributeError, ValueError):
        return None


def _parse_invoice_date(v) -> Optional[_date]:
    """The API writes it as MM/DD/YY (08/04/25). Anything else is ignored."""
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\s*", str(v or ""))
    if not m:
        return None
    mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    yr = yr + 2000 if yr < 100 else yr
    try:
        return _date(yr, mo, da)
    except ValueError:
        return None


def _store_invoice(db: Session, mode: str, entry: dict, day: Optional[_date]) -> bool:
    h = entry.get("header") or {}
    order_no = str(h.get("orderNumber") or "").strip()
    if not order_no:
        return False
    addr = h.get("shipToAddress") or {}
    lines = []
    for d in entry.get("details") or []:
        if not isinstance(d, dict):
            continue
        lines.append(
            {
                "number": d.get("itemNumber"),
                "description": d.get("description"),
                "ordered": d.get("quantityOrdered"),
                "shipped": d.get("quantityShipped"),
                "backordered": d.get("quantityBackOrdered"),
                "price": d.get("actualSellPrice"),
                "warehouse": API_WAREHOUSES.get(str(d.get("warehouseID") or "")),  # Maine (1) is never shown
            }
        )
    inv_no = str(h.get("invoiceNumber") or "").strip()
    db.execute(
        text(
            "insert into supplier_invoices (supplier, mode, order_number, generation, parent_order_number, po_number, invoice_number,"
            " invoice_date, invoice_amount, order_value, item_amount, special_charges, carrier_code, tracking_number, order_status,"
            " ship_to_name, ship_to_city, ship_to_state, ship_to_zip, lines) values (:s, :m, :o, :g, :par, :po, :inv, :dt, :ia, :ov,"
            " :im, :sc, :cc, :tn, :st, :nm, :ct, :sta, :zp, cast(:ln as jsonb)) "
            "on conflict (supplier, mode, order_number, generation) do update set parent_order_number = excluded.parent_order_number,"
            " po_number = excluded.po_number, invoice_number = excluded.invoice_number,"
            " invoice_date = coalesce(excluded.invoice_date, supplier_invoices.invoice_date), invoice_amount = excluded.invoice_amount,"
            " order_value = excluded.order_value, item_amount = excluded.item_amount, special_charges = excluded.special_charges,"
            " carrier_code = excluded.carrier_code, tracking_number = excluded.tracking_number, order_status = excluded.order_status,"
            " ship_to_name = excluded.ship_to_name, ship_to_city = excluded.ship_to_city, ship_to_state = excluded.ship_to_state,"
            " ship_to_zip = excluded.ship_to_zip, lines = excluded.lines, synced_at = now()"
        ),
        {
            "s": SUPPLIER, "m": mode, "o": order_no[:40], "g": str(h.get("orderGenerationNumber") or "")[:10],
            "par": str(h.get("parentOrderNumber") or "")[:40] or None, "po": str(h.get("poNumber") or "").strip()[:60] or None,
            "inv": inv_no[:40] or None, "dt": _parse_invoice_date(h.get("invoiceDate")) or day,
            "ia": _money(h.get("invoiceAmount")), "ov": _money(h.get("totalOrderValue")), "im": _money(h.get("itemSalesAmount")),
            "sc": _money(h.get("totalSpecialCharges")), "cc": str(h.get("carrierCode") or "")[:20] or None,
            "tn": (str(h.get("trackingNumber") or "").strip() or str(h.get("proNumber") or "").strip())[:80] or None, "st": str(h.get("orderStatus") or "")[:20] or None,
            "nm": str(addr.get("name") or "")[:100] or None, "ct": str(addr.get("city") or "")[:60] or None,
            "sta": str(addr.get("state") or "")[:30] or None, "zp": str(addr.get("zipCode") or "")[:20] or None,
            "ln": json.dumps(lines),
        },
    )
    return True


def _sync_invoice_days(db: Session, days: int) -> tuple[int, int, Optional[str]]:
    """Fetch the last `days` days (today first), one call per day. Returns (days done, invoices stored, why it stopped early)."""
    if not _ensure_invoices_table(db):
        return 0, 0, "The invoices table could not be created."
    url, _key = _api_settings()
    mode = _api_mode(url)
    done = stored = 0
    today = _date.today()
    for back in range(days):
        day = today - timedelta(days=back)
        ymd = day.strftime("%Y%m%d")
        try:
            data = _api_get(
                "/order/find/range/details", {"dateType": "I", "startDate": ymd, "endDate": ymd}, not_found="No invoices that day."
            )
        except HTTPException as e:
            if e.status_code == 404:  # nothing invoiced that day
                done += 1
                time.sleep(1.2)
                continue
            db.rollback()
            return done, stored, str(e.detail)
        for entry in (data.get("list") if isinstance(data, dict) else None) or []:
            if isinstance(entry, dict) and _store_invoice(db, mode, entry, day):
                stored += 1
        db.commit()
        done += 1
        time.sleep(1.2)  # stay inside the API's rate limit
    return done, stored, None


_inv_state: dict = {"running": False, "message": "", "finished_at": None}
_inv_lock = threading.Lock()


def _inv_job(days: int) -> None:
    gen = get_db()
    db = next(gen)
    try:
        done, stored, why = _sync_invoice_days(db, days)
        msg = f"Looked at {done} day{'' if done == 1 else 's'}; {stored} invoice{'' if stored == 1 else 's'} saved or updated."
        if why:
            msg += f" Stopped early: {why}"
        _inv_state["message"] = msg
    except Exception as e:
        db.rollback()
        log.exception("invoice sync failed")
        _inv_state["message"] = f"The invoice sync failed: {e}"
    finally:
        _inv_state["running"] = False
        _inv_state["finished_at"] = time.time()
        gen.close()


class InvoiceSyncIn(BaseModel):
    days: int = Field(30, ge=1, le=90)


@router.post("/api/invoices/sync")
def api_invoices_sync(body: InvoiceSyncIn):
    """Fetch the invoices of the last N days in the background (one API call per day, so 30 days takes about 40 seconds)."""
    _url, key = _api_settings()
    if not key:
        raise HTTPException(400, "The API key is not set. Add AUTOMATIC_DIST_API_KEY in the Render environment.")
    with _inv_lock:
        if _inv_state["running"]:
            raise HTTPException(409, "An invoice sync is already running.")
        _inv_state.update(running=True, message="Starting…")
    threading.Thread(target=_inv_job, args=(body.days,), daemon=True, name="autodist-invoice-sync").start()
    return {"running": True}


@router.get("/api/invoices/sync-status")
def api_invoices_sync_status():
    return {"running": _inv_state["running"], "message": _inv_state["message"]}


_INV_SORT = {
    "date": "i.invoice_date", "invoice": "i.invoice_number", "order": "i.order_number", "po": "i.po_number",
    "customer": "i.ship_to_name", "amount": "i.invoice_amount", "shipping": "i.special_charges",
}


def _inv_query(db: Session, q: str, date_from: str, date_to: str, link: str, invoiced: str):
    """The shared FROM / WHERE for the list, the totals and the export. Returns (from_sql, where_sql, params)."""
    cols = _po_columns(db)
    type_sql = "h2.po_type" if "po_type" in cols else "null::text"
    src_sql = "h2.source" if "source" in cols else "null::text"
    from_sql = (
        "supplier_invoices i left join lateral (select h.id, " + type_sql.replace("h2", "h") + " as po_type, " + src_sql.replace("h2", "h") + " as source "
        "from po_headers h where cast(h.po_number as text) = i.po_number and " + _PO_VENDOR + " order by h.id desc limit 1) p on true"
    )
    url, _key = _api_settings()
    where, params = ["i.supplier = :s", "i.mode = :m"], {"s": SUPPLIER, "m": _api_mode(url)}
    if q.strip():
        where.append(
            "(i.invoice_number ilike :q or i.order_number ilike :q or i.po_number ilike :q or i.ship_to_name ilike :q "
            "or i.tracking_number ilike :q or cast(i.lines as text) ilike :q)"
        )
        params["q"] = "%" + q.strip().replace("%", "").replace("_", "") + "%"
    for key, val, op in (("df", date_from, ">="), ("dt", date_to, "<=")):
        if val.strip():
            try:
                params[key] = _date.fromisoformat(val.strip())
            except ValueError:
                raise HTTPException(422, "Dates look like 2026-11-30.")
            where.append(f"i.invoice_date {op} :{key}")
    if link == "po":
        where.append("p.id is not null")
    elif link == "nopo":
        where.append("p.id is null")
    elif link == "ebay":
        where.append("p.source like 'ebay:%'")
    if invoiced == "yes":
        where.append("coalesce(i.invoice_number, '') not in ('', '0')")
    elif invoiced == "no":
        where.append("coalesce(i.invoice_number, '') in ('', '0')")
    return from_sql, " and ".join(where), params


_INV_COLS = (
    "i.id, i.order_number, i.po_number, i.invoice_number, i.invoice_date, i.invoice_amount, i.order_value, i.item_amount, "
    "i.special_charges, i.carrier_code, i.tracking_number, i.order_status, i.ship_to_name, i.ship_to_city, i.ship_to_state, "
    "jsonb_array_length(i.lines) as line_count, p.id as po_id, "
    "case when p.source like 'ebay:%' then substr(p.source, 6) end as ebay_order_id"
)


def _f(v):
    return float(v) if isinstance(v, Decimal) else v


@router.get("/api/invoices")
def api_invoices(
    q: str = Query("", max_length=80), date_from: str = Query("", alias="from", max_length=10), date_to: str = Query("", alias="to", max_length=10),
    link: str = Query("", pattern="^(|po|nopo|ebay)$"), invoiced: str = Query("", pattern="^(|yes|no)$"),
    sort: str = Query("date"), dir: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db),
):
    if not _ensure_invoices_table(db):
        raise HTTPException(503, "The invoices table could not be created.")
    from_sql, where, params = _inv_query(db, q, date_from, date_to, link, invoiced)
    order = _INV_SORT.get(sort, "i.invoice_date")
    tot = db.execute(
        text(f"select count(*), coalesce(sum(i.invoice_amount), 0), coalesce(sum(i.special_charges), 0) from {from_sql} where {where}"), params
    ).first()
    rows = db.execute(
        text(f"select {_INV_COLS} from {from_sql} where {where} order by {order} {dir} nulls last, i.id desc limit :l offset :o"),
        {**params, "l": limit, "o": offset},
    ).all()
    last = db.execute(text("select max(synced_at) from supplier_invoices where supplier = :s"), {"s": SUPPLIER}).scalar()
    return {
        "total": tot[0], "sum_invoice": _f(tot[1]), "sum_shipping": _f(tot[2]), "last_synced_at": last,
        "items": [{k: _f(v) for k, v in r._mapping.items()} for r in rows],
    }


@router.get("/api/invoices/export")
def api_invoices_export(
    q: str = Query("", max_length=80), date_from: str = Query("", alias="from", max_length=10), date_to: str = Query("", alias="to", max_length=10),
    link: str = Query("", pattern="^(|po|nopo|ebay)$"), invoiced: str = Query("", pattern="^(|yes|no)$"), db: Session = Depends(get_db),
):
    """The same filters as the list, as a CSV file (up to 20,000 invoices)."""
    if not _ensure_invoices_table(db):
        raise HTTPException(503, "The invoices table could not be created.")
    from_sql, where, params = _inv_query(db, q, date_from, date_to, link, invoiced)
    rows = db.execute(text(f"select {_INV_COLS} from {from_sql} where {where} order by i.invoice_date desc nulls last, i.id desc limit 20000"), params).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Invoice date", "Invoice #", "Order #", "Our PO #", "eBay order", "Customer", "City", "State", "Carrier", "Tracking", "Parts $", "Shipping and handling $", "Invoice $", "Order value $", "Status code"])
    for r in rows:
        w.writerow([r.invoice_date, r.invoice_number, r.order_number, r.po_number, r.ebay_order_id, r.ship_to_name, r.ship_to_city, r.ship_to_state, r.carrier_code, r.tracking_number, r.item_amount, r.special_charges, r.invoice_amount, r.order_value, r.order_status])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="autodist-invoices.csv"'})


@router.get("/api/invoices/{invoice_id}")
def api_invoice(invoice_id: int, db: Session = Depends(get_db)):
    if not _ensure_invoices_table(db):
        raise HTTPException(503, "The invoices table could not be created.")
    from_sql, where, params = _inv_query(db, "", "", "", "", "")
    row = db.execute(
        text(f"select {_INV_COLS}, i.parent_order_number, i.ship_to_zip, i.lines from {from_sql} where {where} and i.id = :i"), {**params, "i": invoice_id}
    ).first()
    if not row:
        raise HTTPException(404, "That invoice is not in the list.")
    return {k: _f(v) for k, v in row._mapping.items()}


# --------------------------------------------------------------------------
# our purchase orders to this distributor (so an order starts from a PO)
# --------------------------------------------------------------------------
# Only open POs whose vendor is Automatic Distributors are offered, and not ones already placed through the API.
# Each PO has a type (stock or drop ship); a drop ship PO's ship-to is the customer. The PO tables belong to
# app/admin_purchase_orders.py; this only reads them, and writes back the order number once an order is placed.
_PO_VENDOR = "(h.vendor_name ilike '%automatic dist%' or h.vendor_name ilike '%autodist%')"


def _po_columns(db: Session) -> set[str]:
    rows = db.execute(
        text("select column_name from information_schema.columns where table_schema = current_schema() and table_name = 'po_headers'")
    ).all()
    return {r[0] for r in rows}


def _parse_ship_to(txt: Optional[str]) -> dict:
    """Best effort: a PO's ship-to is free text (name, street, 'City, ST 12345'). Anything not recognised is left blank to type in."""
    lines = [l.strip() for l in (txt or "").splitlines() if l.strip()]
    out: dict = {}
    for l in list(lines):
        m = re.match(r"(?i)^(?:phone|tel|ph)[:.\s]+(.+)$", l)
        if m:
            out["phone"] = m.group(1).strip()
            lines.remove(l)
    if lines and re.fullmatch(r"(?i)(usa|us|u\.s\.a?\.?|united states( of america)?)", lines[-1]):
        lines.pop()
    if lines:
        m = re.match(r"^(.*?),?\s+([A-Za-z]{2})\.?\s+(\d{5}(?:-\d{4})?)$", lines[-1])
        if m:
            out.update({"city": m.group(1).strip(), "state": m.group(2).upper(), "zipCode": m.group(3)})
            lines.pop()
    if lines:
        out["name"] = lines[0]
        for key, val in zip(("addressLine1", "addressLine2", "addressLine3"), lines[1:4]):
            out[key] = val
    return out


@router.get("/api/pos")
def api_pos(db: Session = Depends(get_db)):
    """Open purchase orders to Automatic Distributors that have not been placed through the API yet."""
    cols = _po_columns(db)
    type_sel = "coalesce(h.po_type, 'stock')" if "po_type" in cols else "'stock'"
    url, _key = _api_settings()
    mode = _api_mode(url)
    sql = (
        f"select h.id, h.po_number, h.vendor_name, h.sent_at, h.created_at, {type_sel} as po_type, "
        f"(select count(*) from po_lines l where l.po_id = h.id and l.received_at is null) as open_lines "
        f"from po_headers h where h.status = 'open' and {_PO_VENDOR} %s order by h.po_number desc limit 100"
    )
    placed = (
        "and not exists (select 1 from supplier_orders o where o.supplier = :s and o.mode = :m "
        "and o.po_number = cast(h.po_number as text) and o.order_number <> '')"
    )
    try:
        rows = db.execute(text(sql % placed), {"s": SUPPLIER, "m": mode}).all()
    except Exception:
        db.rollback()  # the orders table is not there yet: show every open PO
        rows = db.execute(text(sql % ""), {}).all()
    return {"mode": mode, "items": [dict(r._mapping) for r in rows]}


def _supplier_matches(db: Session, item: Optional[str], barcode: Optional[str], label: Optional[str] = None) -> tuple[list[dict], Optional[str]]:
    """Automatic Distributors parts that could be this PO line: by barcode first, then part number, then the eBay custom label SKU (the same rule as the match report)."""
    upc = re.sub(r"\D", "", barcode or "").lstrip("0")
    mpn = re.sub(r"[^A-Za-z0-9]", "", item or "").upper()
    cols = "s.sku, s.manufacturer, s.name, (s.qty_indiana + s.qty_nevada) as stock, s.effective_cost as cost"
    found: list[dict] = []
    if len(upc) >= 8:
        found = [dict(r._mapping) for r in db.execute(text(f"select {cols} from supplier_items s where s.supplier = :s and s.active and s.upc_norm = :u order by s.sku limit 8"), {"s": SUPPLIER, "u": upc}).all()]
    if not found and mpn:
        found = [dict(r._mapping) for r in db.execute(text(f"select {cols} from supplier_items s where s.supplier = :s and s.active and s.mfr_number_norm = :m order by s.sku limit 8"), {"s": SUPPLIER, "m": mpn}).all()]
    if not found and label:
        toks = label_tokens(label)[:6]
        if toks:
            both = sorted({t.strip() for t in toks} | {t.strip().upper() for t in toks})
            found = [dict(r._mapping) for r in db.execute(
                text(f"select {cols} from supplier_items s where s.supplier = :s and s.active and s.sku = any(cast(:t as text[])) order by s.sku limit 8"),
                {"s": SUPPLIER, "t": both},
            ).all()]
            # A label with several pieces is a kit: never auto-pick one piece for it, let staff choose.
            if len(toks) > 1:
                return found, None
    pick = found[0]["sku"] if len(found) == 1 else None
    return found, pick


@router.get("/api/pos/{po_id}")
def api_po(po_id: int, db: Session = Depends(get_db)):
    """One PO ready to order from: its open lines matched to Automatic Distributors part numbers, and its ship-to split into fields."""
    cols = _po_columns(db)
    type_sel = "coalesce(h.po_type, 'stock')" if "po_type" in cols else "'stock'"
    src_sel = "h.source" if "source" in cols else "null"
    h = db.execute(
        text(f"select h.id, h.po_number, h.vendor_name, h.ship_to, h.note, h.sent_at, h.status, {type_sel} as po_type, {src_sel} as source from po_headers h where h.id = :i"),
        {"i": po_id},
    ).first()
    if not h:
        raise HTTPException(404, "That purchase order does not exist.")
    if h.status != "open":
        raise HTTPException(409, f"PO {h.po_number} is {h.status}, so it cannot be ordered.")
    rows = db.execute(
        text(
            "select l.id, l.item, l.description, l.quantity, l.rate, p.barcode, p.custom_label_sku from po_lines l "
            "left join products p on p.id = l.product_id where l.po_id = :i and l.received_at is null order by l.id"
        ),
        {"i": po_id},
    ).all()
    lines = []
    for r in rows:
        cands, pick = _supplier_matches(db, r.item, r.barcode, r.custom_label_sku)
        lines.append(
            {
                "line_id": r.id,
                "item": r.item,
                "description": r.description,
                "quantity": r.quantity,
                "rate": float(r.rate) if r.rate is not None else None,
                "number": pick or "",
                "candidates": cands,
            }
        )
    return {
        "id": h.id,
        "po_number": h.po_number,
        "po_type": h.po_type,
        "source": h.source,
        "vendor_name": h.vendor_name,
        "note": h.note,
        "sent_at": h.sent_at,
        "ship_to_text": h.ship_to,
        "ship_to": _parse_ship_to(h.ship_to),
        "lines": lines,
    }


def _mark_po_placed(db: Session, po_id: int, po_number: str, order_number: str) -> bool:
    """After an order is placed: mark the PO sent and keep the distributor's order number on it."""
    try:
        sets = "sent_at = coalesce(sent_at, now()), updated_at = now()"
        params: dict = {"i": po_id, "pn": po_number}
        if "supplier_order_number" in _po_columns(db):
            sets += ", supplier_order_number = :o"
            params["o"] = order_number
        got = db.execute(
            text(f"update po_headers set {sets} where id = :i and cast(po_number as text) = :pn and status <> 'cancelled' returning id"),
            params,
        ).first()
        db.commit()
        return got is not None
    except Exception:
        db.rollback()
        log.exception("autodist order placed but PO %s could not be updated", po_number)
        return False


# --------------------------------------------------------------------------
# opportunities: which parts look most worth selling (read only)
# --------------------------------------------------------------------------
# Automatic Distributors' feed carries no sales or popularity data, so "popular" can only come from OUR OWN sales: eBay sales
# lines that are already matched to one of our products. That table is found by its columns (product_id, quantity,
# ebay_order_id) instead of by name; when it cannot be found the "Sells for us" list is simply left out.
# Selling price used for margin: Automatic Distributors' MAP price when it sets one, otherwise the list price.
_sales_table_cache: dict = {}


def _sales_table(db: Session) -> Optional[str]:
    if "t" in _sales_table_cache:
        return _sales_table_cache["t"]
    found = None
    try:
        rows = db.execute(
            text(
                "select table_name from information_schema.columns where table_schema = 'public' "
                "and column_name in ('product_id', 'quantity', 'ebay_order_id') "
                "group by table_name having count(distinct column_name) = 3 order by (table_name like '%line%') desc, table_name"
            )
        ).all()
        if rows and re.fullmatch(r"[a-z_][a-z0-9_]*", rows[0][0]):
            found = rows[0][0]
    except Exception:
        db.rollback()
    _sales_table_cache["t"] = found
    return found


_OPP_COLS = (
    "id, sku, manufacturer, name, images[1] as image, price, price_basis, cost, margin, margin_pct, qty_total, "
    "warehouses, is_ours, special_order, ship_weight_lb"
)


@router.get("/opportunities")
def opportunities(
    min_margin_pct: float = Query(20, ge=0, le=100, description="hide parts below this margin (percent of selling price)"),
    max_margin_pct: float = Query(80, ge=0, le=100, description="hide parts above this: usually a placeholder list price, not a real margin"),
    min_margin: float = Query(5, ge=0, description="hide parts that earn less than this per unit, in dollars"),
    in_stock: bool = Query(True, description="only parts Automatic Distributors has in stock"),
    hide_special_order: bool = Query(True),
    ours: str = Query("all", pattern="^(all|yes|no)$", description="no = parts we do not sell yet"),
    brand: Optional[str] = Query(None, max_length=120),
    limit: int = Query(25, ge=5, le=100),
    db: Session = Depends(get_db),
):
    params = {
        "s": SUPPLIER, "minp": min_margin_pct, "maxp": max_margin_pct, "minm": min_margin, "stock": in_stock,
        "hide_so": hide_special_order, "ours": ours, "brand": (brand or "").strip() or None, "n": limit,
    }
    head = f"""
    with {_OURS_CTE},
    base as materialized (
      select s.id, s.sku, s.manufacturer, s.name, s.images, s.special_order, s.ship_weight_lb,
             coalesce(s.map_price, s.list_price) as price,
             case when s.map_price is not null then 'MAP' else 'list' end as price_basis,
             s.effective_cost as cost, {_TOTAL_QTY} as qty_total,
             ((s.qty_indiana > 0)::int + (s.qty_nevada > 0)::int) as warehouses,
             {_IS_OURS} as is_ours
      from supplier_items s
      where s.supplier = :s and s.active and s.effective_cost > 0 and coalesce(s.map_price, s.list_price) > s.effective_cost
    ), scored as materialized (
      select b.*, (b.price - b.cost) as margin, ((b.price - b.cost) / b.price * 100) as margin_pct
      from base b
      where (not :stock or b.qty_total > 0)
        and (not :hide_so or not coalesce(b.special_order, false))
        and (cast(:brand as text) is null or b.manufacturer ilike '%' || cast(:brand as text) || '%')
        and (:ours = 'all' or (:ours = 'yes') = b.is_ours)
    ), good as materialized (
      select * from scored where margin_pct >= :minp and margin_pct <= :maxp and margin >= :minm
    )
    """

    def top(order: str) -> list[dict]:
        rows = db.execute(text(f"{head} select {_OPP_COLS} from good order by {order} limit :n"), params).all()
        return [dict(r._mapping) for r in rows]

    lists = {
        "best_bets": top("margin * least(qty_total, 10) / 10.0 desc, margin desc"),
        "best_margin": top("margin desc, margin_pct desc"),
        "best_margin_pct": top("margin_pct desc, margin desc"),
    }
    totals = db.execute(
        text(f"{head} select (select count(*) from base) as with_margin, (select count(*) from scored) as after_filters, "
             f"(select count(*) from good) as qualifying"),
        params,
    ).first()
    brands = db.execute(
        text(
            f"""{head}
            select manufacturer, count(*) as parts, count(*) filter (where qty_total > 0) as in_stock_parts,
                   avg(margin_pct) as avg_margin_pct, avg(margin) as avg_margin,
                   sum(margin * least(qty_total, 10)) as potential
            from good where manufacturer is not null
            group by manufacturer having count(*) >= 3
            order by potential desc nulls last limit 40"""
        ),
        params,
    ).all()

    sales = None
    table = _sales_table(db)
    if table:
        try:
            sold = db.execute(
                text(
                    f"""
                    with prod_sold as (
                      select p.id as product_id, p.sku_display as our_sku, p.title as our_title, x.units,
                             nullif(ltrim(regexp_replace(coalesce(p.barcode, ''), '[^0-9]', '', 'g'), '0'), '') as upc_n,
                             nullif(upper(regexp_replace(coalesce({_MPN}, ''), '[^A-Za-z0-9]', '', 'g')), '') as mpn_n
                      from (select product_id, sum(quantity) as units from {table} where product_id is not null group by 1) x
                      join products p on p.id = x.product_id
                    )
                    select ps.product_id, ps.our_sku, ps.our_title, ps.units,
                           m.id, m.sku, m.manufacturer, m.images[1] as image, m.effective_cost as cost,
                           coalesce(m.map_price, m.list_price) as price,
                           case when m.map_price is not null then 'MAP' else 'list' end as price_basis,
                           (coalesce(m.map_price, m.list_price) - m.effective_cost) as margin,
                           ((coalesce(m.map_price, m.list_price) - m.effective_cost) / nullif(coalesce(m.map_price, m.list_price), 0) * 100) as margin_pct,
                           ({_TOTAL_QTY.replace('s.', 'm.')}) as qty_total
                    from prod_sold ps
                    left join lateral (
                      select s.* from supplier_items s
                      where s.supplier = :s and s.active
                        and ((ps.upc_n is not null and length(ps.upc_n) >= 8 and s.upc_norm = ps.upc_n)
                             or (ps.mpn_n is not null and s.mfr_number_norm = ps.mpn_n))
                      order by (s.upc_norm = ps.upc_n) desc nulls last, s.id limit 1
                    ) m on true
                    order by ps.units desc nulls last limit :n
                    """
                ),
                {"s": SUPPLIER, "n": limit},
            ).all()
            sales = {"table": table, "rows": [dict(r._mapping) for r in sold]}
        except Exception:
            db.rollback()
            log.exception("autodist opportunities: could not read our sales from %s", table)
            sales = None

    return {
        "totals": dict(totals._mapping),
        "lists": lists,
        "brands": [dict(b._mapping) for b in brands],
        "sales": sales,
    }


@router.get("/catalog/overview")
def catalog_overview(db: Session = Depends(get_db)):
    """The catalog at a glance: totals, data gaps, stock by warehouse, price bands, units of measure and every brand."""
    t = db.execute(
        text(
            f"""
            select count(*) as total,
                   count(*) filter (where s.active) as active,
                   count(*) filter (where not s.active) as retired,
                   count(*) filter (where s.active and {_TOTAL_QTY} > 0) as in_stock,
                   count(*) filter (where s.active and {_TOTAL_QTY} = 0) as out_of_stock,
                   count(*) filter (where s.active and s.qty_indiana > 0) as in_indiana,
                   count(*) filter (where s.active and s.qty_nevada > 0) as in_nevada,
                   coalesce(sum(s.qty_indiana) filter (where s.active), 0) as units_indiana,
                   coalesce(sum(s.qty_nevada) filter (where s.active), 0) as units_nevada,
                   coalesce(sum(s.effective_cost * {_TOTAL_QTY}) filter (where s.active), 0) as stock_value_at_cost,
                   coalesce(sum(s.list_price * {_TOTAL_QTY}) filter (where s.active), 0) as stock_value_at_list,
                   count(distinct s.manufacturer) filter (where s.active) as brands,
                   count(*) filter (where s.active and s.upc_norm is null) as no_barcode,
                   count(*) filter (where s.active and s.images is null) as no_photo,
                   count(*) filter (where s.active and s.mfr_number is null) as no_mfr_number,
                   count(*) filter (where s.active and s.name is null) as no_name,
                   count(*) filter (where s.active and s.effective_cost is null) as no_cost,
                   count(*) filter (where s.active and s.list_price is null) as no_list_price,
                   count(*) filter (where s.active and s.list_price < s.effective_cost) as list_below_cost,
                   count(*) filter (where s.active and s.updated_at >= now() - interval '7 days') as changed_7d,
                   count(*) filter (where s.active and s.updated_at >= now() - interval '30 days') as changed_30d,
                   avg((s.list_price - s.effective_cost) / nullif(s.list_price, 0) * 100)
                       filter (where s.active and s.list_price > 0 and s.effective_cost is not null) as avg_margin_pct,
                   max(s.last_seen_at) as last_seen
            from supplier_items s where s.supplier = :s
            """
        ),
        {"s": SUPPLIER},
    ).first()
    brands = db.execute(
        text(
            f"""
            select coalesce(s.manufacturer, '(no brand)') as brand, s.manufacturer is null as unbranded,
                   count(*) as parts, count(*) filter (where {_TOTAL_QTY} > 0) as in_stock,
                   coalesce(sum({_TOTAL_QTY}), 0) as units,
                   coalesce(sum(s.effective_cost * {_TOTAL_QTY}), 0) as stock_value_at_cost,
                   min(s.effective_cost) as min_cost, max(s.effective_cost) as max_cost, avg(s.effective_cost) as avg_cost
            from supplier_items s where s.supplier = :s and s.active
            group by s.manufacturer order by count(*) desc, s.manufacturer limit 3000
            """
        ),
        {"s": SUPPLIER},
    ).all()
    bands = db.execute(
        text(
            """
            select case when effective_cost is null then 'No cost'
                        when effective_cost < 5 then 'Under $5'
                        when effective_cost < 25 then '$5 – $25'
                        when effective_cost < 100 then '$25 – $100'
                        when effective_cost < 250 then '$100 – $250'
                        when effective_cost < 500 then '$250 – $500'
                        else '$500 and up' end as band,
                   case when effective_cost is null then 99 when effective_cost < 5 then 1 when effective_cost < 25 then 2
                        when effective_cost < 100 then 3 when effective_cost < 250 then 4 when effective_cost < 500 then 5 else 6 end as ord,
                   count(*) as parts
            from supplier_items where supplier = :s and active group by 1, 2 order by 2
            """
        ),
        {"s": SUPPLIER},
    ).all()
    uoms = db.execute(
        text(
            "select coalesce(unit_of_measure, '(none)') as uom, unit_of_measure is null as unset, count(*) as parts "
            "from supplier_items where supplier = :s and active group by unit_of_measure order by count(*) desc limit 50"
        ),
        {"s": SUPPLIER},
    ).all()
    return {
        "totals": dict(t._mapping),
        "brands": [dict(b._mapping) for b in brands],
        "price_bands": [{"band": b.band, "parts": b.parts} for b in bands],
        "units_of_measure": [dict(u._mapping) for u in uoms],
    }
