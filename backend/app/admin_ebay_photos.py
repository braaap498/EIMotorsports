"""
eBay photo import: the back end of the admin "eBay photo import" tab.

Mounted at /admin/ebay-photos/* (owner/admin only).

  POST /import   {store, mode}   starts a background job (one at a time)
  GET  /status                   the latest job, for the progress display (poll it)
  POST /stop                     asks the running job to stop after its current listing
  POST /labels   {csv}           fills in custom labels from a Seller Hub report
  POST /labels-from-ebay {store, limit}  fills in blank custom labels by asking eBay directly (no report needed)

What a job does (the same steps you ran by hand for the 102 genuine-oem-parts listings):

  1. Ask eBay for the store's ACTIVE listings (public Browse API, same access the "Link new eBay
     listings" tab uses). Ended/inactive listings are not visible this way.
  2. Listings that are not linked to a product yet are added to ebay_label_import with an empty
     custom label (skipped if already there).
  3. Photos are looked up on eBay, downloaded, stored with photo_store, and saved as UNLINKED
     product_photos rows (product_id empty, storage_path EBAY-<item>/EBAY-<item>_<n>.jpg), exactly
     like apply_label_photos.py. Listings that share a custom label share one download, and a
     listing whose label already has stored photos (on a product, or on another imported listing)
     copies those photos without asking eBay at all. A listing whose photos fail gets no rows, so
     nobody ever gets a partial set.
  mode "new_only"     does steps 1-3 only.
  mode "all_missing"  also checks listings that ARE linked to a product and adds any photo eBay has
                      that the product does not. A photo counts as already there if (1) eBay's image id
                      matches a saved source link on the product, (2) the stored picture has the same
                      content key, or (3) it LOOKS the same as a photo the product already has (a small
                      fingerprint of the picture, so a photo that came in through the old scripts, was
                      merged in from another listing, or was re-encoded by eBay is not added twice).
                      A product's main listing is checked before the listings merged into it.

  mode "listing_originals"  gives each eBay listing its OWN photo set, saved in ebay_listings.photos (one entry
                      per photo: image_key, source_url, storage_path, is_primary, sort_order). Listings merged into one
                      product all share that product's gallery, so this is where each listing keeps the pictures it
                      had on eBay. Photos a listing already has (same eBay image id, same stored picture, or one that
                      looks the same) are skipped, never added twice. A saved set that is only a copy of the
                      product's current photos (rows from the "backfill") is replaced by what eBay shows for that
                      item. Nothing is ever taken away from sets that came from a merge. Listings are recorded in
                      ebay_listing_photo_checked, so a stopped or paused run carries on where it left off.

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

from PIL import Image

from app import photo_store
from app.auth import AuthUser, require_admin
from app.config import get_settings
from app.database import get_db, get_engine
from app.ebay_feedback import SEARCH_URL, FeedbackError, _token, configured, seller_username
from app.admin_ebay_listings import _resolve
from app.ebay_edit import _read_item

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-photos", tags=["admin-ebay-photos"], dependencies=[Depends(require_admin)])

API = "https://api.ebay.com"
PAGE = 200
MAX_PAGES = 50  # eBay returns at most 10,000 results (50 x 200)
STALE_MINUTES = 10  # a "running" job with no sign of life for this long was cut off by a restart
SELLER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
MODES = {"new_only", "all_missing", "listing_originals"}


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
        self.reused = 0  # listings that copied photos from a matching custom label

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


def _run(job_id: int, seller: str, include_products: bool, store_key: Optional[str] = None):
    job = _Job(job_id)
    calls = _Calls(_max_calls())
    try:
        _work(job, calls, seller, include_products, store_key)
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


def _work(job: _Job, calls: _Calls, seller: str, include_products: bool, store_key: Optional[str] = None):
    eng = get_engine()
    ids, total = _seller_listings(seller, calls)
    job.c["listings_found"] = len(ids)
    if not ids:
        job.message = f"eBay returned no active listings for {seller} in category {_category_ids()}."
        job.save(calls, "done")
        return
    job.message = f"eBay returned {len(ids)} active listings. Comparing with what you already have…"
    job.save(calls)

    # which of them are already linked to a product ("mains" = the product's own listing, not a merged one)
    linked: dict[str, int] = {}
    mains: set[str] = set()
    with eng.connect() as c:
        for chunk in _chunks(ids, 2000):
            rows = c.execute(
                text(
                    "select digits, product_id, is_main from ("
                    " select regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') as digits, product_id, false as is_main"
                    "   from channel_listings where external_id is not null"
                    " union all"
                    " select regexp_replace(cast(ebay_item_id as text), '[^0-9]', '', 'g'), id, true"
                    "   from products where ebay_item_id is not null"
                    ") t where digits = any(cast(:ids as text[]))"
                ),
                {"ids": chunk},
            ).all()
            for d, pid, is_main in rows:
                linked[d] = pid
                if is_main:
                    mains.add(d)
    unlinked = [i for i in ids if i not in linked]

    # add the unlinked ones to the import table (empty label), skipping any already there
    added = 0
    with eng.begin() as c:
        for chunk in _chunks(unlinked, 1000):
            res = c.execute(
                text(
                    "insert into ebay_label_import (item_number, custom_label_sku, store) "
                    "select x, null, :st from unnest(cast(:ids as text[])) as x "
                    "where not exists (select 1 from ebay_label_import i where cast(i.item_number as text) = x)"
                ),
                {"ids": chunk, "st": store_key},
            )
            added += res.rowcount or 0
            # rows that were already there with no store get this store; a store already set is never changed
            if store_key:
                c.execute(
                    text(
                        "update ebay_label_import set store = :st "
                        "where store is null and cast(item_number as text) = any(cast(:ids as text[]))"
                    ),
                    {"ids": chunk, "st": store_key},
                )
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

    groups: list[tuple[Optional[str], list[str]]] = []
    by_label: dict[str, list[str]] = defaultdict(list)
    for item in todo:
        if item in labels:
            by_label[labels[item]].append(item)
        else:
            groups.append((None, [item]))
    groups = list(by_label.items()) + groups

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
        never = sorted((i for i in mine if i not in checked), key=lambda i: 0 if i in mains else 1)
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
    for label, group in groups:
        if job.stop_requested():
            stopped = True
            break
        _photos_for_group(job, calls, group, label)
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
        f"{c['photos_added']} photo rows added"
        + (f" ({job.reused} listings copied photos from a matching custom label)" if job.reused else "")
        + f", {c['no_photos']} listings eBay would not give photos for, {c['failed']} failed."
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


def _sibling_keys(label: str) -> list[str]:
    """Stored photos of another listing or product with the same custom label (same label = same part).

    A product with that label comes first (it is the finished copy), then another imported listing."""
    with get_engine().connect() as c:
        pid = c.execute(
            text(
                "select x.product_id from product_photos x join products p on p.id = x.product_id "
                "where trim(cast(p.custom_label_sku as text)) = :lab and x.image_key is not null "
                "group by x.product_id order by count(*) desc, x.product_id limit 1"
            ),
            {"lab": label},
        ).scalar()
        if pid is not None:
            rows = c.execute(
                text(
                    "select image_key from product_photos where product_id = :p and image_key is not null "
                    "order by sort_order, id"
                ),
                {"p": pid},
            ).all()
        else:
            rows = c.execute(
                text(
                    "select pp.image_key from product_photos pp "
                    "where pp.product_id is null and pp.image_key is not null and split_part(pp.storage_path, '/', 1) = ("
                    " select 'EBAY-' || cast(i.item_number as text) from ebay_label_import i"
                    "  where trim(cast(i.custom_label_sku as text)) = :lab"
                    "    and exists (select 1 from product_photos y where y.product_id is null"
                    "                and split_part(y.storage_path, '/', 1) = 'EBAY-' || cast(i.item_number as text))"
                    "  order by i.item_number limit 1) "
                    "order by pp.sort_order, pp.id"
                ),
                {"lab": label},
            ).all()
    keys: list[str] = []
    for (k,) in rows:
        if k not in keys:
            keys.append(k)
    return keys


def _save_rows(job: _Job, group: list[str], keys: list[str]):
    """Unlinked photo rows for every listing in the group, in one transaction."""
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


def _photos_for_group(job: _Job, calls: _Calls, group: list[str], label: Optional[str] = None):
    """Photos for listings that share a custom label: copied from a matching label if one has photos,
    otherwise one eBay lookup and download shared by every listing in the group."""
    if label:
        keys = _sibling_keys(label)
        if keys:
            _save_rows(job, group, keys)
            job.reused += len(group)
            return
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
    _save_rows(job, group, keys)


def _dhash(data: bytes) -> int:
    """A 64-bit fingerprint of how a picture looks (not its bytes), so re-saved copies still match."""
    im = Image.open(io.BytesIO(data)).convert("L").resize((9, 8), Image.LANCZOS)
    px = im.tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return bits


def _looks_same(a: int, b: int) -> bool:
    return bin(a ^ b).count("1") <= 6


def _stored_fingerprints(keys: set) -> list[int]:
    """Fingerprints of a product's stored photos, read from their thumbnails in the bucket."""
    base = get_settings().image_base_url.rstrip("/")
    out: list[int] = []
    for k in keys:
        try:
            out.append(_dhash(_download(f"{base}/{photo_store.thumb_path(k)}")))
        except Exception:
            continue  # unreadable thumbnail: it just cannot be compared
    return out


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
    prints: Optional[list[int]] = None  # loaded only if something needs comparing
    for url in new_urls:
        try:
            p = photo_store.process_image(_download(url))
            if p.key in have_keys:  # same picture already on the product
                continue
            if prints is None:
                prints = _stored_fingerprints(have_keys)
            mine_print = _dhash(p.thumb)
            if any(_looks_same(mine_print, q) for q in prints):  # same picture, saved a different way
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
        prints.append(mine_print)
        job.c["photos_added"] += 1


# ------------------------------------------- each listing's own original photos
_DIG = "regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g')"
_NEEDS_TABLE = (
    "The listing-photo run needs one small table first. Run the SQL for it in the Supabase SQL editor "
    "(ebay_listing_photo_checked), then start the run again."
)


def _original_candidates(store_key: str, product_id: Optional[int], item_id: Optional[str] = None) -> list[tuple[str, int]]:
    """Listings still to do: ones that share a product with other listings (merged) or already have a saved
    ebay_listings row. A listing that is alone on its product already has its own photos on that product."""
    with get_engine().connect() as c:
        rows = c.execute(
            text(
                f"""
                select {_DIG} as item, min(cl.product_id) as product_id, min(cl.id) as first_id
                from channel_listings cl
                left join ebay_listings el on el.ebay_item_id = {_DIG}
                where lower(cl.store) = lower(:st) and cl.product_id is not null and {_DIG} <> ''
                  and (el.ebay_item_id is not null
                       or (select count(*) from channel_listings x where x.product_id = cl.product_id) > 1)
                  and not exists (select 1 from ebay_listing_photo_checked k where k.item_number = {_DIG})
                  and (cast(:pid as bigint) is null or cl.product_id = cast(:pid as bigint))
                  and (cast(:item as text) is null or {_DIG} = cast(:item as text))
                group by 1
                order by first_id
                """
            ),
            {"st": store_key, "pid": product_id, "item": item_id},
        ).all()
    return [(r[0], r[1]) for r in rows]


def _mark_checked(item: str, result: str):
    with get_engine().begin() as c:
        c.execute(
            text(
                "insert into ebay_listing_photo_checked (item_number, result, checked_at) values (:i, :r, now()) "
                "on conflict (item_number) do update set result = :r, checked_at = now()"
            ),
            {"i": item, "r": result},
        )


_INSERT_LISTING_SQL = """
insert into ebay_listings
  (ebay_item_id, store, product_id, original_product_id, custom_label_sku, sku, title,
   list_price, quantity_on_hand, condition, description, item_specifics,
   fitment_vehicle_ids, photos, product_data, merge_log_id, source)
select :i, :st, p.id,
       case when o.own then p.id end,
       coalesce(nullif(btrim((select r.custom_label_sku from ebay_report_rows r where r.item_id = :i limit 1)), ''),
                nullif(btrim((select a.custom_label_sku from ebay_api_details a where a.item_id = :i limit 1)), ''),
                case when o.own then p.custom_label_sku end),
       p.sku_display,
       coalesce(nullif(btrim((select r.title from ebay_report_rows r where r.item_id = :i limit 1)), ''),
                nullif(btrim((select a.title from ebay_api_details a where a.item_id = :i limit 1)), ''),
                nullif(btrim((select m.title from ebay_compat_items m where m.item_id = :i limit 1)), ''),
                case when o.own then p.title end),
       case when o.own then p.list_price end,
       case when o.own then p.quantity_on_hand end,
       case when o.own then to_jsonb(p)->>'condition' end,
       case when o.own then to_jsonb(p)->>'description' end,
       case when o.own then (select coalesce(jsonb_agg(to_jsonb(s)), '[]'::jsonb)
                               from product_item_specifics s where s.product_id = p.id)
            else '[]'::jsonb end,
       case when o.own then (select coalesce(array_agg(pf.vehicle_id order by pf.vehicle_id), '{}'::bigint[])
                               from product_fitment pf where pf.product_id = p.id)
            else '{}'::bigint[] end,
       cast(:ph as jsonb),
       case when o.own then to_jsonb(p) else '{}'::jsonb end,
       null, 'backfill'
from products p
cross join lateral (
  select regexp_replace(coalesce(cast(p.ebay_item_id as text), ''), '[^0-9]', '', 'g') = :i as own) o
where p.id = :pid
on conflict (ebay_item_id) do update set photos = excluded.photos, updated_at = now()
"""


def _as_list(v) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def _originals_for(job: _Job, calls: _Calls, item: str, product_id: int, store_key: str, tally: dict):
    """One listing: saves the photos it has on eBay into its own ebay_listings row, skipping the ones it has."""
    with get_engine().connect() as c:
        row = c.execute(
            text("select photos, source from ebay_listings where ebay_item_id = :i"), {"i": item}
        ).first()
        pkeys = {
            r[0]
            for r in c.execute(
                text("select image_key from product_photos where product_id = :p and image_key is not null"),
                {"p": product_id},
            )
        }
    existing = [e for e in _as_list(row[0]) if isinstance(e, dict) and e.get("image_key")] if row else []
    ekeys = {e["image_key"] for e in existing}
    # a "backfill" set that is nothing but the product's current photos is a copy, not the listing's own
    suspect = bool(row) and (row[1] or "") == "backfill" and bool(existing) and ekeys <= pkeys

    urls = _ebay_photos(item, calls)
    if not urls:  # ended, removed, or a multi-variation listing: eBay's public lookup will not show it
        tally["no_ebay"] += 1
        job.c["no_photos"] += 1
        _mark_checked(item, "no_ebay_photos")
        return

    final = [] if suspect else [dict(e) for e in existing]
    have_ids = {photo_key(e["source_url"]) for e in final if e.get("source_url")}
    have_keys = {e["image_key"] for e in final}
    storage_has = pkeys | ekeys  # already uploaded: no need to send these again
    prints: Optional[list[int]] = None
    added, problem = 0, None
    for url in urls:
        if photo_key(url) in have_ids:
            continue
        try:
            p = photo_store.process_image(_download(url))
            if p.key in have_keys:
                have_ids.add(photo_key(url))
                continue
            if prints is None:
                prints = _stored_fingerprints(have_keys)
            mine = _dhash(p.thumb)
            if any(_looks_same(mine, q) for q in prints):  # same picture, saved a different way
                continue
            if p.key not in storage_has:
                photo_store.upload(p)
        except Exception as e:
            if _fatal_storage(e):
                raise
            problem = str(e)
            break
        final.append(
            {
                "image_key": p.key,
                "source_url": url,
                "storage_path": photo_store.full_path(p.key),
                "is_primary": False,
                "sort_order": 0,
            }
        )
        have_keys.add(p.key)
        have_ids.add(photo_key(url))
        prints.append(mine)
        added += 1
    if problem:  # never a partial set: nothing is written and the listing is tried again next time
        job.fail(item, problem)
        return
    if not final:
        job.fail(item, "no photo could be stored")
        return

    for n, e in enumerate(final):
        e["sort_order"] = n
        if suspect:
            e["is_primary"] = n == 0
    if not any(e.get("is_primary") is True for e in final):
        final[0]["is_primary"] = True

    if row and [e["image_key"] for e in final] == [e["image_key"] for e in existing]:
        tally["unchanged"] += 1
        _mark_checked(item, "unchanged")
        return

    ph = json.dumps(final)
    with get_engine().begin() as c:
        if row:
            c.execute(
                text("update ebay_listings set photos = cast(:ph as jsonb), updated_at = now() where ebay_item_id = :i"),
                {"ph": ph, "i": item},
            )
        else:
            # Everything hinges on the eBay item number. When it IS the product's own item number, the product
            # is this listing's original, so its description, price, specifics and fitment are this listing's own
            # and are copied. For any other listing (merged in later) only facts we have are saved: the item
            # number, store, linked product, the title and label saved for this exact item number (report, then
            # eBay lookup) and its photos. The rest stays empty rather than guessed.
            c.execute(text(_INSERT_LISTING_SQL), {"i": item, "st": store_key, "ph": ph, "pid": product_id})
    job.c["photos_added"] += added
    if not row:
        tally["created"] += 1
    elif suspect:
        tally["replaced"] += 1
    else:
        tally["added_to"] += 1
    _mark_checked(item, "saved")


def _originals_message(tally: dict, left: int = 0) -> str:
    bits = [
        f"{tally['replaced']} copied sets replaced with the listing's own photos",
        f"{tally['created']} listing rows created",
        f"{tally['added_to']} sets got extra photos",
        f"{tally['unchanged']} already complete",
        f"{tally['no_ebay']} not shown by eBay's public lookup (ended, removed or multi-variation)",
    ]
    return "; ".join(bits) + "."


def _work_originals(job: _Job, calls: _Calls, store_key: str, product_id: Optional[int], item_id: Optional[str] = None):
    cands = _original_candidates(store_key, product_id, item_id)
    job.c["items_total"] = len(cands)
    tally = {"replaced": 0, "created": 0, "added_to": 0, "unchanged": 0, "no_ebay": 0}
    if not cands:
        job.message = "Nothing left to do: every merged listing has been checked."
        job.save(calls, "done")
        return
    stopped = False
    for n, (item, pid) in enumerate(cands, 1):
        if job.stop_requested():
            stopped = True
            break
        try:
            _originals_for(job, calls, item, pid, store_key, tally)
        except (RateLimited, FeedbackError):
            raise
        except Exception as e:  # one bad listing must not stop the rest
            if _fatal_storage(e):
                raise
            log.exception("listing photos: item %s failed", item)
            job.fail(item, str(e))
        job.c["items_done"] = n
        if n % 10 == 0:
            job.message = f"Working… {_originals_message(tally)}"
            job.save(calls)
    job.message = _originals_message(tally)
    job.save(calls, "stopped" if stopped else "done")


# ------------------------- make a listing's saved photos exactly what eBay has (fixes wrong/extra photos)
def _exact_candidates(store_key: str, product_id: Optional[int], item_id: Optional[str] = None) -> list[tuple[str, int]]:
    """Listings of this store to make exact: merged ones, ones that already have a saved row, or (when a product
    number is typed) every listing of that product. Ended listings count; listings with no saved row get one."""
    with get_engine().connect() as c:
        rows = c.execute(
            text(
                f"""
                select {_DIG} as item, min(cl.product_id) as product_id
                from channel_listings cl
                left join ebay_listings el on el.ebay_item_id = {_DIG}
                where lower(cl.store) = lower(:st) and cl.product_id is not null and {_DIG} <> ''
                  and (cast(:pid as bigint) is not null or cast(:item as text) is not null
                       or el.ebay_item_id is not null
                       or (select count(*) from channel_listings x where x.product_id = cl.product_id) > 1)
                  and (cast(:pid as bigint) is not null or cast(:item as text) is not null
                       or not exists (select 1 from ebay_listing_photo_checked k
                                      where k.item_number = {_DIG} and k.result = 'exact'))
                  and (cast(:pid as bigint) is null or cl.product_id = cast(:pid as bigint))
                  and (cast(:item as text) is null or {_DIG} = cast(:item as text))
                group by 1
                order by 1
                """
            ),
            {"st": store_key, "pid": product_id, "item": item_id},
        ).all()
    return [(str(r[0]), r[1]) for r in rows]


def _sort_no(e: dict) -> int:
    v = e.get("sort_order")
    return v if isinstance(v, int) else 0


def _exact_for(job: _Job, item: str, product_id: int, store_key: str, tally: dict):
    """Replace one listing's saved photo set with exactly what eBay shows for it, in eBay's order.

    Reads through the connected seller account (the same read the edit screen uses), so ended listings work too.
    Only the listing's saved list changes; no stored picture file is ever deleted."""
    with Session(get_engine()) as db:
        data = _read_item(db, store_key, item)
    urls: list[str] = []
    seen: set[str] = set()
    for p in data.get("photos") or []:
        u = (p.get("url") or "").strip()
        if u and photo_key(u) not in seen:
            seen.add(photo_key(u))
            urls.append(u)
    if not urls:
        tally["no_photos"] += 1
        _mark_checked(item, "exact_no_photos")
        return
    with get_engine().connect() as c:
        row = c.execute(text("select photos from ebay_listings where ebay_item_id = :i"), {"i": item}).first()
    old = [e for e in _as_list(row[0] if row else None) if isinstance(e, dict) and e.get("image_key")]
    old.sort(key=_sort_no)
    by_id = {photo_key(e["source_url"]): e for e in old if e.get("source_url")}
    final: list[dict] = []
    have: set[str] = set()
    for url in urls:
        e = by_id.get(photo_key(url))
        if e is not None:  # eBay's picture is one we already stored for this listing
            key, path = e["image_key"], e.get("storage_path") or photo_store.full_path(e["image_key"])
        else:
            p = photo_store.process_image(_download(url))
            photo_store.upload(p)
            key, path = p.key, photo_store.full_path(p.key)
        if key in have:
            continue
        have.add(key)
        final.append({"image_key": key, "source_url": url, "storage_path": path, "is_primary": False, "sort_order": 0})
    if not final:
        job.fail(item, "no photo could be stored")
        return
    for n, e in enumerate(final):
        e["sort_order"] = n
        e["is_primary"] = n == 0
    old_keys = [e["image_key"] for e in old]
    old_primary = next((e["image_key"] for e in old if e.get("is_primary") is True), None)
    if old_keys == [e["image_key"] for e in final] and old_primary == final[0]["image_key"]:
        tally["unchanged"] += 1
    else:
        ph = json.dumps(final)
        with get_engine().begin() as c:
            if row:
                c.execute(
                    text("update ebay_listings set photos = cast(:ph as jsonb), updated_at = now() where ebay_item_id = :i"),
                    {"ph": ph, "i": item},
                )
                tally["fixed"] += 1
                tally["dropped"] += len([k for k in old_keys if k not in have])
            else:  # no saved row yet (typical for ended listings): create it with eBay's photos
                c.execute(text(_INSERT_LISTING_SQL), {"i": item, "st": store_key, "ph": ph, "pid": product_id})
                tally["created"] += 1
    _mark_checked(item, "exact")


def _why_none(store_key: str, item_id: str) -> str:
    with get_engine().connect() as c:
        r = c.execute(
            text(f"select cl.store, cl.product_id from channel_listings cl where {_DIG} = :i limit 1"), {"i": item_id}
        ).first()
    if r is None:
        return f"eBay item {item_id} is not in your linked listings at all, so there is nothing to fix. Check the number."
    if lower_store_differs(r[0], store_key):
        return f"eBay item {item_id} is filed under the store '{r[0]}', not '{store_key}'. Put that store name in the box above."
    if r[1] is None:
        return f"eBay item {item_id} is not linked to a product yet, so it was skipped. Link it first."
    return f"Nothing found for eBay item {item_id}."


def lower_store_differs(a, b) -> bool:
    return (a or "").strip().lower() != (b or "").strip().lower()


def _work_exact(job: _Job, calls: _Calls, store_key: str, product_id: Optional[int], item_id: Optional[str] = None):
    cands = _exact_candidates(store_key, product_id, item_id)
    job.c["items_total"] = len(cands)
    tally = {"fixed": 0, "created": 0, "unchanged": 0, "no_photos": 0, "dropped": 0}
    if not cands:
        job.message = (
            _why_none(store_key, item_id)
            if item_id
            else "Nothing left to do: every merged or saved listing of this store has been made to match eBay (or none matched the product number)."
        )
        job.save(calls, "done")
        return

    def msg() -> str:
        return (
            f"{tally['created']} listings had no saved photos and got eBay's set; "
            f"{tally['fixed']} saved photo sets rewritten to match eBay ({tally['dropped']} photos that eBay does not "
            f"show on that listing taken off the set); {tally['unchanged']} already matched; "
            f"{tally['no_photos']} had no photos on eBay."
        )

    stopped, bad_in_a_row = False, 0
    for n, (item, _pid) in enumerate(cands, 1):
        if job.stop_requested():
            stopped = True
            break
        try:
            _exact_for(job, item, _pid, store_key, tally)
            bad_in_a_row = 0
        except Exception as e:  # one bad listing must not stop the rest
            if _fatal_storage(e):
                raise
            log.exception("exact listing photos: item %s failed", item)
            why = getattr(e, "detail", None) or str(e)
            job.fail(item, str(why))
            bad_in_a_row += 1
            if bad_in_a_row >= 5:
                job.message = f"Stopped after 5 failures in a row ({why}). {msg()}"
                job.save(calls, "failed")
                return
        job.c["items_done"] = n
        if n % 10 == 0:
            job.message = f"Working… {msg()}"
            job.save(calls)
        time.sleep(0.2)
    job.message = msg()
    job.save(calls, "stopped" if stopped else "done")


def _run_originals(job_id: int, store_key: str, product_id: Optional[int], exact: bool = False, item_id: Optional[str] = None):
    job = _Job(job_id)
    calls = _Calls(_max_calls())
    try:
        (_work_exact if exact else _work_originals)(job, calls, store_key, product_id, item_id)
    except RateLimited as e:
        job.message = f"Paused: {e} Start it again later and it carries on where it stopped."
        job.save(calls, "paused")
    except (FeedbackError, photo_store.StorageError) as e:
        job.message = str(e)
        job.save(calls, "failed")
    except Exception:
        log.exception("listing photo run crashed")
        job.message = "Something unexpected went wrong. The details are in the API log."
        job.save(calls, "failed")


# --------------------------------------------------------------------- routes
class ImportIn(BaseModel):
    store: str = ""
    mode: str
    product_id: Optional[int] = None  # listing_originals only: do just this product's listings (a test run)
    item_id: Optional[str] = None  # listing_originals only: do just this one eBay item number
    exact: bool = False  # listing_originals only: make each saved set exactly eBay's (removes wrong photos)


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
    store_key = _resolve(db, body.store)["store"]  # the store name the other imports file things under
    _expire_stale(db.execute)
    running = db.execute(text("select id from ebay_photo_jobs where status in ('running','stopping') limit 1")).first()
    if running:
        db.commit()
        raise HTTPException(409, "An import is already running. Wait for it to finish or stop it first.")
    originals = body.mode == "listing_originals"
    item_id = (body.item_id or "").strip() or None
    if body.product_id is not None and body.product_id >= 100_000_000 and not item_id:
        item_id, body.product_id = str(body.product_id), None  # an eBay item number typed as a product number
    if item_id and not re.fullmatch(r"\d{9,15}", item_id):
        raise HTTPException(400, "That does not look like an eBay item number.")
    if originals and not db.execute(text("select to_regclass('ebay_listing_photo_checked')")).scalar():
        db.commit()
        raise HTTPException(503, _NEEDS_TABLE)
    try:
        job_id = db.execute(
            text(
                "insert into ebay_photo_jobs (store, mode, started_by, message) "
                "values (:s, :m, :u, 'Starting…') returning id"
            ),
            {"s": store, "m": body.mode, "u": user.email or str(user.user_id)},
        ).scalar()
        db.commit()
    except Exception:
        db.rollback()
        if originals:
            log.exception("could not record the listing photo run")
            raise HTTPException(503, "The jobs table does not accept this run type yet. " + _NEEDS_TABLE)
        raise
    if originals:
        threading.Thread(
            target=_run_originals, args=(job_id, store_key, body.product_id, body.exact, item_id), daemon=True, name=f"ebay-photos-{job_id}"
        ).start()
    else:
        threading.Thread(
            target=_run, args=(job_id, store, body.mode == "all_missing", store_key), daemon=True, name=f"ebay-photos-{job_id}"
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


@router.get("/product-for-item")
def product_for_item(item_id: str, db: Session = Depends(get_db)):
    """The product number (products.id) an eBay item number is linked to, for the photo run's product box."""
    item = (item_id or "").strip()
    if not re.fullmatch(r"\d{9,15}", item):
        raise HTTPException(400, "That does not look like an eBay item number.")
    count = "(select count(*) from channel_listings x where x.product_id = p.id)"
    row = db.execute(
        text(
            f"select p.id, p.title, cl.store, {count} as listings "
            "from channel_listings cl join products p on p.id = cl.product_id "
            "where regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = :i order by cl.id limit 1"
        ),
        {"i": item},
    ).first()
    if row is None:  # the product's own main listing
        row = db.execute(
            text(
                f"select p.id, p.title, null as store, {count} as listings from products p "
                "where regexp_replace(cast(p.ebay_item_id as text), '[^0-9]', '', 'g') = :i limit 1"
            ),
            {"i": item},
        ).first()
    if row is None:
        raise HTTPException(404, "No product is linked to that eBay item number.")
    return {"product_id": row.id, "title": row.title, "store": row.store, "listings": row.listings}


# ------------------------------------------------------- custom labels (CSV)
class LabelsIn(BaseModel):
    csv: str
    store: str = ""  # blank = do not record a store for these listings


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
    # Unlike the report upload, a blank store here means "unknown", not the main store, so a forgotten box
    # can never stamp the wrong store on a file.
    store_key = _resolve(db, body.store)["store"] if body.store.strip() else None
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
            text("insert into ebay_label_import (item_number, custom_label_sku, store) values (:i, :l, :st)"),
            {"i": i, "l": report[i] or None, "st": store_key},
        )
    if store_key:
        # listings already in the table with no store get this one; a store already set is never changed
        for chunk in _chunks([i for i in ids if i in existing], 3000):
            db.execute(
                text(
                    "update ebay_label_import set store = :st "
                    "where store is null and cast(item_number as text) = any(cast(:ids as text[]))"
                ),
                {"st": store_key, "ids": chunk},
            )
    db.commit()
    return {
        "store": store_key,
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


# ------------------------------------------------------- custom labels straight from eBay (no report)
class FillApiIn(BaseModel):
    store: str = ""  # required: the connected eBay store whose listings are asked about
    limit: int = 40  # listings to ask eBay about in this request (one call each)


# Listings in the import table with no label yet, for one store, that are not linked to a product and that
# eBay has not already been asked about (a saved answer in ebay_api_details, label or not, means "done").
_LABEL_TODO = """
from ebay_label_import i
where coalesce(trim(cast(i.custom_label_sku as text)), '') = ''
  and lower(coalesce(i.store, '')) = lower(:st)
  and not exists (select 1 from channel_listings cl
                  where regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = cast(i.item_number as text))
"""


@router.post("/labels-from-ebay")
def labels_from_ebay(body: FillApiIn, db: Session = Depends(get_db)):
    """Report-free label fill. Asks eBay (one call per listing, through the store's own login) for the custom label
    of listings in the import table that have none, and saves it. A label already set is never changed. Changes
    nothing on eBay. The answer is also kept in ebay_api_details, the same place the listing import keeps it, so
    the same listing is never asked about twice."""
    from app.admin_ebay_import import FETCH_MAX, _authorized_stores, _fetch_details  # here, not at the top: that module imports this one

    if not body.store.strip():
        raise HTTPException(400, "Type the eBay store name first, so the right store is asked.")
    store_key = _resolve(db, body.store)["store"]
    row = next((s for s in _authorized_stores(db) if s.store.lower() == store_key.lower()), None)
    if row is None:
        raise HTTPException(409, f"{store_key} is not connected to eBay yet, so eBay cannot be asked. Connect it on the Stores page, or use a report.")
    limit = max(1, min(int(body.limit), FETCH_MAX))
    params = {"st": store_key}

    # 1) labels eBay already gave us earlier (saved by the listing import): no eBay call needed
    saved = db.execute(
        text(
            "update ebay_label_import i set custom_label_sku = nullif(trim(a.custom_label_sku), '') "
            "from ebay_api_details a "
            "where a.item_id = cast(i.item_number as text) and a.status = 'ok' "
            "and nullif(trim(a.custom_label_sku), '') is not null "
            "and coalesce(trim(cast(i.custom_label_sku as text)), '') = '' "
            "and lower(coalesce(i.store, '')) = lower(:st)"
        ),
        params,
    ).rowcount or 0
    db.commit()

    todo = [
        r.item
        for r in db.execute(
            text(f"select cast(i.item_number as text) as item {_LABEL_TODO} "
                 "and not exists (select 1 from ebay_api_details a where a.item_id = cast(i.item_number as text)) "
                 "order by i.item_number desc"),
            params,
        ).all()
    ]
    db.rollback()

    asked = filled = no_label = inactive = other_store = errors = 0
    error_sample: list[str] = []
    rate_limited: Optional[str] = None
    for n, item in enumerate(todo[:limit], 1):
        try:
            d = _fetch_details(db, [row], item)
        except RateLimited as e:
            rate_limited = str(e)
            break
        if d["state"] == "error":
            errors += 1
            if len(error_sample) < 10:
                error_sample.append(f"{item}: {d['reason']}")
            if errors >= 5 and asked == 0:
                break  # nothing is working (approval expired, eBay down): stop instead of burning calls
            continue
        asked += 1
        if d["state"] == "skipped":
            if "Not on any connected store" in d["reason"]:
                other_store += 1  # belongs to another store: not saved, so that store can still ask about it
            else:
                inactive += 1
                db.execute(
                    text("insert into ebay_api_details (item_id, status, reason) values (:i, 'skipped', :r) "
                         "on conflict (item_id) do update set status = 'skipped', reason = excluded.reason, fetched_at = now()"),
                    {"i": item, "r": d["reason"]},
                )
        else:
            db.execute(
                text("insert into ebay_api_details (item_id, status, store, title, custom_label_sku, price, quantity, ebay_category) "
                     "values (:i, 'ok', :s, :t, :l, :p, :q, :c) "
                     "on conflict (item_id) do update set status = 'ok', reason = null, store = excluded.store, "
                     "title = excluded.title, custom_label_sku = excluded.custom_label_sku, price = excluded.price, "
                     "quantity = excluded.quantity, ebay_category = excluded.ebay_category, fetched_at = now()"),
                {"i": item, "s": d["store"], "t": d["title"], "l": d["label"], "p": d["price"], "q": d["qty"], "c": d["category"]},
            )
            if d["label"]:
                db.execute(
                    text("update ebay_label_import set custom_label_sku = :l where cast(item_number as text) = :i "
                         "and coalesce(trim(cast(custom_label_sku as text)), '') = ''"),
                    {"l": d["label"], "i": item},
                )
                filled += 1
            else:
                no_label += 1
        if n % 10 == 0:
            db.commit()
    db.commit()

    left = len(
        db.execute(
            text(f"select 1 {_LABEL_TODO} and not exists (select 1 from ebay_api_details a where a.item_id = cast(i.item_number as text))"),
            params,
        ).all()
    )
    no_store = db.execute(
        text("select count(*) from ebay_label_import i where coalesce(trim(cast(i.custom_label_sku as text)), '') = '' and i.store is null")
    ).scalar() or 0
    db.rollback()
    return {
        "store": store_key,
        "filled_from_saved": saved,
        "asked_ebay": asked,
        "labels_filled": filled,
        "no_label_on_ebay": no_label,
        "not_active": inactive,
        "other_store": other_store,
        "errors": errors,
        "error_sample": error_sample,
        "rate_limited": rate_limited,
        "remaining": left,
        "blank_without_store": int(no_store),
    }


# ------------------------------------------------------- custom labels for INVENTORY (the products themselves)
# The path above fills the import table for the eBay sales channel. This one fills products.custom_label_sku.
#
# For every product we collect its eBay item ids (every channel_listings row linked to it, plus the product's own
# EBAY-<id>, which is the source for a product that was never merged). For each id we know what we saved earlier
# (uploaded report, eBay lookup, label import table, merge snapshot) and, for active listings, we ask eBay live.
#
#   product has a label   -> every id's label must equal it. Any id with a different label is LISTED (id_mismatch),
#                            nothing is changed. All fine = already_ok.
#   product has no label  -> all ids must agree (saved vs eBay, and id vs id). One agreed label is applied (filled).
#                            No label anywhere = left blank (no_label). Any disagreement = listed (conflict) and
#                            nothing is applied.
#   an id eBay could not be asked about because of an error: the product is NOT recorded, so the next click retries.
#   an id that is ended / on a store that is not connected cannot be asked: its saved label is used, and the result
#   says how many ids were not verified live.
#
# Progress is kept in inventory_label_checks (one row per checked product), so every click carries on where the last
# one stopped. Nothing is ever changed on eBay, and a label that is already set is never overwritten.
class InvLabelsIn(BaseModel):
    limit: int = 100  # eBay lookups allowed in this click
    reset: str = "none"  # "none" | "flagged" (re-check products that were listed) | "all" (start over)


def _lab_norm(v) -> str:
    return (v or "").strip().upper()


def _ensure_inv_checks(db: Session) -> None:
    db.execute(
        text(
            """
            create table if not exists inventory_label_checks (
                product_id bigint primary key,
                outcome text not null,
                detail jsonb,
                checked_at timestamptz not null default now()
            )
            """
        )
    )
    db.commit()


def _judge_product(product_label: str, items: dict) -> dict:
    """Pure decision. items: {item_id: {"saved": [labels], "state": "ok"|"unverified", "ebay": label|None}}.
    Returns {"outcome", "label"?, "rows": [...], "unverified": n}."""
    rows = []
    best: dict[str, str] = {}
    conflicts = 0
    unverified = 0
    for item, d in items.items():
        saved = [s.strip() for s in d["saved"] if (s or "").strip()]
        saved_norm = {_lab_norm(s) for s in saved}
        why = ""
        if d["state"] == "ok":
            live = (d.get("ebay") or "").strip()
            if saved_norm and (not live or _lab_norm(live) not in saved_norm or len(saved_norm) > 1):
                why = "eBay and the saved label differ" if len(saved_norm) == 1 else "saved labels disagree with each other or eBay"
            final = live
        else:
            unverified += 1
            final = saved[0] if saved else ""
            if len(saved_norm) > 1:
                why = "saved labels disagree with each other"
        if why:
            conflicts += 1
        if final:
            best[item] = final
        rows.append({"item": item, "label": final or None, "saved": saved[0] if saved else None,
                     "ebay": (d.get("ebay") or None) if d["state"] == "ok" else None,
                     "checked_on_ebay": d["state"] == "ok", "problem": why or None})

    have = (product_label or "").strip()
    if have:
        bad = [r["item"] for r in rows if r["label"] and _lab_norm(r["label"]) != _lab_norm(have)]
        for r in rows:
            r["differs_from_product"] = r["item"] in bad
        return {"outcome": "id_mismatch" if bad else "already_ok", "rows": rows, "unverified": unverified}

    distinct = {_lab_norm(v) for v in best.values()}
    if conflicts or len(distinct) > 1:
        return {"outcome": "conflict", "rows": rows, "unverified": unverified}
    if len(distinct) == 1:
        return {"outcome": "filled", "label": next(iter(best.values())), "rows": rows, "unverified": unverified}
    return {"outcome": "no_label", "rows": rows, "unverified": unverified}


@router.post("/labels-inventory")
def labels_inventory(body: InvLabelsIn, db: Session = Depends(get_db)):
    from app.admin_ebay_import import _authorized_stores, _fetch_details  # here, not at the top: that module imports this one

    limit = max(1, min(int(body.limit), 100))
    _ensure_inv_checks(db)
    if body.reset == "all":
        db.execute(text("delete from inventory_label_checks"))
    elif body.reset == "flagged":
        db.execute(text("delete from inventory_label_checks where outcome in ('id_mismatch', 'conflict')"))
    db.commit()

    stores = _authorized_stores(db)
    by_store = {s.store.lower(): s for s in stores}

    counts = {"filled": 0, "already_ok": 0, "no_label": 0, "no_items": 0, "id_mismatch": 0, "conflict": 0}
    products_done = asked = unverified_ids = errors = 0
    error_sample: list[str] = []
    rate_limited: Optional[str] = None
    stop = False
    started = time.time()
    last_id = 0

    while not stop and asked < limit and time.time() - started < 85:
        batch = db.execute(
            text(
                "select p.id, btrim(coalesce(p.custom_label_sku, '')) as label, "
                "regexp_replace(coalesce(p.ebay_item_id, ''), '[^0-9]', '', 'g') as own "
                "from products p where p.id > :last "
                "and not exists (select 1 from inventory_label_checks c where c.product_id = p.id) "
                "order by p.id limit 200"
            ),
            {"last": last_id},
        ).all()
        if not batch:
            break
        pids = [b.id for b in batch]
        # every item id each product points to, with the store and status of its link
        links: dict[int, dict[str, dict]] = {pid: {} for pid in pids}
        for r in db.execute(
            text(
                "select cl.product_id as pid, regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') as digits, "
                "cl.store, lower(coalesce(cl.status, '')) as status from channel_listings cl "
                "where cl.product_id = any(cast(:p as bigint[])) and cl.external_id is not null"
            ),
            {"p": pids},
        ):
            if r.digits:
                links[r.pid][r.digits] = {"store": r.store, "active": r.status == "active"}
        for b in batch:
            if b.own and b.own not in links[b.id]:
                links[b.id][b.own] = {"store": None, "active": True}  # source id of an unmerged product: ask eBay
        all_ids = sorted({d for m in links.values() for d in m})
        saved: dict[str, list[str]] = defaultdict(list)
        for chunk in _chunks(all_ids, 3000):
            for item, lab in db.execute(
                text(
                    "select item, lab from ("
                    " select item_id as item, custom_label_sku as lab, 1 as o from ebay_report_rows"
                    " union all select item_id, custom_label_sku, 2 from ebay_api_details where status = 'ok'"
                    " union all select cast(item_number as text), cast(custom_label_sku as text), 3 from ebay_label_import"
                    " union all select ebay_item_id, custom_label_sku, 4 from ebay_listings"
                    ") t where item = any(cast(:ids as text[])) and nullif(btrim(lab), '') is not null order by o"
                ),
                {"ids": chunk},
            ):
                if lab.strip() not in saved[item]:
                    saved[item].append(lab.strip())

        for b in batch:
            last_id = b.id
            ids = links[b.id]
            if not ids:
                db.execute(text("insert into inventory_label_checks (product_id, outcome) values (:p, 'no_items') "
                                "on conflict (product_id) do nothing"), {"p": b.id})
                counts["no_items"] += 1
                products_done += 1
                continue
            cost = sum(1 if v["store"] and v["store"].lower() in by_store else (len(stores) or 0)
                       for v in ids.values() if v["active"] and (not v["store"] or v["store"].lower() in by_store))
            if asked > 0 and asked + cost > limit:
                stop = True
                break

            items: dict[str, dict] = {}
            product_failed = False
            for item, v in ids.items():
                d = {"saved": saved.get(item, []), "state": "unverified", "ebay": None}
                can_ask = v["active"] and (not v["store"] or v["store"].lower() in by_store)
                if can_ask:
                    use = [by_store[v["store"].lower()]] if v["store"] else list(stores)
                    try:
                        r = _fetch_details(db, use, item)
                    except RateLimited as e:
                        rate_limited = str(e)
                        product_failed = stop = True
                        break
                    asked += len(use) if not v["store"] else 1
                    if r["state"] == "ok":
                        d["state"], d["ebay"] = "ok", r["label"]
                    elif r["state"] == "error":
                        errors += 1
                        if len(error_sample) < 10:
                            error_sample.append(f"{item}: {r['reason']}")
                        product_failed = True
                        break
                    # "skipped" = ended or not on a connected store: saved label is used, counted as not verified
                items[item] = d
            if product_failed:
                if errors >= 5 and products_done == 0:
                    stop = True
                    break
                if stop:
                    break
                continue  # not recorded: retried on the next click

            j = _judge_product(b.label, items)
            if j["outcome"] == "filled":
                n = db.execute(
                    text("update products set custom_label_sku = :l, updated_at = now() "
                         "where id = :p and coalesce(btrim(custom_label_sku), '') = ''"),
                    {"l": j["label"], "p": b.id},
                ).rowcount or 0
                if not n:
                    j["outcome"] = "already_ok"
            unverified_ids += j["unverified"]
            detail = {"label": j.get("label"), "unverified": j["unverified"], "items": j["rows"][:40]}
            db.execute(
                text("insert into inventory_label_checks (product_id, outcome, detail) values (:p, :o, cast(:d as jsonb)) "
                     "on conflict (product_id) do update set outcome = excluded.outcome, detail = excluded.detail, checked_at = now()"),
                {"p": b.id, "o": j["outcome"], "d": json.dumps(detail)},
            )
            counts[j["outcome"]] += 1
            products_done += 1
            if products_done % 10 == 0:
                db.commit()
        db.commit()
        if time.time() - started >= 85:
            break
    db.commit()

    left = db.execute(
        text("select count(*) from products p where not exists (select 1 from inventory_label_checks c where c.product_id = p.id)")
    ).scalar() or 0
    totals = {r.outcome: r.n for r in db.execute(text("select outcome, count(*) as n from inventory_label_checks group by 1"))}
    flagged = [
        {
            "product_id": r.product_id, "sku": r.sku, "title": r.title, "product_label": (r.label or None),
            "outcome": r.outcome, "items": (r.detail or {}).get("items", []),
        }
        for r in db.execute(
            text(
                "select c.product_id, c.outcome, c.detail, p.sku_display as sku, p.title, btrim(p.custom_label_sku) as label "
                "from inventory_label_checks c join products p on p.id = c.product_id "
                "where c.outcome in ('id_mismatch', 'conflict') order by c.checked_at desc, c.product_id limit 100"
            )
        )
    ]
    db.rollback()
    return {
        "this_click": {**counts, "products": products_done, "asked_ebay": asked, "unverified_ids": unverified_ids},
        "totals": totals,
        "remaining": int(left),
        "errors": errors,
        "error_sample": error_sample,
        "rate_limited": rate_limited,
        "flagged": flagged,
        "flagged_total": int(totals.get("id_mismatch", 0) + totals.get("conflict", 0)),
    }
