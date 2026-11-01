"""
eBay product import: the back end of the admin "eBay product import" tab.

Mounted at /admin/ebay-import/* (owner/admin only).

  POST /report   {store, csv}   saves a Seller Hub "all active listings" report (title, price, label) for a store
  GET  /preview                 read-only: what an import would do right now
  POST /run      {limit}        creates products for eBay listings that are not linked to a product yet

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

A listing is skipped (and reported) when its store or title cannot be worked out. The store comes from an
uploaded report, else from the compat import; the title likewise. Each listing is saved on its own, so one
bad listing never blocks the rest, and re-running only picks up what is left.
"""
import csv
import io
import logging
import re
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
from app.admin_ebay_photos import _chunks, _clean_item, _norm
from app.auth import AuthUser, require_admin
from app.database import get_db
from app.skus import clean_ebay_item_id

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-import", tags=["admin-ebay-import"], dependencies=[Depends(require_admin)])

MAX_RUN = 500
DEFAULT_RUN = 200
COMMIT_EVERY = 50

# Unlinked listings with whatever we know about each (one row per item number).
_CANDIDATES = """
with ids as (
  select cast(item_number as text) as item from ebay_label_import
  union
  select item_id from ebay_report_rows
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
       coalesce(nullif(trim(r.custom_label_sku), ''), nullif(trim(cast(l.custom_label_sku as text)), '')) as label,
       coalesce(r.store, c.store) as store,
       coalesce(nullif(trim(r.title), ''), nullif(trim(c.title), '')) as title,
       r.price,
       exists (select 1 from product_photos pp
               where pp.product_id is null and split_part(pp.storage_path, '/', 1) = 'EBAY-' || o.item) as has_photos,
       (c.item_id is not null and c.status = 'ok') as has_compat
from open_ids o
left join ebay_report_rows r on r.item_id = o.item
left join ebay_compat_items c on c.item_id = o.item
left join ebay_label_import l on cast(l.item_number as text) = o.item
"""


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
            "i": item, "s": store, "t": cell(ct)[:300] or None, "l": cell(cl) or None,
            "p": _money(cell(cp)), "q": int(qty) if qty.isdigit() else None, "c": cell(cc) or None,
        }
    if not parsed:
        raise HTTPException(400, "No listings were found in that file.")

    ids = list(parsed)
    mismatch = 0
    for chunk in _chunks(ids, 3000):
        mismatch += db.execute(
            text("select count(*) from ebay_compat_items where item_id = any(cast(:ids as text[])) and store <> :s"),
            {"ids": chunk, "s": store},
        ).scalar()
    db.execute(
        text(
            "insert into ebay_report_rows (item_id, store, title, custom_label_sku, price, quantity, ebay_category) "
            "values (:i, :s, :t, :l, :p, :q, :c) "
            "on conflict (item_id) do update set store = excluded.store, title = excluded.title, "
            "custom_label_sku = excluded.custom_label_sku, price = excluded.price, quantity = excluded.quantity, "
            "ebay_category = excluded.ebay_category, uploaded_at = now()"
        ),
        list(parsed.values()),
    )
    db.commit()
    return {
        "store": store, "listings_in_report": len(parsed), "unreadable_rows": bad,
        "with_label": sum(1 for v in parsed.values() if v["l"]),
        "without_price": sum(1 for v in parsed.values() if v["p"] is None),
        "store_mismatch": mismatch,  # listings whose compat data came from the OTHER store: wrong file?
    }


# ------------------------------------------------------------------ preview
def _summary(rows) -> dict:
    out = {"total": len(rows), "ready": 0, "no_store": 0, "no_title": 0, "no_photos": 0, "no_compat": 0,
           "no_price": 0, "with_label": 0, "by_store": {}}
    for r in rows:
        if not r.store:
            out["no_store"] += 1
        if not r.title:
            out["no_title"] += 1
        if r.store and r.title:
            out["ready"] += 1
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
        for r in rows if r.store and r.title
    ][:15]
    skipped = [{"item": r.item, "store": r.store, "title": r.title} for r in rows if not (r.store and r.title)][:15]
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

    def add(self, db: Session, item: str, product_id: int, store: str) -> int:
        value_sql = "cast(:e as bigint)" if self.numeric else "cast(:e as text)"
        return db.execute(
            text(
                f"insert into channel_listings (product_id, channel_id, external_id, store) "
                f"values (:p, :c, {value_sql}, :st) returning id"
            ),
            {"p": product_id, "c": self.channel, "e": item if self.numeric else self.prefix + item, "st": store},
        ).scalar()


class RunIn(BaseModel):
    limit: int = DEFAULT_RUN


@router.post("/run")
def run(body: RunIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    limit = max(1, min(int(body.limit), MAX_RUN))
    rows = db.execute(text(_CANDIDATES)).all()
    ready = [r for r in rows if r.store and r.title]
    linker = _Linker(db)
    db.rollback()  # setup only reads

    made = photos = compat = 0
    label_dropped = 0
    problems: list[str] = []
    bridge_error: Optional[str] = None
    for n, r in enumerate(ready[:limit], 1):
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
                lid = linker.add(db, r.item, pid, r.store)
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

    left = len(ready) - made - len(problems)
    return {
        "created": made, "photos_attached": photos, "compat_copied": compat, "label_dropped": label_dropped,
        "problems": problems[:40], "problem_count": len(problems), "compat_copy_error": bridge_error,
        "remaining": max(0, left), "not_ready": len(rows) - len(ready),
    }
