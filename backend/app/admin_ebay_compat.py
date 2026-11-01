"""
eBay compatibility import: the back end of the admin "eBay compat import" tab.

Mounted at /admin/ebay-compat/* (owner/admin only).

  POST /start    {store}   starts a background run (one at a time)
  GET  /status             the latest run plus totals saved per store, for the progress display (poll it)
  POST /stop               asks the running run to stop after its current listing

What a run does:

  1. Asks eBay (public Browse API, same access the photo import uses) for the store's ACTIVE listings.
  2. Skips every item number that already has compat data in ebay_compat_items. That includes everything
     the first import (pull_ebay_compat.py, eandimotorsports) saved, which the migration copied in.
     A listing is only looked up again if it has never been saved (a new listing) or its last try ended
     in an error.
  3. For each remaining listing, asks eBay's Trading API (GetItem) for the listing's "compatible vehicles"
     table with the store's own approval (the Connect eBay tab) and saves the answer exactly as eBay sent
     it, together with the listing title.
  3b. "Re-check" mode also re-reads listings that are already saved (not read in the last 7 days) and adds
     any vehicle eBay now lists that was not saved before. Old rows are kept; nothing is removed. A listing
     with nothing new is only marked as checked. This takes one eBay call per saved listing, so a store with
     thousands of listings takes a few days of daily limits; it is resumable because checked listings are
     skipped for 7 days.
  4. At the end, reads make / model / years out of the saved titles with the database function
     parse_fitment_title() and saves them beside the eBay table (parsed_* columns).

Nothing here touches products, vehicles or product_fitment. Item numbers are the key, so it works for
listings that are not products yet (genuine-oem-parts).

Limits: eBay allows roughly 5,000 Trading calls a day. A run stops by itself at EBAY_COMPAT_MAX_CALLS
(default 4500) or when eBay says the daily limit is reached (error 518); start it again later and it
carries on, because saved listings are skipped. Progress lives in the database (ebay_compat_jobs).
A run cut off by a server restart shows as "failed (interrupted)" and can simply be started again.
"""
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.admin_ebay_listings import _resolve
from app.admin_ebay_photos import RateLimited, _Calls, _chunks, _seller_listings
from app.auth import AuthUser, require_admin
from app.database import get_db, get_engine
from app.ebay_edit import NS, _errors, _trading
from app.ebay_feedback import FeedbackError, configured
from app.ebay_oauth import _expected_username, _store_row, get_user_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-compat", tags=["admin-ebay-compat"], dependencies=[Depends(require_admin)])

ITEM_RE = re.compile(r"\d{9,15}")
STALE_MINUTES = 10  # a "running" run with no sign of life for this long was cut off by a restart
COMMIT_EVERY = 25
MAX_CONSECUTIVE_ERRORS = 3
DELAY = 0.5  # seconds between eBay calls
NOT_FOUND_CODES = {"17"}  # "item cannot be accessed" (ended / deleted / not yours)
RATE_LIMIT_CODE = "518"  # "call usage limit has been reached"
RECHECK_DAYS = 7  # a saved listing read again within this many days is skipped in re-check mode
COUNTERS = ("listings_found", "already_saved", "items_total", "items_done", "with_compat", "compat_rows",
            "no_list", "count_only", "not_found", "failed", "rechecked", "changed", "new_rows")


def _max_calls() -> int:
    try:
        return max(50, int(os.environ.get("EBAY_COMPAT_MAX_CALLS") or 4500))
    except ValueError:
        return 4500


# ------------------------------------------------------------ reading eBay's reply
def _add_attr(attrs: dict, name: str, value: Optional[str]):
    if name in attrs:
        cur = attrs[name]
        attrs[name] = (cur if isinstance(cur, list) else [cur]) + [value]
    else:
        attrs[name] = value


def _read_listing(db: Session, store: str, expected_user: str, item_id: str) -> dict:
    """One GetItem call. Returns {status, title, count, compat, code, message}; raises RateLimited at eBay's limit.

    status is one of ok | count_only | no_list | not_found | error. The compat list uses the same layout as
    ebay_compat_raw: [{"attrs": {"Make": ..., "Model": ..., "Year": ...}, "notes": ...}, ...].
    """
    try:
        root = _trading(
            db, store, "GetItem",
            f"<ItemID>{item_id}</ItemID><DetailLevel>ReturnAll</DetailLevel>"
            "<IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>",
        )
    except HTTPException as e:  # network trouble, unreadable answer, store not approved
        return {"status": "error", "code": str(e.status_code), "message": str(e.detail)}

    ack = root.findtext(f"{NS}Ack") or ""
    if ack not in ("Success", "Warning"):
        errs = [e for e in _errors(root) if e["severity"] == "Error"] or _errors(root)
        codes = {e["code"] for e in errs}
        if RATE_LIMIT_CODE in codes:
            raise RateLimited("eBay says the daily call limit has been reached (error 518).")
        first = errs[0] if errs else {"code": "", "message": "eBay reported a failure without details."}
        return {
            "status": "not_found" if codes & NOT_FOUND_CODES else "error",
            "code": first["code"],
            "message": first["message"],
        }

    item = root.find(f"{NS}Item")
    if item is None:
        return {"status": "error", "code": "", "message": "eBay's reply had no listing in it."}

    seller = (item.findtext(f"{NS}Seller/{NS}UserID") or "").strip()
    if seller and seller.lower() != expected_user.lower():
        return {"status": "error", "code": "wrong_seller",
                "message": f"Belongs to '{seller}', not '{expected_user}'. Not saved."}

    title = (item.findtext(f"{NS}Title") or "").strip()
    count_txt = (item.findtext(f"{NS}ItemCompatibilityCount") or "").strip()
    count = int(count_txt) if count_txt.isdigit() else None

    compat = []
    for comp in item.findall(f"{NS}ItemCompatibilityList/{NS}Compatibility"):
        attrs: dict = {}
        for nv in comp.findall(f"{NS}NameValueList"):
            name = (nv.findtext(f"{NS}Name") or "").strip()
            if not name:
                continue
            values = [v.text for v in nv.findall(f"{NS}Value")]
            _add_attr(attrs, name, values[0] if len(values) == 1 else values)
        compat.append({"attrs": attrs, "notes": comp.findtext(f"{NS}CompatibilityNotes")})

    if compat:
        return {"status": "ok", "title": title, "count": count if count is not None else len(compat), "compat": compat}
    if count:
        return {"status": "count_only", "title": title, "count": count}
    return {"status": "no_list", "title": title, "count": count or 0}


# ------------------------------------------------------------ run bookkeeping
class _Job:
    def __init__(self, job_id: int):
        self.id = job_id
        self.c = {k: 0 for k in COUNTERS}
        self.failures: list[str] = []
        self.message = ""

    def fail(self, item: str, why: str):
        self.c["failed"] += 1
        if len(self.failures) < 40:
            self.failures.append(f"{item}: {why}"[:300])

    def save(self, trading_calls: int = 0, status: Optional[str] = None):
        sets = dict(self.c)
        sets["message"] = self.message
        sets["ebay_calls"] = trading_calls
        sql = ", ".join(f"{k} = :{k}" for k in sets) + ", failures = cast(:failures as jsonb), updated_at = now()"
        params = {**sets, "failures": json.dumps(self.failures), "id": self.id}
        if status:
            sql += ", status = :status, finished_at = now()"
            params["status"] = status
        with get_engine().begin() as c:
            # never overwrite a stop request with a plain progress save
            where = "id = :id" if status else "id = :id and status in ('running','stopping')"
            c.execute(text(f"update ebay_compat_jobs set {sql} where {where}"), params)

    def stop_requested(self) -> bool:
        with get_engine().connect() as c:
            return c.execute(text("select status from ebay_compat_jobs where id = :i"), {"i": self.id}).scalar() == "stopping"


def _expire_stale(db_exec):
    db_exec(
        text(
            "update ebay_compat_jobs set status = 'failed', finished_at = now(), "
            "message = 'Interrupted (the server restarted or stopped). Start it again; saved listings are skipped.' "
            f"where status in ('running','stopping') and updated_at < now() - interval '{STALE_MINUTES} minutes'"
        )
    )


_UPSERT = text(
    "insert into ebay_compat_items (item_id, store, title, status, compat_count, compat_list, source, fetched_at) "
    "values (:item, :store, :title, :status, :count, cast(:compat as jsonb), 'import_tab', now()) "
    "on conflict (item_id) do update set store = excluded.store, title = excluded.title, status = excluded.status, "
    "compat_count = excluded.compat_count, compat_list = excluded.compat_list, error_code = null, "
    "error_message = null, source = 'import_tab', fetched_at = excluded.fetched_at, checked_at = now(), parsed_at = null"
)

def _row_key(entry: dict) -> str:
    """Identity of one compatibility row (Make/Model/Year/etc.), ignoring case, spacing and the notes."""
    attrs = (entry or {}).get("attrs") or {}
    parts = []
    for k, v in attrs.items():
        vals = v if isinstance(v, list) else [v]
        parts.append((str(k).strip().lower(), tuple(sorted(str(x or "").strip().lower() for x in vals))))
    return json.dumps(sorted(parts))


def _merge_saved(conn, item: str, r: dict) -> int:
    """Add the vehicles eBay lists now that were not saved before. Returns how many were added."""
    cur = conn.execute(
        text("select compat_list, title from ebay_compat_items where item_id = :i"), {"i": item}
    ).first()
    old = cur.compat_list if cur else None
    if isinstance(old, str):  # the first import stored some lists as a JSON string
        try:
            old = json.loads(old)
        except ValueError:
            old = None
    old = old if isinstance(old, list) else []
    seen = {_row_key(e) for e in old}
    added = []
    for e in r.get("compat") or []:
        k = _row_key(e)
        if k not in seen:
            seen.add(k)
            added.append(e)
    title = r.get("title") or None
    if added:
        merged = old + added
        conn.execute(
            text(
                "update ebay_compat_items set compat_list = cast(:l as jsonb), compat_count = :n, status = 'ok', "
                "title = coalesce(:t, title), "
                "parsed_at = case when :t is not null and :t is distinct from title then null else parsed_at end, "
                "checked_at = now(), changed_at = now(), new_rows_last = :a where item_id = :i"
            ),
            {"l": json.dumps(merged), "n": max(len(merged), r.get("count") or 0), "t": title, "a": len(added), "i": item},
        )
    else:
        conn.execute(
            text(
                "update ebay_compat_items set checked_at = now(), title = coalesce(title, :t), "
                "parsed_at = case when title is null and :t is not null then null else parsed_at end "
                "where item_id = :i"
            ),
            {"t": title, "i": item},
        )
    return len(added)


# Title parsing: same parser and same status words as fitment stage 1 (fitment_1_parse.sql).
_PARSE = text(
    r"""
    update ebay_compat_items i
    set parsed_make = f.o_make,
        parsed_model = nullif(f.o_model, ''),
        parsed_submodel = nullif(f.o_submodel, ''),
        parsed_years = f.o_years,
        parsed_status = case
            when t.title ~* '\muniversal\M'                  then 'universal'
            when f.o_make is null                            then 'no_make'
            when coalesce(f.o_model, '') = ''                then 'no_model'
            when coalesce(array_length(f.o_years, 1), 0) = 0 then 'no_years'
            else 'ok' end,
        parsed_at = now()
    from (select item_id, title from ebay_compat_items
          where parsed_at is null and title is not null order by item_id limit 500) t
    cross join lateral parse_fitment_title(t.title) f
    where i.item_id = t.item_id
    """
)


def _parse_titles(job: _Job):
    with get_engine().begin() as c:
        c.execute(text("update ebay_compat_items set parsed_status = 'no_title', parsed_at = now() "
                       "where parsed_at is null and title is null"))
    while True:
        with get_engine().begin() as c:
            n = c.execute(_PARSE).rowcount
        if not n:
            break


# ------------------------------------------------------------------ the work
def _run(job_id: int, store: str, seller: str, recheck: bool):
    job = _Job(job_id)
    calls = {"n": 0}
    try:
        _work(job, calls, store, seller, recheck)
    except RateLimited as e:
        job.message = f"Paused: {e} Start it again later and it carries on where it stopped."
        job.save(calls["n"], "paused")
    except FeedbackError as e:
        job.message = str(e)
        job.save(calls["n"], "failed")
    except Exception:
        log.exception("eBay compat import crashed")
        job.message = "Something unexpected went wrong. The details are in the API log."
        job.save(calls["n"], "failed")
    finally:
        try:
            _parse_titles(job)
        except Exception:
            log.exception("eBay compat import: title parsing failed")


def _work(job: _Job, calls: dict, store: str, seller: str, recheck: bool):
    eng = get_engine()
    ids, _total = _seller_listings(seller, _Calls(300))
    job.c["listings_found"] = len(ids)
    if not ids:
        job.message = f"eBay returned no active listings for {seller}."
        job.save(0, "done")
        return

    saved: dict[str, bool] = {}  # item number -> True if it is due for a re-check
    with eng.connect() as c:
        for chunk in _chunks(ids, 2000):
            rows = c.execute(
                text(
                    "select item_id, (checked_at is null or checked_at < now() - make_interval(days => :d)) as stale "
                    "from ebay_compat_items where item_id = any(cast(:ids as text[]))"
                ),
                {"ids": chunk, "d": RECHECK_DAYS},
            ).all()
            for r in rows:
                saved[r[0]] = bool(r[1])
    new_items = [i for i in ids if i not in saved]
    stale_items = [i for i in ids if saved.get(i)] if recheck else []
    todo = new_items + stale_items
    job.c["already_saved"] = len(ids) - len(todo)
    limit = _max_calls()
    over = max(0, len(todo) - limit)
    todo = todo[:limit]
    stale_set = set(stale_items)
    job.c["items_total"] = len(todo)
    if not todo:
        job.message = (f"eBay shows {len(ids):,} active listings and all of them already have compatibility data saved"
                       + (f" and were re-checked in the last {RECHECK_DAYS} days" if recheck else "")
                       + ". Nothing to look up.")
        job.save(0, "done")
        return
    job.message = (f"eBay shows {len(ids):,} active listings; {job.c['already_saved']:,} are skipped. "
                   f"Looking up {len(new_items[:limit]):,} not saved yet"
                   + (f" and re-checking {len(todo) - len(new_items[:limit]):,} saved ones for new vehicles" if recheck else "")
                   + "…")
    job.save(0)

    consecutive = 0
    with Session(eng) as db:
        row = _store_row(db, store)
        expected = _expected_username(row)
        conn = eng.connect()
        try:
            for n, item in enumerate(todo, 1):
                if job.stop_requested():
                    conn.commit()
                    job.message = "Stopped. Everything looked up so far is saved."
                    job.save(calls["n"], "stopped")
                    return
                calls["n"] += 1
                r = _read_listing(db, row.store, expected, item)
                db.rollback()  # the token lookup only reads; don't keep a transaction open for hours
                job.c["items_done"] = n
                if r["status"] == "error":
                    job.fail(item, f"[{r.get('code')}] {r.get('message')}")
                    consecutive += 1
                    if consecutive >= MAX_CONSECUTIVE_ERRORS:
                        conn.commit()
                        hint = " The approval for this store may have expired; check the Connect eBay tab." \
                            if any("approv" in f.lower() or "token" in f.lower() for f in job.failures) else ""
                        job.message = (f"{MAX_CONSECUTIVE_ERRORS} errors in a row, so it stopped to avoid wasting "
                                       f"calls.{hint}")
                        job.save(calls["n"], "failed")
                        return
                elif item in stale_set:
                    consecutive = 0
                    job.c["rechecked"] += 1
                    if r["status"] in ("ok", "no_list", "count_only", "not_found"):
                        added = _merge_saved(conn, item, r)
                        if added:
                            job.c["changed"] += 1
                            job.c["new_rows"] += added
                else:
                    consecutive = 0
                    conn.execute(_UPSERT, {
                        "item": item, "store": row.store, "title": r.get("title") or None, "status": r["status"],
                        "count": r.get("count"),
                        "compat": json.dumps(r["compat"]) if r.get("compat") is not None else None,
                    })
                    key = {"ok": "with_compat"}.get(r["status"], r["status"])
                    job.c[key] += 1
                    job.c["compat_rows"] += len(r.get("compat") or [])
                if n % COMMIT_EVERY == 0:
                    conn.commit()
                    job.save(calls["n"])
                if n < len(todo):
                    time.sleep(DELAY)
            conn.commit()
        finally:
            conn.close()

    job.message = "Finished."
    if over:
        job.message = (f"Reached this run's limit of {limit:,} lookups. {over:,} listings are still to do; "
                       "start it again later and it carries on.")
    job.save(calls["n"], "done")


# --------------------------------------------------------------------- routes
class StartIn(BaseModel):
    store: str = ""
    recheck: bool = False  # also re-read listings that are already saved, adding vehicles that are new


def _job_json(row) -> dict:
    d = dict(row._mapping)
    d["running"] = d["status"] in ("running", "stopping")
    return d


def _saved_totals(db: Session) -> list[dict]:
    rows = db.execute(
        text("select store, status, count(*) as n from ebay_compat_items group by 1, 2 order by 1, 2")
    ).all()
    return [{"store": r.store, "status": r.status, "n": r.n} for r in rows]


@router.post("/start")
def start(body: StartIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    if not configured():
        raise HTTPException(503, "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API.")
    info = _resolve(db, body.store)
    if not info["known"]:
        raise HTTPException(404, f"'{info['store']}' is not in the store list yet. Add it on the Live listings tab first.")
    get_user_token(db, info["store"])  # fails here, with a readable reason, if the store has not been approved
    _expire_stale(db.execute)
    running = db.execute(text("select id from ebay_compat_jobs where status in ('running','stopping') limit 1")).first()
    if running:
        db.commit()
        raise HTTPException(409, "A compatibility import is already running. Wait for it to finish or stop it first.")
    job_id = db.execute(
        text("insert into ebay_compat_jobs (store, mode, started_by, message) values (:s, :m, :u, 'Starting…') returning id"),
        {"s": info["store"], "m": "recheck" if body.recheck else "new_only", "u": user.email or str(user.user_id)},
    ).scalar()
    db.commit()
    threading.Thread(
        target=_run, args=(job_id, info["store"], info["seller"], body.recheck), daemon=True, name=f"ebay-compat-{job_id}"
    ).start()
    log.info("eBay compat import %s started for %s by %s", job_id, info["store"], user.email)
    row = db.execute(text("select * from ebay_compat_jobs where id = :i"), {"i": job_id}).first()
    return _job_json(row)


@router.get("/status")
def status(db: Session = Depends(get_db)):
    _expire_stale(db.execute)
    db.commit()
    row = db.execute(text("select * from ebay_compat_jobs order by id desc limit 1")).first()
    return {"job": _job_json(row) if row else None, "saved": _saved_totals(db)}


@router.post("/stop")
def stop(db: Session = Depends(get_db)):
    db.execute(text("update ebay_compat_jobs set status = 'stopping', updated_at = now() where status = 'running'"))
    db.commit()
    return status(db)
