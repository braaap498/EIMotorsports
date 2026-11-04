"""
Automatic Distributors (Autodist) supplier data.

Mounted at /admin/autodist/* (owner/admin only).

  GET  /summary                what is stored, and the last imports
  POST /import/start           begin an import (the browser then sends the file in pieces)
  POST /import/chunk           one piece of the feed file
  POST /import/finish          check the whole file arrived, then retire parts that left the feed
  GET  /ftp/status             is the Autodist FTP login set up on the server (nothing is contacted)
  POST /ftp/check              log in to the Autodist FTP server, list the folders, preview the inventory file
  POST /import/ftp             pull the live inventory file from the FTP server and import it (runs in the background)
  GET  /ftp/schedule           the automatic-sync setting (off by default) and what the last check found
  PUT  /ftp/schedule           turn automatic sync on/off and set how often the server checks (minutes)
  GET  /items                  browse / search / filter / sort the stored supplier parts (see items() for every filter)
  GET  /items/{id}             one part in full: every stored field, our matching products, same-barcode / same-number parts
  GET  /catalog/overview       the whole catalog at a glance: totals, data gaps, stock by warehouse, brands, price bands
  GET  /match-report           how many of OUR products match a supplier part (changes nothing)

HOW IT FITS
  * Supplier stock lives in its own tables (supplier_items). It is NEVER added to products.quantity_on_hand:
    that number drives reservations, picking and reordering (app/stock.py) and must only count parts on
    our shelf.
  * `upsert_rows` is the one door the data goes through. Today the rows come from the feed file; when the
    Autodist API is wired in, its adapter should build the same dicts (see clean_row) and call
    `upsert_rows` - nothing else here changes. The API key belongs in the server's settings
    (autodist_api_key in app/config.py), never in the browser.
  * The FTP login is read from Render environment variables (AUTODIST_FTP_USER, AUTODIST_FTP_PASSWORD, and
    optionally AUTODIST_FTP_HOST, AUTODIST_FTP_INVENTORY_FILE, AUTODIST_FTP_TLS), never stored in the database
    or sent to the browser.

LICENSE (Product Data License Agreement)
  This is licensed data. Dealer cost and stock are confidential (7.3) and must not be made available in
  bulk (4(c)). So: every route here is admin-only, nothing here is mounted on the public store API, and
  the tables are closed to the public Supabase roles (see sql/autodist_tables.sql). All Autodist data sits
  in supplier_items / supplier_import_runs so it can be deleted in one step if the agreement ends (11.2).
"""
import csv
import ftplib
import io
import logging
import os
import re
import tempfile
import threading
import time
import zipfile
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
# pulling the live inventory file from Autodist's FTP server
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


def map_headers(header: list[str]) -> dict[str, int]:
    """Header cells -> {our field: column index}. Raises 400 when there is no SKU column."""
    by_norm = {}
    for i, h in enumerate(header):
        by_norm.setdefault(_norm_header(h), i)
    out: dict[str, int] = {}
    for field, names in _ALIASES.items():
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
        raise HTTPException(400, "The Autodist FTP login is not set. Add AUTODIST_FTP_USER and AUTODIST_FTP_PASSWORD in the Render environment.")
    use_tls = os.environ.get("AUTODIST_FTP_TLS", "").strip().lower() in ("1", "true", "yes")
    ftp: ftplib.FTP = ftplib.FTP_TLS(timeout=_FTP_TIMEOUT) if use_tls else ftplib.FTP(timeout=_FTP_TIMEOUT)
    try:
        ftp.connect(host, 21)
        ftp.login(user, pw)
        if use_tls:
            ftp.prot_p()  # type: ignore[attr-defined]
        ftp.set_pasv(True)
    except ftplib.error_perm:
        raise HTTPException(502, "Autodist's FTP server refused the username or password.")
    except Exception as e:  # network, timeout, DNS
        log.warning("autodist ftp connect failed: %s", e)
        raise HTTPException(502, f"Could not reach the Autodist FTP server ({host}): {e}")
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


def _rows_from_stream(text_stream, mapping: Optional[dict] = None, delimiter: Optional[str] = None):
    """Yield (clean_row | None) per data line of the feed (None = unreadable line)."""
    reader = csv.reader(text_stream, delimiter=delimiter or ",")
    header = next(reader, None)
    if header is None:
        return
    mp = mapping or map_headers(header)
    width = len(header)
    for rec in reader:
        if not rec or all(not c.strip() for c in rec):
            continue
        if len(rec) != width:
            yield None
            continue
        raw = {f: rec[i] for f, i in mp.items()}
        yield clean_row(raw)


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
    """A cheap fingerprint of the remote file (its modified time and size) so a check can tell whether Autodist
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
               signature: Optional[str] = None, changed: bool = False) -> None:
    """Record what the last check / import found. Never lets a missing settings table break an import."""
    try:
        db.execute(
            text(
                "update supplier_sync_settings set last_summary = coalesce(:sm, last_summary), last_error = :er, "
                "last_signature = coalesce(:sg, last_signature), "
                "last_changed_at = case when :ch then now() else last_changed_at end, updated_at = now() "
                "where supplier = :s"
            ),
            {"sm": summary, "er": (error or None) and error[:400], "sg": signature, "ch": changed, "s": SUPPLIER},
        )
        db.commit()
    except Exception:
        db.rollback()
        log.warning("could not record the Autodist sync result (is sql/autodist_sync_settings.sql applied?)")


def _ftp_job(run_id: int) -> None:
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
    base = {"top": top, "price_files": price_files, "inventory_file": path, "login_folder": where, "raw_listing": raw, "error": error}
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


@router.post("/import/ftp")
def import_ftp(db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Start pulling the live inventory file from the FTP server. Returns at once; the import runs in the background
    and shows up in the list of imports."""
    _, u, pw = _ftp_settings()
    if not u or not pw:
        raise HTTPException(400, "The Autodist FTP login is not set. Add AUTODIST_FTP_USER and AUTODIST_FTP_PASSWORD in the Render environment.")
    run_id = _begin_run(db, "ftp", user.email or user.user_id)
    threading.Thread(target=_ftp_job, args=(run_id,), daemon=True, name=f"autodist-ftp-{run_id}").start()
    return {"run_id": run_id}


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


def _auto_tick() -> None:
    gen = get_db()
    db = next(gen)
    try:
        claimed = db.execute(
            text(
                "update supplier_sync_settings set last_checked_at = now() where supplier = :s and auto_enabled "
                "and (last_checked_at is null or last_checked_at <= now() - make_interval(mins => interval_minutes)) "
                "returning last_signature"
            ),
            {"s": SUPPLIER},
        ).first()
        db.commit()
        if claimed is None:
            return  # off, not due yet, or another copy of the server took this check
        last_sig = claimed[0]
        try:
            ftp = _ftp_connect()
            try:
                sig = _remote_signature(ftp, _find_inventory_file(ftp))
            finally:
                try:
                    ftp.quit()
                except Exception:
                    ftp.close()
        except HTTPException as e:
            _sync_note(db, error=str(e.detail))
            return
        if sig != "-|-" and sig == last_sig:
            _sync_note(db, summary="Checked: Autodist's file has not changed since the last import.")
            return
        try:
            run_id = _begin_run(db, "ftp_auto", "automatic sync")
        except HTTPException:
            return  # an import is already running
        _ftp_job(run_id)
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


def _schedule_row(db: Session) -> dict:
    row = db.execute(
        text(
            "select auto_enabled, interval_minutes, last_checked_at, last_changed_at, last_summary, last_error "
            "from supplier_sync_settings where supplier = :s"
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
_TOTAL_QTY = "(s.qty_maine + s.qty_indiana + s.qty_nevada)"
_SORTS = {
    "brand": "s.manufacturer",
    "sku": "s.sku",
    "mfr": "s.mfr_number",
    "name": "s.name",
    "uom": "s.unit_of_measure",
    "list": "s.list_price",
    "cost": "s.dealer_cost",
    "margin": "(s.list_price - s.dealer_cost)",
    "margin_pct": "((s.list_price - s.dealer_cost) / nullif(s.list_price, 0))",
    "stock": _TOTAL_QTY,
    "maine": "s.qty_maine",
    "indiana": "s.qty_indiana",
    "nevada": "s.qty_nevada",
    "updated": "s.updated_at",
    "seen": "s.last_seen_at",
}
_ITEM_COLS = (
    "s.id, s.sku, s.mfr_number, s.mfr_number_norm, s.manufacturer, s.name, s.unit_of_measure, s.list_price, "
    "s.dealer_cost, s.qty_maine, s.qty_indiana, s.qty_nevada, s.upc, s.upc_norm, s.active, s.updated_at, s.last_seen_at"
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
        where.append({"maine": "s.qty_maine", "indiana": "s.qty_indiana", "nevada": "s.qty_nevada"}[warehouse] + " > 0")
    for key, expr, op, val in (
        ("min_qty", _TOTAL_QTY, ">=", min_qty), ("max_qty", _TOTAL_QTY, "<=", max_qty),
        ("min_cost", "s.dealer_cost", ">=", min_cost), ("max_cost", "s.dealer_cost", "<=", max_cost),
        ("min_list", "s.list_price", ">=", min_list), ("max_list", "s.list_price", "<=", max_list),
    ):
        if val is not None:
            params[key] = val
            where.append(f"{expr} {op} :{key}")
    pct = "((s.list_price - s.dealer_cost) / nullif(s.list_price, 0) * 100)"
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
        "cost": "s.dealer_cost is null",
        "list": "s.list_price is null",
        "below_cost": "s.list_price is not null and s.dealer_cost is not null and s.list_price < s.dealer_cost",
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
    warehouse: Optional[str] = Query(None, pattern="^(maine|indiana|nevada)$", description="stock at that warehouse"),
    min_qty: Optional[int] = Query(None, ge=0),
    max_qty: Optional[int] = Query(None, ge=0),
    min_cost: Optional[float] = Query(None, ge=0),
    max_cost: Optional[float] = Query(None, ge=0),
    min_list: Optional[float] = Query(None, ge=0),
    max_list: Optional[float] = Query(None, ge=0),
    min_margin_pct: Optional[float] = Query(None, description="(list - cost) / list, in percent"),
    max_margin_pct: Optional[float] = Query(None),
    uom: Optional[str] = Query(None, max_length=40),
    missing: Optional[str] = Query(None, pattern="^(upc|mfr|name|cost|list|below_cost)$", description="data gaps to find"),
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
            f"count(distinct s.manufacturer) as brands, min(s.dealer_cost) as min_cost, max(s.dealer_cost) as max_cost, "
            f"avg(s.dealer_cost) as avg_cost, coalesce(sum(s.dealer_cost * {_TOTAL_QTY}), 0) as stock_value_at_cost, "
            f"coalesce(sum({_TOTAL_QTY}), 0) as units from supplier_items s where {clause}"
        ),
        params,
    ).first()
    page = db.execute(
        text(
            f"{with_clause} select {_ITEM_COLS}, {_TOTAL_QTY} as qty_total, (s.list_price - s.dealer_cost) as margin, "
            f"((s.list_price - s.dealer_cost) / nullif(s.list_price, 0) * 100) as margin_pct "
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

    cols = "id, sku, mfr_number, manufacturer, name, list_price, dealer_cost, qty_maine, qty_indiana, qty_nevada, upc, active"
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
                   count(*) filter (where s.active and s.qty_maine > 0) as in_maine,
                   count(*) filter (where s.active and s.qty_indiana > 0) as in_indiana,
                   count(*) filter (where s.active and s.qty_nevada > 0) as in_nevada,
                   coalesce(sum(s.qty_maine) filter (where s.active), 0) as units_maine,
                   coalesce(sum(s.qty_indiana) filter (where s.active), 0) as units_indiana,
                   coalesce(sum(s.qty_nevada) filter (where s.active), 0) as units_nevada,
                   coalesce(sum(s.dealer_cost * {_TOTAL_QTY}) filter (where s.active), 0) as stock_value_at_cost,
                   coalesce(sum(s.list_price * {_TOTAL_QTY}) filter (where s.active), 0) as stock_value_at_list,
                   count(distinct s.manufacturer) filter (where s.active) as brands,
                   count(*) filter (where s.active and s.upc_norm is null) as no_barcode,
                   count(*) filter (where s.active and s.mfr_number is null) as no_mfr_number,
                   count(*) filter (where s.active and s.name is null) as no_name,
                   count(*) filter (where s.active and s.dealer_cost is null) as no_cost,
                   count(*) filter (where s.active and s.list_price is null) as no_list_price,
                   count(*) filter (where s.active and s.list_price < s.dealer_cost) as list_below_cost,
                   count(*) filter (where s.active and s.updated_at >= now() - interval '7 days') as changed_7d,
                   count(*) filter (where s.active and s.updated_at >= now() - interval '30 days') as changed_30d,
                   avg((s.list_price - s.dealer_cost) / nullif(s.list_price, 0) * 100)
                       filter (where s.active and s.list_price > 0 and s.dealer_cost is not null) as avg_margin_pct,
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
                   coalesce(sum(s.dealer_cost * {_TOTAL_QTY}), 0) as stock_value_at_cost,
                   min(s.dealer_cost) as min_cost, max(s.dealer_cost) as max_cost, avg(s.dealer_cost) as avg_cost
            from supplier_items s where s.supplier = :s and s.active
            group by s.manufacturer order by count(*) desc, s.manufacturer limit 3000
            """
        ),
        {"s": SUPPLIER},
    ).all()
    bands = db.execute(
        text(
            """
            select case when dealer_cost is null then 'No cost'
                        when dealer_cost < 5 then 'Under $5'
                        when dealer_cost < 25 then '$5 – $25'
                        when dealer_cost < 100 then '$25 – $100'
                        when dealer_cost < 250 then '$100 – $250'
                        when dealer_cost < 500 then '$250 – $500'
                        else '$500 and up' end as band,
                   case when dealer_cost is null then 99 when dealer_cost < 5 then 1 when dealer_cost < 25 then 2
                        when dealer_cost < 100 then 3 when dealer_cost < 250 then 4 when dealer_cost < 500 then 5 else 6 end as ord,
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
