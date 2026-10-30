#!/usr/bin/env python3
"""
FULL SCAN: check eBay for new photos on ALL listings, including ones that already have photos in storage.

For each product with an eBay item ID this asks eBay (Browse API, read-only)
for the listing's current photos and compares them with the source_url values
already in product_photos. Photos are matched by eBay's image id, not by the
raw URL, so the same photo at a different size (s-l500 vs s-l1600) is NOT new.

Exit codes (so a wrapper script can decide whether to keep going):
    0   nothing new on eBay  -> stop here
    10  new photos found     -> (inserted into product_photos when --apply)
    1   error

SAFE BY DEFAULT: without --apply nothing is written to the database. New
photos are always listed in new_photos.csv.

With --apply, each new photo becomes a product_photos row with source_url set
and storage_path empty, which is exactly what download_product_images.py
looks for. Existing rows are never changed or deleted. Photos eBay no longer
shows are left alone.

Setup (PowerShell, from the backend folder):
    $env:DATABASE_URL="postgresql://..."
    The eBay keys are read from the backend .env (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET,
    the same ones the old app used). If they are not there, set them in PowerShell:
    $env:EBAY_CLIENT_ID="..."; $env:EBAY_CLIENT_SECRET="..."

Usage:
    python scripts\\check_all_ebay_photos_full_scan.py --limit 25          (small trial)
    python scripts\\check_all_ebay_photos_full_scan.py                     (full check, report only)
    python scripts\\check_all_ebay_photos_full_scan.py --apply             (record new photos)

eBay limits how many lookups an app can make per day (commonly 5,000 for the
Browse API; check your developer dashboard). So each run checks at most
--max-calls listings, never-checked ones first and then the longest-ago
checked, and remembers progress in --state. With 10,000+ listings, run it
again the next day and it carries on where it stopped.

Options:
    --apply       write new photo rows (default: report only)
    --limit N     only look at N listings (trial run)
    --max-calls N most lookups this run (default 4500)
    --state FILE  progress file (default ebay_photo_check_all_state.json)
    --report FILE CSV of new photos (default new_photos_all.csv)
"""
import argparse
import base64
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

API = "https://api.ebay.com"
EXIT_NONE, EXIT_NEW, EXIT_ERROR = 0, 10, 1

# Listings that were merged into a product live in channel_listings. If you
# want those checked too, set this to a query returning exactly two columns:
# (product_id, ebay_item_id). Left as None, only products.ebay_item_id is used.
CHANNEL_LISTINGS_SQL = None
# e.g. "select product_id, external_id from channel_listings where channel = 'ebay'"


class RateLimited(Exception):
    pass


def db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        sys.exit("Set DATABASE_URL first (your Supabase Postgres connection string).")
    return url.replace("postgres://", "postgresql://", 1)


def env_or_dotenv(name: str) -> str:
    """Environment variable first, then a NAME=value line in .env (current or parent folder)."""
    v = os.environ.get(name, "")
    if v:
        return v
    for path in (".env", os.path.join("..", ".env")):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, val = line.partition("=")
                        if k.strip().upper() == name:
                            return val.strip().strip("\"'")
        except OSError:
            continue
    return ""


def photo_key(url: str) -> str:
    """Identify a photo by eBay's image id so different sizes compare equal."""
    m = re.search(r"/images/g/([^/]+)/", url)
    if m:
        return "g:" + m.group(1)
    m = re.search(r"/z/([^/]+)/", url)
    if m:
        return "z:" + m.group(1)
    u = re.sub(r"^https?://", "", url.split("?")[0].strip().lower())
    return "u:" + re.sub(r"/s-l\d+\.", "/s-l.", u)


def clean_item_id(value) -> str:
    v = re.sub(r"^EBAY-", "", str(value or "").strip(), flags=re.I)
    return v if v.isdigit() else ""


class Ebay:
    def __init__(self, client_id: str, client_secret: str):
        self.basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self.token, self.expires = None, 0.0

    def _token(self) -> str:
        if self.token and time.time() < self.expires - 120:
            return self.token
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"}
        ).encode()
        req = urllib.request.Request(
            f"{API}/identity/v1/oauth2/token",
            data=body,
            headers={"Authorization": f"Basic {self.basic}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            sys.exit(f"eBay sign-in failed (HTTP {e.code}). Check EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are the PRODUCTION keys.")
        self.token = d["access_token"]
        self.expires = time.time() + int(d.get("expires_in", 7200))
        return self.token

    def photos(self, item_id: str):
        """Return list of photo URLs, or None if the listing can't be read
        (ended, removed, or a multi-variation listing)."""
        url = f"{API}/buy/browse/v1/item/get_item_by_legacy_id?legacy_item_id={item_id}"
        last, limited = None, False
        for attempt in range(5):
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {self._token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    d = json.load(r)
                urls = [(d.get("image") or {}).get("imageUrl")]
                urls += [(a or {}).get("imageUrl") for a in d.get("additionalImages") or []]
                seen, out = set(), []
                for u in urls:
                    if u and photo_key(u) not in seen:
                        seen.add(photo_key(u))
                        out.append(u)
                return out
            except urllib.error.HTTPError as e:
                if e.code in (400, 404):  # ended, removed, or variation listing
                    return None
                if e.code == 401:
                    self.token = None
                limited = e.code == 429
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if limited and retry_after and retry_after.isdigit():
                    delay = min(int(retry_after), 60)
                else:
                    delay = min(2 ** attempt, 30)
                last = e
            except Exception as e:
                limited, delay, last = False, 1.5 * (attempt + 1), e
            time.sleep(delay)
        if limited:
            raise RateLimited()  # still limited after backing off
        raise RuntimeError(f"item {item_id}: {last}")


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-calls", type=int, default=4500)
    ap.add_argument("--state", default="ebay_photo_check_all_state.json")
    ap.add_argument("--report", default="new_photos_all.csv")
    args = ap.parse_args()

    cid, secret = env_or_dotenv("EBAY_CLIENT_ID"), env_or_dotenv("EBAY_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not found (set them in PowerShell or in the backend .env).")

    engine = create_engine(db_url())
    with engine.connect() as conn:
        pairs = [(r[0], clean_item_id(r[1])) for r in conn.execute(
            text("select id, ebay_item_id from products where ebay_item_id is not null and ebay_item_id <> ''"))]
        if CHANNEL_LISTINGS_SQL:
            pairs += [(r[0], clean_item_id(r[1])) for r in conn.execute(text(CHANNEL_LISTINGS_SQL))]
        existing = {}
        for pid, url in conn.execute(text("select product_id, source_url from product_photos where source_url is not null")):
            existing.setdefault(pid, set()).add(photo_key(url))
        extra = {r[0]: (r[1], r[2]) for r in conn.execute(text(
            "select product_id, coalesce(max(sort_order), 0), coalesce(bool_or(is_primary), false) "
            "from product_photos group by product_id"))}

    items = {}
    for pid, iid in pairs:
        if iid and iid not in items:
            items[iid] = pid
    print(f"{len(items)} eBay listings to consider; {sum(len(v) for v in existing.values())} photos already recorded.")

    state = load_state(args.state)
    order = sorted(items, key=lambda i: state.get(i, ""))  # never-checked ("") first, then oldest
    cap = args.max_calls if not args.limit else min(args.limit, args.max_calls)
    batch = order[:cap]
    never = sum(1 for i in items if i not in state)
    print(f"Checking {len(batch)} listing(s) this run ({never} never checked so far).")

    ebay = Ebay(cid, secret)
    found, unreadable, errors, checked = [], 0, 0, 0
    try:
        for n, iid in enumerate(batch, 1):
            pid = items[iid]
            try:
                urls = ebay.photos(iid)
            except RateLimited:
                print("\neBay is still rate-limiting after several retries (most likely the daily limit). Stopping; run again later to continue.")
                break
            except Exception as e:
                errors += 1
                print(f"  error on {iid}: {e}")
                continue
            state[iid] = datetime.now(timezone.utc).isoformat()
            checked += 1
            if urls is None:
                unreadable += 1
            else:
                have = existing.get(pid, set())
                for u in urls:
                    if photo_key(u) not in have:
                        found.append((pid, iid, u))
                        have = have | {photo_key(u)}
                        existing[pid] = have
            if n % 100 == 0:
                save_state(args.state, state)
                print(f"  {n}/{len(batch)} checked, {len(found)} new photos so far")
            time.sleep(0.1)
    finally:
        save_state(args.state, state)

    left = sum(1 for i in items if i not in state)
    print(f"\nChecked {checked}. Ended/variation/unreadable: {unreadable}. Errors: {errors}. Never checked yet: {left}.")

    if errors and not checked:
        print("Nothing could be checked - see errors above.")
        return EXIT_ERROR
    if not found:
        print("No new photos on eBay for the listings checked. Nothing to do.")
        return EXIT_NONE

    with open(args.report, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "ebay_item_id", "source_url"])
        w.writerows(found)
    print(f"{len(found)} new photo(s) on {len({f[0] for f in found})} product(s). Listed in {args.report}.")

    if not args.apply:
        print("Dry run - nothing was written. Add --apply to record them.")
        return EXIT_NEW

    rows, counters = [], {}
    for pid, _iid, url in found:
        top, has_primary = extra.get(pid, (0, False))
        n = counters.get(pid, 0) + 1
        counters[pid] = n
        rows.append({"pid": pid, "url": url, "prim": (not has_primary and n == 1), "so": top + n})
    with engine.connect() as conn:
        conn.execute(text(
            "insert into product_photos (product_id, source_url, is_primary, sort_order) "
            "values (:pid, :url, :prim, :so)"), rows)
        conn.commit()
    print(f"Recorded {len(rows)} new photo row(s). Next: download_product_images.py picks them up.")
    return EXIT_NEW


if __name__ == "__main__":
    sys.exit(main())
