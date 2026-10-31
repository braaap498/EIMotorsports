"""
eBay photo import: the back end of the admin "eBay photo import" tab.

Mounted at /admin/ebay-photos/* (owner/admin only).

  POST /import   {store, mode}   starts a background job (one at a time)
  GET  /status                   the latest job, for the progress display (poll it)
  POST /stop                     asks the running job to stop after its current listing
  POST /labels   {csv}           fills in custom labels from a Seller Hub report

What a job does (the same steps you ran by hand for the 102 genuine-oem-parts listings):

  1. Ask eBay for the store's ACTIVE listings (public Browse API, same access the "Link new eBay
     listings" tab uses). Ended/inactive listings are not visible this way.
  2. Listings that are not linked to a product yet are added to ebay_label_import with an empty
     custom label (skipped if already there).
  3. Photos are looked up on eBay, downloaded, stored with photo_store, and saved as UNLINKED
     product_photos rows (product_id empty, storage_path EBAY-<item>/EBAY-<item>_<n>.jpg), exactly
     like apply_label_photos.py. Listings that share a custom label share one download. A listing
     whose photos fail gets no rows, so nobody ever gets a partial set.
  mode "new_only"     does steps 1-3 only.
  mode "all_missing"  also checks listings that ARE linked to a product and adds any photo eBay has
                      that the product does not (matched by eBay's image id against the product's
                      saved source link, the same way check_ebay_photos_new_listings_only.py does).

Progress lives in the database (ebay_photo_jobs), because the server's disk is not kept. A job
that stops at eBay's lookup limit is "paused"; start it again later and it carries on, because
finished listings are skipped. If the server restarts mid-job the job shows as "failed
(interrupted)" and can simply be started again.

Settings: EBAY_CLIENT_ID / EBAY_CLIENT_SECRET (same as the other eBay screens), optional
EBAY_CATEGORY_IDS (default 6000) and EBAY_PHOTO_MAX_CALLS (default 4500 eBay calls per run).
"""
import csv
import io
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import photo_store
from app.auth import AuthUser, require_admin
from app.database import get_db, get_engine
from app.ebay_feedback import SEARCH_URL, FeedbackError, _token, configured, seller_username

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-photos", tags=["admin-ebay-photos"], dependencies=[Depends(require_admin)])

API = "https://api.ebay.com"
PAGE = 200
MAX_PAGES = 50  # eBay returns at most 10,000 results (50 x 200)
STALE_MINUTES = 10  # a "running" job with no sign of life for this long was cut off by a restart
SELLER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
MODES = {"new_only", "all_missing"}


def _max_calls() -> int:
    try:
        return max(50, int(os.environ.get("EBAY_PHOTO_MAX_CALLS") or 4500))
    except ValueError:
        return 4500


def _category_ids() -> str:
    return (os.environ.get("EBAY_CATEGORY_IDS") or "6000").strip()


# ---------------------------------------------------------------- eBay access
class RateLimited(Exception):
    """eBay says slow down, or this run used its allowance of lookups."""


_tok = {"v": None, "t": 0.0}


def _ebay_token(force: bool = False) -> str:
    if force or not _tok["v"] or time.time() - _tok["t"] > 5400:
        _tok["v"] = _token()
        _tok["t"] = time.time()
    return _tok["v"]


class _Calls:
    """Counts eBay requests in one run so it can stop before eBay's daily limit."""

    def __init__(self, limit: int):
        self.n, self.limit = 0, limit

    def tick(self):
        if self.n >= self.limit:
            raise RateLimited(f"This run reached its limit of {self.limit} eBay lookups.")
        self.n += 1


def _get(url: str, calls: _Calls, missing_ok: bool):
    """JSON from eBay. Returns None for 'cannot be read' (404, or 400 when missing_ok)."""
    last: Optional[Exception] = None
    limited = False
    for attempt in range(5):
        calls.tick()
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {_ebay_token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404 or (e.code == 400 and missing_ok):
                return None
            if e.code == 401:
                _ebay_token(force=True)
            limited = e.code == 429
            retry = e.headers.get("Retry-After") if e.headers else None
            delay = min(int(retry), 60) if (limited and retry and retry.isdigit()) else min(2**attempt, 30)
            last = e
        except Exception as e:  # network trouble
            limited, delay, last = False, 1.5 * (attempt + 1), e
        time.sleep(delay)
    if limited:
        raise RateLimited("eBay is limiting requests right now.")
    raise FeedbackError(f"eBay could not be read ({last}).")


def _item_number(summary: dict) -> Optional[str]:
    legacy = str(summary.get("legacyItemId") or "").strip()
    if legacy.isdigit():
        return legacy
    parts = str(summary.get("itemId") or "").split("|")
    if len(parts) >= 2 and parts[1].isdigit():
        return parts[1]
    return None


def _seller_listings(seller: str, calls: _Calls) -> tuple[list[str], Optional[int]]:
    """Item numbers of the seller's active listings (newest first), and eBay's own total."""
    ids: list[str] = []
    seen: set[str] = set()
    total: Optional[int] = None
    for page in range(MAX_PAGES):
        q = urllib.parse.urlencode(
            {
                "filter": f"sellers:{{{seller}}}",
                "category_ids": _category_ids(),
                "sort": "newlyListed",
                "limit": PAGE,
                "offset": page * PAGE,
            }
        )
        data = _get(f"{SEARCH_URL}?{q}", calls, missing_ok=False) or {}
        if total is None and isinstance(data.get("total"), int):
            total = data["total"]
        summaries = data.get("itemSummaries") or []
        for s in summaries:
            n = _item_number(s)
            if n and n not in seen:
                seen.add(n)
                ids.append(n)
        if len(summaries) < PAGE:
            break
    return ids, total


def photo_key(url: str) -> str:
    """eBay's image id, so the same photo at another size compares equal."""
    m = re.search(r"/images/g/([^/]+)/", url)
    if m:
        return "g:" + m.group(1)
    m = re.search(r"/z/([^/]+)/", url)
    if m:
        return "z:" + m.group(1)
    u = re.sub(r"^https?://", "", url.split("?")[0].strip().lower())
    return "u:" + re.sub(r"/s-l\d+\.", "/s-l.", u)


def _ebay_photos(item: str, calls: _Calls) -> Optional[list[str]]:
    """Photo URLs of one listing, or None if eBay will not show it (ended, removed, variations)."""
    d = _get(f"{API}/buy/browse/v1/item/get_item_by_legacy_id?legacy_item_id={item}", calls, missing_ok=True)
    if d is None:
        return None
    urls = [(d.get("image") or {}).get("imageUrl")]
    urls += [(a or {}).get("imageUrl") for a in d.get("additionalImages") or []]
    out, seen = [], set()
    for u in urls:
        if u and photo_key(u) not in seen:
            seen.add(photo_key(u))
            out.append(u)
    return out or None


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _path_for(item: str, n: int) -> str:
    return f"EBAY-{item}/EBAY-{item}_{n}.jpg"


# ----------------------------------------------------------- job bookkeeping
class _Job:
    """The counters of one run, saved to ebay_photo_jobs as it goes."""

    FIELDS = ("listings_found", "listings_added", "items_total", "items_done", "photos_added", "no_photos", "failed")

    def __init__(self, job_id: int):
        self.id = job_id
        self.c = {k: 0 for k in self.FIELDS}
        self.failures: list[str] = []
        self.message = ""

    def fail(self, label: str, why: str):
        self.c["failed"] += 1
        if len(self.failures) < 40:
            self.failures.append(f"{label}: {why}"[:300])

    def save(self, calls: Optional[_Calls] = None, status: Optional[str] = None):
        sets = {k: self.c[k] for k in self.FIELDS}
        sets["message"] = self.message
        if calls is not None:
            sets["ebay_calls"] = calls.n
        sql = ", ".join(f"{k} = :{k}" for k in sets) + ", failures = cast(:failures as jsonb), updated_at = now()"
        params = {**sets, "failures": json.dumps(self.failures), "id": self.id}
        if status:
            sql += ", status = :status, finished_at = now()"
            params["status"] = status
        with get_engine().begin() as c:
            # never overwrite a stop request with a plain progress save
            where = "id = :id" if status else "id = :id and status in ('running','stopping')"
            c.execute(text(f"update ebay_photo_jobs set {sql} where {where}"), params)

    def stop_requested(self) -> bool:
        with get_engine().connect() as c:
            return c.execute(text("select status from ebay_photo_jobs where id = :i"), {"i": self.id}).scalar() == "stopping"


def _expire_stale(db_exec):
    db_exec(
        text(
            "update ebay_photo_jobs set status = 'failed', finished_at = now(), "
            "message = 'Interrupted (the server restarted or stopped). Start it again; finished listings are skipped.' "
            f"where status in ('running','stopping') and updated_at < now() - interval '{STALE_MINUTES} minutes'"
        )
    )


# ------------------------------------------------------------------ the work
def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _run(job_id: int, seller: str, include_products: bool):
    job = _Job(job_id)
    calls = _Calls(_max_calls())
    try:
        _work(job, calls, seller, include_products)
    except RateLimited as e:
        job.message = f"Paused: {e} Start the import again later and it carries on where it stopped."
        job.save(calls, "paused")
    except (FeedbackError, photo_store.StorageError) as e:
        job.message = str(e)
        job.save(calls, "failed")
    except Exception:
        log.exception("eBay photo import crashed")
        job.message = "Something unexpected went wrong. The details are in the API log."
        job.save(calls, "failed")


def _work(job: _Job, calls: _Calls, seller: str, include_products: bool):
    eng = get_engine()
    ids, total = _seller_listings(seller, calls)
    job.c["listings_found"] = len(ids)
    if not ids:
        job.message = f"eBay returned no active listings for {seller} in category {_category_ids()}."
        job.save(calls, "done")
        return
    job.message = f"eBay returned {len(ids)} active listings. Comparing with what you already have…"
    job.save(calls)

    # which of them are already linked to a product
    linked: dict[str, int] = {}
    with eng.connect() as c:
        for chunk in _chunks(ids, 2000):
            rows = c.execute(
                text(
                    "select digits, product_id from ("
                    " select regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') as digits, product_id"
                    "   from channel_listings where external_id is not null"
                    " union all"
                    " select regexp_replace(cast(ebay_item_id as text), '[^0-9]', '', 'g'), id"
                    "   from products where ebay_item_id is not null"
                    ") t where digits = any(cast(:ids as text[]))"
                ),
                {"ids": chunk},
            ).all()
            for d, pid in rows:
                linked[d] = pid
    unlinked = [i for i in ids if i not in linked]

    # add the unlinked ones to the import table (empty label), skipping any already there
    added = 0
    with eng.begin() as c:
        for chunk in _chunks(unlinked, 1000):
            res = c.execute(
                text(
                    "insert into ebay_label_import (item_number, custom_label_sku) "
                    "select x, null from unnest(cast(:ids as text[])) as x "
                    "where not exists (select 1 from ebay_label_import i where cast(i.item_number as text) = x)"
                ),
                {"ids": chunk},
            )
            added += res.rowcount or 0
    job.c["listings_added"] = added

    # unlinked listings that still need photos
    with eng.connect() as c:
        have = set()
        for chunk in _chunks(unlinked, 2000):
            for (p,) in c.execute(
                text(
                    "select distinct split_part(storage_path, '/', 1) from product_photos "
                    "where split_part(storage_path, '/', 1) = any(cast(:paths as text[]))"
                ),
                {"paths": [f"EBAY-{i}" for i in chunk]},
            ):
                have.add(p.replace("EBAY-", "", 1))
        todo = [i for i in unlinked if i not in have]
        labels: dict[str, str] = {}
        for chunk in _chunks(todo, 2000):
            for item, lab in c.execute(
                text(
                    "select cast(item_number as text), nullif(trim(cast(custom_label_sku as text)), '') "
                    "from ebay_label_import where cast(item_number as text) = any(cast(:ids as text[]))"
                ),
                {"ids": chunk},
            ):
                if lab:
                    labels[item] = lab

    groups: list[list[str]] = []
    by_label: dict[str, list[str]] = defaultdict(list)
    for item in todo:
        if item in labels:
            by_label[labels[item]].append(item)
        else:
            groups.append([item])
    groups = list(by_label.values()) + groups

    product_items: list[str] = []
    if include_products:
        mine = [i for i in ids if i in linked]
        checked: dict[str, object] = {}
        with eng.connect() as c:
            for chunk in _chunks(mine, 2000):
                for item, when in c.execute(
                    text("select item_number, checked_at from ebay_photo_checked where item_number = any(cast(:ids as text[]))"),
                    {"ids": chunk},
                ):
                    checked[item] = when
        never = [i for i in mine if i not in checked]
        old = sorted((i for i in mine if i in checked), key=lambda i: checked[i])
        product_items = never + old

    job.c["items_total"] = len(groups) + len(product_items)
    job.message = (
        f"{len(unlinked)} listings are not products yet ({added} newly added to the import table); "
        f"{len(groups)} need photos"
        + (f"; checking {len(product_items)} linked listings for missing photos" if include_products else "")
        + "."
    )
    job.save(calls)

    stopped = False
    for group in groups:
        if job.stop_requested():
            stopped = True
            break
        _photos_for_group(job, calls, group)
        job.c["items_done"] += 1
        job.save(calls)
    if not stopped:
        for item in product_items:
            if job.stop_requested():
                stopped = True
                break
            _missing_for_product(job, calls, item, linked[item])
            job.c["items_done"] += 1
            job.save(calls)

    c = job.c
    job.message = (
        ("Stopped. " if stopped else "Finished. ")
        + f"{c['listings_found']} active listings read, {c['listings_added']} added to the import table, "
        f"{c['photos_added']} photo rows added, {c['no_photos']} listings eBay would not give photos for, "
        f"{c['failed']} failed."
    )
    job.save(calls, "stopped" if stopped else "done")


def _store_photos(urls: list[str]) -> list[str]:
    """Download, process and upload each URL; returns the storage keys in order."""
    keys: list[str] = []
    for url in urls:
        p = photo_store.process_image(_download(url))
        photo_store.upload(p)
        if p.key not in keys:
            keys.append(p.key)
    return keys


def _fatal_storage(e: Exception) -> bool:
    return isinstance(e, photo_store.StorageError) and "not set up" in str(e)


def _photos_for_group(job: _Job, calls: _Calls, group: list[str]):
    """One download per group (listings sharing a custom label), shared by every listing in it."""
    urls = None
    for item in group:  # try listings one at a time until eBay will show one
        urls = _ebay_photos(item, calls)
        if urls:
            break
    if not urls:
        job.c["no_photos"] += len(group)
        return
    try:
        keys = _store_photos(urls)
    except Exception as e:
        if _fatal_storage(e):
            raise
        job.fail(group[0], str(e))
        return
    if not keys:
        job.fail(group[0], "no photo could be stored")
        return
    with get_engine().begin() as c:
        for item in group:
            for n, key in enumerate(keys, 1):
                c.execute(
                    text(
                        "insert into product_photos (product_id, image_key, storage_path, is_primary, sort_order) "
                        "values (null, :k, :sp, :pr, :o)"
                    ),
                    {"k": key, "sp": _path_for(item, n), "pr": n == 1, "o": n - 1},
                )
                job.c["photos_added"] += 1


def _missing_for_product(job: _Job, calls: _Calls, item: str, product_id: int):
    urls = _ebay_photos(item, calls)
    with get_engine().begin() as c:
        c.execute(
            text(
                "insert into ebay_photo_checked (item_number, checked_at) values (:i, now()) "
                "on conflict (item_number) do update set checked_at = now()"
            ),
            {"i": item},
        )
    if not urls:
        job.c["no_photos"] += 1
        return
    with get_engine().connect() as c:
        rows = c.execute(
            text("select source_url, image_key from product_photos where product_id = :p"), {"p": product_id}
        ).all()
    have_ids = {photo_key(r[0]) for r in rows if r[0]}
    have_keys = {r[1] for r in rows if r[1]}
    new_urls = [u for u in urls if photo_key(u) not in have_ids]
    for url in new_urls:
        try:
            p = photo_store.process_image(_download(url))
            if p.key in have_keys:  # same picture already on the product
                continue
            photo_store.upload(p)
        except Exception as e:
            if _fatal_storage(e):
                raise
            job.fail(f"{item} (product {product_id})", str(e))
            continue
        with get_engine().begin() as c:
            nxt = c.execute(
                text("select coalesce(max(sort_order), -1) + 1 from product_photos where product_id = :p"),
                {"p": product_id},
            ).scalar()
            c.execute(
                text(
                    "insert into product_photos (product_id, image_key, source_url, storage_path, is_primary, sort_order) "
                    "values (:p, :k, :u, :sp, :pr, :o)"
                ),
                {"p": product_id, "k": p.key, "u": url, "sp": photo_store.full_path(p.key), "pr": not rows and not have_keys, "o": nxt},
            )
        have_keys.add(p.key)
        job.c["photos_added"] += 1


# --------------------------------------------------------------------- routes
class ImportIn(BaseModel):
    store: str = ""
    mode: str


def _job_json(row) -> dict:
    d = dict(row._mapping)
    d["running"] = d["status"] in ("running", "stopping")
    return d


@router.post("/import")
def start_import(body: ImportIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    if not configured():
        raise HTTPException(503, "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API.")
    if body.mode not in MODES:
        raise HTTPException(400, "Unknown import type.")
    store = body.store.strip() or seller_username()
    if not SELLER_RE.fullmatch(store):
        raise HTTPException(400, "That does not look like an eBay store name.")
    _expire_stale(db.execute)
    running = db.execute(text("select id from ebay_photo_jobs where status in ('running','stopping') limit 1")).first()
    if running:
        db.commit()
        raise HTTPException(409, "An import is already running. Wait for it to finish or stop it first.")
    job_id = db.execute(
        text(
            "insert into ebay_photo_jobs (store, mode, started_by, message) "
            "values (:s, :m, :u, 'Starting…') returning id"
        ),
        {"s": store, "m": body.mode, "u": user.email or str(user.user_id)},
    ).scalar()
    db.commit()
    threading.Thread(
        target=_run, args=(job_id, store, body.mode == "all_missing"), daemon=True, name=f"ebay-photos-{job_id}"
    ).start()
    log.info("eBay photo import %s started for %s (%s) by %s", job_id, store, body.mode, user.email)
    row = db.execute(text("select * from ebay_photo_jobs where id = :i"), {"i": job_id}).first()
    return _job_json(row)


@router.get("/status")
def status(db: Session = Depends(get_db)):
    _expire_stale(db.execute)
    db.commit()
    row = db.execute(text("select * from ebay_photo_jobs order by id desc limit 1")).first()
    return {"job": _job_json(row) if row else None}


@router.post("/stop")
def stop(db: Session = Depends(get_db)):
    db.execute(text("update ebay_photo_jobs set status = 'stopping', updated_at = now() where status = 'running'"))
    db.commit()
    return status(db)


# ------------------------------------------------------- custom labels (CSV)
class LabelsIn(BaseModel):
    csv: str


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


ITEM_HEADERS = {"itemnumber", "itemid", "ebayitemid", "ebayitemnumber", "item"}
LABEL_HEADERS = {"customlabelsku", "customlabel", "sku"}


def _clean_item(v: str) -> str:
    v = (v or "").strip().lstrip("=").strip('"').strip()
    if v.endswith(".0"):
        v = v[:-2]
    return v if (v.isdigit() and 9 <= len(v) <= 15) else ""


@router.post("/labels")
def fill_labels(body: LabelsIn, db: Session = Depends(get_db)):
    if len(body.csv) > 20_000_000:
        raise HTTPException(413, "That file is too large.")
    rows = list(csv.reader(io.StringIO(body.csv.lstrip("\ufeff"))))
    head_at = next(
        (
            i
            for i, r in enumerate(rows[:15])
            if any(_norm(c) in ITEM_HEADERS for c in r) and any(_norm(c) in LABEL_HEADERS for c in r)
        ),
        None,
    )
    if head_at is None:
        raise HTTPException(
            400,
            "Could not find an item number column and a custom label column in the first rows. "
            "Use the Seller Hub report as downloaded (it has 'Item number' and 'Custom label (SKU)').",
        )
    head = [_norm(c) for c in rows[head_at]]
    ci = next(i for i, h in enumerate(head) if h in ITEM_HEADERS)
    cl = next(i for i, h in enumerate(head) if h in LABEL_HEADERS)

    report: dict[str, str] = {}
    bad = 0
    for r in rows[head_at + 1 :]:
        if len(r) <= max(ci, cl):
            continue
        item = _clean_item(r[ci])
        if not item:
            if any(c.strip() for c in r):
                bad += 1
            continue
        label = (r[cl] or "").strip()
        if item not in report or (label and not report[item]):
            report[item] = label
    if not report:
        raise HTTPException(400, "No listings were found in that file.")

    ids = list(report)
    existing: dict[str, str] = {}
    for chunk in _chunks(ids, 3000):
        for item, lab in db.execute(
            text(
                "select cast(item_number as text), coalesce(trim(cast(custom_label_sku as text)), '') "
                "from ebay_label_import where cast(item_number as text) = any(cast(:ids as text[]))"
            ),
            {"ids": chunk},
        ):
            existing[item] = lab

    fill = [(i, report[i]) for i in ids if i in existing and not existing[i] and report[i]]
    same = sum(1 for i in ids if i in existing and existing[i] and existing[i] == report[i])
    differs = [{"item": i, "current": existing[i], "report": report[i]} for i in ids
               if i in existing and existing[i] and report[i] and existing[i] != report[i]]
    missing = [i for i in ids if i not in existing]

    linked: set[str] = set()
    for chunk in _chunks(missing, 3000):
        for (d,) in db.execute(
            text(
                "select digits from ("
                " select regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') as digits from channel_listings"
                "  where external_id is not null"
                " union all"
                " select regexp_replace(cast(ebay_item_id as text), '[^0-9]', '', 'g') from products where ebay_item_id is not null"
                ") t where digits = any(cast(:ids as text[]))"
            ),
            {"ids": chunk},
        ):
            linked.add(d)
    to_add = [i for i in missing if i not in linked]

    for i, lab in fill:
        db.execute(
            text(
                "update ebay_label_import set custom_label_sku = :l where cast(item_number as text) = :i "
                "and coalesce(trim(cast(custom_label_sku as text)), '') = ''"
            ),
            {"l": lab, "i": i},
        )
    for i in to_add:
        db.execute(
            text("insert into ebay_label_import (item_number, custom_label_sku) values (:i, :l)"),
            {"i": i, "l": report[i] or None},
        )
    db.commit()
    return {
        "listings_in_report": len(report),
        "with_label": sum(1 for v in report.values() if v),
        "labels_filled": len(fill),
        "listings_added": len(to_add),
        "already_linked_skipped": len(missing) - len(to_add),
        "already_had_same_label": same,
        "differs_count": len(differs),
        "differs": differs[:50],
        "unreadable_rows": bad,
    }
