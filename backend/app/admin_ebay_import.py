"""
eBay listing import: the back end of the admin "eBay listing import" tab.

Mounted at /admin/ebay-import/* (owner/admin only).

  POST /report   {store, csv}   saves a Seller Hub "all active listings" report (title, price, label) for a store
  GET  /state                   which reports are on file, and how many listings still need asking eBay about
  GET  /preview                 read-only: what an import would do right now (report mode)
  POST /run      {limit}        creates products for eBay listings that are not linked to a product yet (report mode)
  POST /run-api  {limit}        same, but asks eBay about each listing instead of using a report (no report loaded)

Which listings count as "not a product yet": every item number in ebay_label_import (filled by the photo
import and the label report upload) or in an uploaded report, whose number is NOT in channel_listings and NOT
in products.ebay_item_id (prefixes ignored, the same comparison the Link tab uses).

For each one the run does what "Create product" on the Link tab does, plus the pieces that tab does not do:

  * products row: title, slug, ebay_item_id (EBAY-<number>), custom_label_sku (when there is one and it is not
    the item number), list_price (from the report), quantity_on_hand = 0, unpublished, active. The SKU is left
    to the database default, exactly like the Link tab. Stock is 0 because there is no inventory history for
    these parts; eBay's "available quantity" is kept in ebay_report_rows for reference only.
  * channel_listings row, written the same way the Link tab writes it (same channel, same id format, store).
  * the unlinked photo rows saved by the photo import (product_photos with no product, stored under
    EBAY-<number>/) are attached to the new product.
  * compat data saved by the compat import (ebay_compat_items) is copied into ebay_compat_raw for the new
    listing, so the fitment scripts (fitment_5 ...) see these products like the older ones. Fitment itself is
    NOT applied here.

A listing is skipped (and reported) when its store or title cannot be worked out. The store and title come from
an uploaded report, else from what eBay said (run-api), else from the compat import. Each listing is saved on its own, so one
bad listing never blocks the rest, and re-running only picks up what is left.
"""
import csv
import io
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.admin_catalog import _unique_slug, slugify
from app.admin_ebay_listings import _resolve
from app.admin_ebay_new import _stored_prefix
from app.admin_ebay_compat import DELAY, NOT_FOUND_CODES, RATE_LIMIT_CODE
from app.admin_ebay_photos import RateLimited, _chunks, _clean_item, _norm
from app.auth import AuthUser, require_admin
from app.database import get_db
from app.ebay_edit import NS, _errors, _trading
from app.ebay_oauth import _expected_username
from app.skus import clean_ebay_item_id

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-import", tags=["admin-ebay-import"], dependencies=[Depends(require_admin)])

MAX_RUN = 500
DEFAULT_RUN = 200
COMMIT_EVERY = 50

# Unlinked listings with whatever we know about each (one row per item number).
# Title, store, price and label come from, in order: an uploaded report, what eBay said (run-api), the compat import.
_CANDIDATES = """
with ids as (
  select cast(item_number as text) as item from ebay_label_import
  union
  select item_id from ebay_report_rows
  union
  select item_id from ebay_api_details
),
open_ids as (
  select item from ids i
  where not exists (select 1 from channel_listings cl
                    where regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = i.item)
    and not exists (select 1 from products p
                    where p.ebay_item_id is not null
                      and regexp_replace(cast(p.ebay_item_id as text), '[^0-9]', '', 'g') = i.item)
)
select o.item,
       coalesce(nullif(trim(r.custom_label_sku), ''), nullif(trim(a.custom_label_sku), ''),
                nullif(trim(cast(l.custom_label_sku as text)), '')) as label,
       coalesce(r.store, a.store, c.store, nullif(trim(cast(l.store as text)), '')) as store,
       coalesce(nullif(trim(r.title), ''), nullif(trim(a.title), ''), nullif(trim(c.title), '')) as title,
       coalesce(r.price, a.price) as price,
       (r.item_id is not null) as in_report,
       r.listing_status as listing_status,
       a.status as api_status,
       a.reason as api_reason,
       c.store as compat_store,
       exists (select 1 from product_photos pp
               where pp.product_id is null and split_part(pp.storage_path, '/', 1) = 'EBAY-' || o.item) as has_photos,
       (c.item_id is not null and c.status = 'ok') as has_compat
from open_ids o
left join ebay_report_rows r on r.item_id = o.item
left join ebay_api_details a on a.item_id = o.item
left join ebay_compat_items c on c.item_id = o.item
left join ebay_label_import l on cast(l.item_number as text) = o.item
"""


def _usable(r) -> bool:
    return bool(r.store and r.title and r.api_status != "skipped")


# ------------------------------------------------------------------ report upload
_TITLE = {"title", "itemtitle"}
_PRICE = ["currentprice", "startprice", "price"]
_QTY = {"availablequantity", "quantity"}
_CAT = {"ebaycategory1name", "categoryname"}
_LABEL = {"customlabelsku", "customlabel", "sku"}
_ITEM = {"itemnumber", "itemid", "ebayitemid", "ebayitemnumber", "item"}


class ReportIn(BaseModel):
    store: str = ""
    csv: str
    status: str = ""  # "active", "ended" or "scheduled" (the page works it out from the file name); blank = unknown


_REPORT_STATUS = {"active", "ended", "scheduled"}


def _link_status(r) -> str:
    """The status written on the new eBay link, in the words the existing links already use."""
    ls = getattr(r, "listing_status", None)
    if ls in ("active", "ended"):
        return ls
    if getattr(r, "api_status", None) == "ok":  # eBay itself just confirmed the listing is live
        return "active"
    return "draft"  # scheduled, or not known which kind of report it came from


def _money(v: str) -> Optional[Decimal]:
    v = (v or "").replace("$", "").replace(",", "").strip()
    try:
        d = Decimal(v)
    except InvalidOperation:
        return None
    return d if 0 <= d < Decimal("100000000") else None


@router.post("/report")
def upload_report(body: ReportIn, db: Session = Depends(get_db)):
    info = _resolve(db, body.store)
    if not info["known"]:
        raise HTTPException(404, f"'{info['store']}' is not in the store list yet. Add it on the Live listings tab first.")
    store = info["store"]
    list_status = body.status.strip().lower() if body.status.strip().lower() in _REPORT_STATUS else None

    rows = list(csv.reader(io.StringIO(body.csv.lstrip("\ufeff"))))
    head_at = next(
        (i for i, r in enumerate(rows[:30]) if any(_norm(c) in _ITEM for c in r) and any(_norm(c) in _TITLE for c in r)),
        None,
    )
    if head_at is None:
        raise HTTPException(400, "Could not find 'Item number' and 'Title' columns. Use the Seller Hub report as downloaded.")
    head = [_norm(c) for c in rows[head_at]]

    def col(names) -> Optional[int]:
        return next((i for i, h in enumerate(head) if h in names), None)

    ci, ct, cl, cq, cc = col(_ITEM), col(_TITLE), col(_LABEL), col(_QTY), col(_CAT)
    cp = next((head.index(p) for p in _PRICE if p in head), None)

    parsed: dict[str, dict] = {}
    bad = 0
    for r in rows[head_at + 1 :]:
        item = _clean_item(r[ci]) if len(r) > ci else ""
        if not item:
            if any(c.strip() for c in r):
                bad += 1
            continue

        def cell(i):
            return (r[i] or "").strip() if i is not None and len(r) > i else ""

        qty = cell(cq)
        parsed[item] = {
            "i": item, "s": store, "ls": list_status, "t": cell(ct)[:300] or None, "l": cell(cl) or None,
            "p": _money(cell(cp)), "q": int(qty) if qty.isdigit() else None, "c": cell(cc) or None,
        }
    if not parsed:
        # A report with only the header row (e.g. no scheduled listings right now) is valid: nothing to save.
        return {
            "store": store, "listing_status": list_status, "listings_in_report": 0, "unreadable_rows": bad,
            "with_label": 0, "without_price": 0, "store_mismatch": 0,
        }

    ids = list(parsed)
    mismatch = 0
    for chunk in _chunks(ids, 3000):
        mismatch += db.execute(
            text("select count(*) from ebay_compat_items where item_id = any(cast(:ids as text[])) and store <> :s"),
            {"ids": chunk, "s": store},
        ).scalar()
    db.execute(
        text(
            "insert into ebay_report_rows (item_id, store, listing_status, title, custom_label_sku, price, quantity, ebay_category) "
            "values (:i, :s, :ls, :t, :l, :p, :q, :c) "
            "on conflict (item_id) do update set store = excluded.store, "
            "listing_status = coalesce(excluded.listing_status, ebay_report_rows.listing_status), "
            "title = excluded.title, "
            "custom_label_sku = excluded.custom_label_sku, price = excluded.price, quantity = excluded.quantity, "
            "ebay_category = excluded.ebay_category, uploaded_at = now()"
        ),
        list(parsed.values()),
    )
    db.commit()
    return {
        "store": store, "listing_status": list_status, "listings_in_report": len(parsed), "unreadable_rows": bad,
        "with_label": sum(1 for v in parsed.values() if v["l"]),
        "without_price": sum(1 for v in parsed.values() if v["p"] is None),
        "store_mismatch": mismatch,  # listings whose compat data came from the OTHER store: wrong file?
    }


# ------------------------------------------------------------------ state
@router.get("/state")
def state(db: Session = Depends(get_db)):
    """Cheap facts the page needs on load: is a report loaded, and how far along is the straight-from-eBay mode."""
    reports = db.execute(text("select store, count(*) n from ebay_report_rows group by 1 order by 1")).all()
    rows = db.execute(text(_CANDIDATES)).all()
    return {
        "reports": [{"store": r.store, "n": r.n} for r in reports],
        "api_todo": sum(1 for r in rows if not r.in_report and r.api_status is None),
        "api_ready": sum(1 for r in rows if not r.in_report and r.api_status == "ok"),
        "api_skipped": sum(1 for r in rows if r.api_status == "skipped"),
    }


# ------------------------------------------------------------------ preview
def _summary(rows) -> dict:
    out = {"total": len(rows), "ready": 0, "no_store": 0, "no_title": 0, "no_photos": 0, "no_compat": 0,
           "no_price": 0, "with_label": 0, "ready_ended": 0, "by_store": {}}
    for r in rows:
        if not r.store:
            out["no_store"] += 1
        if not r.title:
            out["no_title"] += 1
        if _usable(r):
            out["ready"] += 1
            out["ready_ended"] += 1 if r.listing_status == "ended" else 0
            out["by_store"][r.store] = out["by_store"].get(r.store, 0) + 1
        out["no_photos"] += 0 if r.has_photos else 1
        out["no_compat"] += 0 if r.has_compat else 1
        out["no_price"] += 0 if r.price is not None else 1
        out["with_label"] += 1 if r.label else 0
    return out


@router.get("/preview")
def preview(db: Session = Depends(get_db)):
    rows = db.execute(text(_CANDIDATES)).all()
    s = _summary(rows)
    sample = [
        {"item": r.item, "store": r.store, "title": r.title, "label": r.label,
         "price": float(r.price) if r.price is not None else None, "photos": r.has_photos, "compat": r.has_compat}
        for r in rows if _usable(r)
    ][:15]
    skipped = [{"item": r.item, "store": r.store, "title": r.title, "reason": r.api_reason} for r in rows if not _usable(r)][:15]
    reports = db.execute(text("select store, count(*) n from ebay_report_rows group by 1 order by 1")).all()
    return {**s, "sample": sample, "skipped_sample": skipped, "reports": [{"store": r.store, "n": r.n} for r in reports]}


# ------------------------------------------------------------------ run
class _Linker:
    """Writes channel_listings rows the way the Link tab does, but works out the setup once per run."""

    def __init__(self, db: Session):
        ch = db.execute(
            text("select channel_id from channel_listings group by channel_id order by count(*) desc limit 1")
        ).first()
        if not ch:
            raise HTTPException(409, "No existing eBay links to copy the channel from, so nothing was imported.")
        self.channel = ch[0]
        cols = db.execute(
            text(
                "select column_name, data_type, is_nullable, column_default, is_identity, is_generated "
                "from information_schema.columns where table_schema = 'public' and table_name = 'channel_listings'"
            )
        ).all()
        by = {c.column_name: c for c in cols}
        if "external_id" not in by:
            raise HTTPException(500, "channel_listings has no external_id column.")
        must = [c.column_name for c in cols if c.is_nullable == "NO" and c.column_default is None
                and c.is_identity != "YES" and c.is_generated == "NEVER"
                and c.column_name not in {"product_id", "channel_id", "external_id", "store"}]
        if must:
            raise HTTPException(409, "channel_listings has required columns this import does not fill in: " + ", ".join(must))
        self.numeric = by["external_id"].data_type in {"bigint", "integer", "smallint", "numeric"}
        self.prefix = "" if self.numeric else _stored_prefix(db)

    def add(self, db: Session, item: str, product_id: int, store: str, status: str = "draft") -> int:
        value_sql = "cast(:e as bigint)" if self.numeric else "cast(:e as text)"
        return db.execute(
            text(
                f"insert into channel_listings (product_id, channel_id, external_id, store, status) "
                f"values (:p, :c, {value_sql}, :st, :ss) returning id"
            ),
            {"p": product_id, "c": self.channel, "e": item if self.numeric else self.prefix + item, "st": store,
             "ss": status},
        ).scalar()


class RunIn(BaseModel):
    limit: int = DEFAULT_RUN


def _import_rows(db: Session, linker: _Linker, rows) -> dict:
    """Create the product, link, photos and compat copy for each candidate row. Each listing saves on its own."""
    made = photos = compat = label_dropped = 0
    problems: list[str] = []
    bridge_error: Optional[str] = None
    for n, r in enumerate(rows, 1):
        label = r.label
        if label and re.fullmatch(r"(?i)(ebay-)?\d+", label) and re.sub(r"[^0-9]", "", label) == r.item:
            label, label_dropped = None, label_dropped + 1  # a label must never equal the item number
        title = " ".join(r.title.split())[:255]
        try:
            with db.begin_nested():
                slug = _unique_slug(db, f"{slugify(title)[:100]}-{r.item}".strip("-"))
                pid = db.execute(
                    text(
                        "insert into products (title, slug, ebay_item_id, custom_label_sku, list_price, "
                        "quantity_on_hand, is_published, is_active) "
                        "values (:t, :s, :e, :l, :p, 0, false, true) returning id"
                    ),
                    {"t": title, "s": slug, "e": clean_ebay_item_id(r.item), "l": label, "p": r.price},
                ).scalar()
                lid = linker.add(db, r.item, pid, r.store, _link_status(r))
                photos += db.execute(
                    text("update product_photos set product_id = :p "
                         "where product_id is null and split_part(storage_path, '/', 1) = :f"),
                    {"p": pid, "f": f"EBAY-{r.item}"},
                ).rowcount
            made += 1
        except IntegrityError:
            problems.append(f"{r.item}: already exists or was just linked (skipped)")
            continue
        except Exception as e:  # one bad listing must not stop the rest
            log.exception("ebay import: item %s failed", r.item)
            problems.append(f"{r.item}: {str(e)[:200]}")
            continue

        if r.has_compat:  # kept separate: a problem here must never undo the product
            try:
                with db.begin_nested():
                    compat += db.execute(
                        text(
                            "insert into ebay_compat_raw (channel_listing_id, ebay_item_id, status, compat_count, "
                            "compat_list, error_code, error_message, fetched_at) "
                            "select :l, c.item_id, c.status, c.compat_count, c.compat_list, c.error_code, "
                            "c.error_message, c.fetched_at from ebay_compat_items c where c.item_id = :i "
                            "on conflict (channel_listing_id) do nothing"
                        ),
                        {"l": lid, "i": r.item},
                    ).rowcount
            except Exception as e:
                bridge_error = bridge_error or str(e)[:300]
        if n % COMMIT_EVERY == 0:
            db.commit()
    db.commit()
    return {"created": made, "photos_attached": photos, "compat_copied": compat, "label_dropped": label_dropped,
            "problems": problems, "compat_copy_error": bridge_error}


@router.post("/run")
def run(body: RunIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    limit = max(1, min(int(body.limit), MAX_RUN))
    rows = db.execute(text(_CANDIDATES)).all()
    ready = [r for r in rows if _usable(r)]
    linker = _Linker(db)
    db.rollback()  # setup only reads
    out = _import_rows(db, linker, ready[:limit])
    left = len(ready) - out["created"] - len(out["problems"])
    return {
        **out, "problems": out["problems"][:40], "problem_count": len(out["problems"]),
        "remaining": max(0, left), "not_ready": len(rows) - len(ready),
    }


# ------------------------------------------------------------------ straight from eBay (no report loaded)
FETCH_DEFAULT = 40  # one eBay call per listing, half a second apart, so keep each request short
FETCH_MAX = 100


def _authorized_stores(db: Session) -> list:
    return db.execute(
        text(
            "select store, seller_username, refresh_token_enc, scopes, refresh_token_expires_at "
            "from ebay_accounts where refresh_token_enc is not null order by store"
        )
    ).all()


def _num(v: Optional[str]) -> Optional[Decimal]:
    return _money(v or "")


def _fetch_details(db: Session, stores: list, item_id: str) -> dict:
    """Ask eBay about one listing, trying the given stores until one owns it.

    Returns {"state": "ok", ...details} | {"state": "skipped", "reason"} | {"state": "error", "reason"}.
    "error" is never saved, so that listing is asked about again next time. Raises RateLimited at eBay's limit.
    """
    outcomes: list[str] = []
    last_error = ""
    for row in stores:
        expected = _expected_username(row)
        try:
            root = _trading(db, row.store, "GetItem",
                            f"<ItemID>{item_id}</ItemID><DetailLevel>ReturnAll</DetailLevel>")
        except HTTPException as e:
            outcomes.append("error")
            last_error = str(e.detail)[:200]
            continue
        finally:
            time.sleep(DELAY)

        ack = root.findtext(f"{NS}Ack") or ""
        if ack not in ("Success", "Warning"):
            errs = [e for e in _errors(root) if e["severity"] == "Error"] or _errors(root)
            codes = {e["code"] for e in errs}
            if RATE_LIMIT_CODE in codes:
                raise RateLimited("eBay says the daily call limit has been reached (error 518).")
            if codes & NOT_FOUND_CODES:
                outcomes.append("not_found")
            else:
                outcomes.append("error")
                last_error = (errs[0]["message"] if errs else "eBay reported a failure without details.")[:200]
            continue

        item = root.find(f"{NS}Item")
        if item is None:
            outcomes.append("error")
            last_error = "eBay's reply had no listing in it."
            continue
        seller = (item.findtext(f"{NS}Seller/{NS}UserID") or "").strip()
        if seller and seller.lower() != expected.lower():
            outcomes.append("wrong_seller")
            continue

        status = (item.findtext(f"{NS}SellingStatus/{NS}ListingStatus") or "").strip()
        if status and status != "Active":
            return {"state": "skipped", "reason": f"Listing is not active on eBay ({status})."}
        qty_txt = (item.findtext(f"{NS}Quantity") or "").strip()
        sold_txt = (item.findtext(f"{NS}SellingStatus/{NS}QuantitySold") or "0").strip()
        qty = max(0, int(qty_txt) - (int(sold_txt) if sold_txt.isdigit() else 0)) if qty_txt.isdigit() else None
        return {
            "state": "ok",
            "store": row.store,
            "title": (item.findtext(f"{NS}Title") or "").strip()[:300] or None,
            "label": (item.findtext(f"{NS}SKU") or "").strip() or None,
            "price": _num(item.findtext(f"{NS}SellingStatus/{NS}CurrentPrice")) or _num(item.findtext(f"{NS}StartPrice")),
            "qty": qty,
            "category": (item.findtext(f"{NS}PrimaryCategory/{NS}CategoryName") or "").strip()[:200] or None,
        }

    if "ok" not in outcomes and "error" not in outcomes:
        if all(o == "not_found" for o in outcomes):
            return {"state": "skipped", "reason": "eBay cannot find this listing (ended or deleted)."}
        return {"state": "skipped", "reason": "Not on any connected store."}
    return {"state": "error", "reason": last_error or "eBay could not be read."}


@router.post("/run-api")
def run_api(body: RunIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Report-free mode. Asks eBay about listings that have no report row (one call each), remembers the answer,
    then creates products for the listings it has answers for."""
    limit = max(1, min(int(body.limit), FETCH_MAX))
    stores = _authorized_stores(db)
    if not stores:
        raise HTTPException(409, "No eBay store has been approved yet, so eBay cannot be asked. Connect a store on the Live listings tab, or upload a report.")
    linker = _Linker(db)
    rows = db.execute(text(_CANDIDATES)).all()
    todo = [r for r in rows if not r.in_report and r.api_status is None][:limit]
    db.rollback()  # reads only so far

    by_name = {s.store.lower(): s for s in stores}
    fetched = skipped = errors = 0
    error_sample: list[str] = []
    rate_limited: Optional[str] = None
    for n, r in enumerate(todo, 1):
        # A listing the compat import already placed in a store is asked about only that store.
        order = [by_name[r.compat_store.lower()]] if r.compat_store and r.compat_store.lower() in by_name else stores
        try:
            d = _fetch_details(db, order, r.item)
        except RateLimited as e:
            rate_limited = str(e)
            break
        if d["state"] == "error":
            errors += 1
            if len(error_sample) < 10:
                error_sample.append(f"{r.item}: {d['reason']}")
            if errors >= 5 and fetched + skipped == 0:
                break  # nothing is working (approval expired, eBay down): stop instead of burning calls
            continue
        if d["state"] == "skipped":
            skipped += 1
            db.execute(text("insert into ebay_api_details (item_id, status, reason) values (:i, 'skipped', :r) "
                            "on conflict (item_id) do update set status = 'skipped', reason = excluded.reason, fetched_at = now()"),
                       {"i": r.item, "r": d["reason"]})
        else:
            fetched += 1
            db.execute(
                text("insert into ebay_api_details (item_id, status, store, title, custom_label_sku, price, quantity, ebay_category) "
                     "values (:i, 'ok', :s, :t, :l, :p, :q, :c) "
                     "on conflict (item_id) do update set status = 'ok', reason = null, store = excluded.store, "
                     "title = excluded.title, custom_label_sku = excluded.custom_label_sku, price = excluded.price, "
                     "quantity = excluded.quantity, ebay_category = excluded.ebay_category, fetched_at = now()"),
                {"i": r.item, "s": d["store"], "t": d["title"], "l": d["label"], "p": d["price"], "q": d["qty"], "c": d["category"]},
            )
        if n % 10 == 0:
            db.commit()
    db.commit()

    rows = db.execute(text(_CANDIDATES)).all()
    db.rollback()
    ready = [r for r in rows if not r.in_report and r.api_status == "ok" and _usable(r)]
    out = _import_rows(db, linker, ready[:MAX_RUN])
    still_fetch = sum(1 for r in rows if not r.in_report and r.api_status is None)
    return {
        **out, "problems": out["problems"][:40], "problem_count": len(out["problems"]),
        "asked_ebay": fetched + skipped + errors, "skipped": skipped, "errors": errors, "error_sample": error_sample,
        "rate_limited": rate_limited, "remaining_to_ask": still_fetch,
        "remaining_to_create": max(0, len(ready) - out["created"] - len(out["problems"])),
    }
