#!/usr/bin/env python3
"""
Pull each eBay listing's "compatible vehicles" list into a RAW staging table.

Nothing is interpreted: no vehicles, makes, models or fitment rows are
created. The lists are kept exactly as eBay returned them so they can be
matched against your own year/make/model database later.

    python pull_ebay_compat.py --item 123456789012   # test ONE listing (no database needed)
    python pull_ebay_compat.py                       # SAMPLE: 25 random listings, saves nothing
    python pull_ebay_compat.py --commit              # the real run: saves, resumable

Credentials come from environment variables (never put them in a file you commit):
    EBAY_AUTH_TOKEN   an "Auth'n'Auth" user token for your seller account
                      (developer.ebay.com > Application Keys > Production > User Tokens;
                      valid ~18 months)   -- OR --
    EBAY_OAUTH_TOKEN  an OAuth user access token (expires after ~2 hours)
    EBAY_APP_ID, EBAY_DEV_ID, EBAY_CERT_ID   optional; sent along if all three are set
    DATABASE_URL      the same value the API uses (not needed for --item)

eBay limits how many calls an app may make per day (about 5,000 by default) and
signals it with error 518. The tool stops cleanly when that happens; run it again
the next day and it continues where it left off.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from xml.sax.saxutils import escape

import psycopg
from psycopg.types.json import Jsonb

NS = "{urn:ebay:apis:eBLBaseComponents}"
ENDPOINT = "https://api.ebay.com/ws/api.dll"
COMPAT_LEVEL = "1423"
NOT_FOUND_CODES = {"17"}          # "item cannot be accessed" (ended / deleted / not yours)
RATE_LIMIT_CODE = "518"           # "call usage limit has been reached"
COMMIT_EVERY = 25
MAX_CONSECUTIVE_ERRORS = 3


class RateLimited(Exception):
    pass


@dataclass
class Result:
    status: str                    # ok | count_only | no_list | not_found | error
    count: int | None = None
    compat: list | None = None
    code: str | None = None
    message: str | None = None


# --------------------------------------------------------------------------
# parsing eBay's reply
# --------------------------------------------------------------------------
def _add_attr(attrs, name, value):
    if name in attrs:
        cur = attrs[name]
        attrs[name] = (cur if isinstance(cur, list) else [cur]) + [value]
    else:
        attrs[name] = value


def parse_get_item(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        # eBay's own documentation shows a bare <br> inside CompatibilityNotes. If that ever
        # arrives unescaped the reply is not well-formed XML; escape those tags and retry once.
        try:
            root = ET.fromstring(re.sub(r"<br\s*/?>", "&lt;br&gt;", xml_text, flags=re.I))
        except ET.ParseError as e:
            return Result("error", message=f"unreadable response from eBay: {e}")

    ack = root.findtext(NS + "Ack")
    if ack in ("Failure", "PartialFailure"):
        errors = []
        for e in root.findall(NS + "Errors"):
            if (e.findtext(NS + "SeverityCode") or "Error") != "Error":
                continue
            errors.append((e.findtext(NS + "ErrorCode"),
                           e.findtext(NS + "LongMessage") or e.findtext(NS + "ShortMessage")))
        codes = {c for c, _ in errors}
        if RATE_LIMIT_CODE in codes:
            raise RateLimited()
        code, msg = errors[0] if errors else (None, "eBay reported a failure without details")
        return Result("not_found" if codes & NOT_FOUND_CODES else "error", code=code, message=msg)
    if ack not in ("Success", "Warning"):
        return Result("error", message=f"unexpected reply from eBay (Ack={ack!r})")

    item = root.find(NS + "Item")
    if item is None:
        return Result("error", message="reply had no Item element")
    count_txt = item.findtext(NS + "ItemCompatibilityCount")
    count = int(count_txt) if count_txt and count_txt.strip().isdigit() else None

    compat = []
    for comp in item.findall(f"{NS}ItemCompatibilityList/{NS}Compatibility"):
        attrs = {}
        for nv in comp.findall(NS + "NameValueList"):
            name = nv.findtext(NS + "Name")
            if not name:
                continue
            values = [v.text for v in nv.findall(NS + "Value")]
            _add_attr(attrs, name, values[0] if len(values) == 1 else values)
        compat.append({"attrs": attrs, "notes": comp.findtext(NS + "CompatibilityNotes")})

    if compat:
        return Result("ok", count=count if count is not None else len(compat), compat=compat)
    if count:
        return Result("count_only", count=count)
    return Result("no_list", count=count or 0)


# --------------------------------------------------------------------------
# talking to eBay
# --------------------------------------------------------------------------
def _http_post(url, headers, body, timeout=60):
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


class EbayTrading:
    def __init__(self, *, auth_token=None, oauth_token=None, app_id=None, dev_id=None, cert_id=None,
                 endpoint=ENDPOINT, post=_http_post, retries=3, sleep=time.sleep):
        if not (auth_token or oauth_token):
            raise ValueError("no eBay token")
        self.auth_token, self.oauth_token = auth_token, oauth_token
        self.app_id, self.dev_id, self.cert_id = app_id, dev_id, cert_id
        self.endpoint, self.post, self.retries, self.sleep = endpoint, post, retries, sleep

    def headers(self):
        h = {
            "X-EBAY-API-COMPATIBILITY-LEVEL": COMPAT_LEVEL,
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-SITEID": "0",
            "Content-Type": "text/xml",
        }
        if self.oauth_token:
            h["X-EBAY-API-IAF-TOKEN"] = self.oauth_token
        if self.app_id and self.dev_id and self.cert_id:
            h["X-EBAY-API-APP-NAME"] = self.app_id
            h["X-EBAY-API-DEV-NAME"] = self.dev_id
            h["X-EBAY-API-CERT-NAME"] = self.cert_id
        return h

    def body(self, item_id):
        creds = ""
        if self.auth_token and not self.oauth_token:
            creds = f"<RequesterCredentials><eBayAuthToken>{escape(self.auth_token)}</eBayAuthToken></RequesterCredentials>"
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"{creds}<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>"
            f"<ItemID>{escape(str(item_id))}</ItemID>"
            "<IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>"
            "<DetailLevel>ReturnAll</DetailLevel></GetItemRequest>"
        )

    def get_item_compat(self, item_id):
        last = "no attempt made"
        for attempt in range(self.retries):
            try:
                status, text = self.post(self.endpoint, self.headers(), self.body(item_id))
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = f"network problem: {e}"
            else:
                if status < 500:
                    return parse_get_item(text)
                last = f"eBay server error (HTTP {status})"
            if attempt < self.retries - 1:
                self.sleep(2 ** attempt)
        return Result("error", message=last)


def client_from_env(env=os.environ, **kw):
    if not (env.get("EBAY_AUTH_TOKEN") or env.get("EBAY_OAUTH_TOKEN")):
        sys.exit("No eBay token. Set EBAY_AUTH_TOKEN (or EBAY_OAUTH_TOKEN). See the README, section 6.")
    return EbayTrading(auth_token=env.get("EBAY_AUTH_TOKEN"), oauth_token=env.get("EBAY_OAUTH_TOKEN"),
                       app_id=env.get("EBAY_APP_ID"), dev_id=env.get("EBAY_DEV_ID"),
                       cert_id=env.get("EBAY_CERT_ID"), **kw)


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------
PENDING_SQL = """
    select cl.id, cl.external_id
    from channel_listings cl join sales_channels c on c.id = cl.channel_id
    where lower(c.name) = 'ebay' and cl.external_id ~ '^[0-9]+$'
      and not exists (select 1 from ebay_compat_raw r
                      where r.channel_listing_id = cl.id and r.status <> 'error')
"""


class PullReport:
    def __init__(self):
        self.statuses, self.attr_names, self.makes = Counter(), Counter(), Counter()
        self.compat_rows = 0
        self.calls = 0
        self.stopped = None          # reason string, if the run ended early
        self.example = None
        self.errors = []             # (item, code, message)
        self.notes = []

    def add(self, item_id, r):
        self.calls += 1
        self.statuses[r.status] += 1
        if r.status == "error" and len(self.errors) < 5:
            self.errors.append((item_id, r.code, r.message))
        for c in r.compat or []:
            self.compat_rows += 1
            self.attr_names.update(c["attrs"].keys())
            make = c["attrs"].get("Make")
            if isinstance(make, str):
                self.makes[make.strip().title()] += 1
        if r.status == "ok" and self.example is None:
            self.example = (item_id, r.compat[:3], r.count)

    def render(self):
        out = ["", "=" * 64, "EBAY COMPATIBILITY PULL", "=" * 64]
        out.append(f"calls made: {self.calls}")
        for s in ("ok", "count_only", "no_list", "not_found", "error"):
            if self.statuses[s]:
                out.append(f"  {s:<11}{self.statuses[s]:>7}")
        if self.compat_rows:
            out.append(f"\ncompatibility rows received: {self.compat_rows}")
            out.append("fields eBay uses: " + ", ".join(f"{k} ({v})" for k, v in self.attr_names.most_common(10)))
            out.append("most common makes: " + ", ".join(f"{k} ({v})" for k, v in self.makes.most_common(8)))
        if self.example:
            item, rows, count = self.example
            out.append(f"\nexample - listing {item} ({count} compatible vehicles), first rows as eBay sent them:")
            out.extend(f"    {r['attrs']}" + (f"  notes: {r['notes']}" if r["notes"] else "") for r in rows)
        if self.errors:
            out.append("\nfirst errors:")
            out.extend(f"    {i}: [{c}] {m}" for i, c, m in self.errors)
        out.extend(f"\n{n}" for n in self.notes)
        if self.stopped:
            out.append(f"\nSTOPPED EARLY: {self.stopped}")
        return "\n".join(out)


def _upsert(cur, lid, item_id, r):
    cur.execute(
        """insert into ebay_compat_raw (channel_listing_id, ebay_item_id, status, compat_count, compat_list,
                                        error_code, error_message, fetched_at)
           values (%s,%s,%s,%s,%s,%s,%s, now())
           on conflict (channel_listing_id) do update set
             ebay_item_id = excluded.ebay_item_id, status = excluded.status,
             compat_count = excluded.compat_count, compat_list = excluded.compat_list,
             error_code = excluded.error_code, error_message = excluded.error_message,
             fetched_at = excluded.fetched_at""",
        (lid, item_id, r.status, r.count, Jsonb(r.compat) if r.compat is not None else None, r.code, r.message))


def run_pull(conn, client, *, commit=False, sample=25, max_calls=4500, delay=0.6, sleep=time.sleep):
    rep = PullReport()
    cur = conn.cursor()

    cur.execute("select count(*) from channel_listings cl join sales_channels c on c.id = cl.channel_id "
                "where lower(c.name) = 'ebay'")
    total = cur.fetchone()[0]
    cur.execute(PENDING_SQL.replace("select cl.id, cl.external_id", "select count(*)"))
    pending = cur.fetchone()[0]
    cur.execute("select count(*) from channel_listings cl join sales_channels c on c.id = cl.channel_id "
                "where lower(c.name) = 'ebay' and (cl.external_id is null or cl.external_id !~ '^[0-9]+$')")
    unusable = cur.fetchone()[0]
    rep.notes.append(f"eBay listings on file: {total}. Already fetched: {total - pending - unusable}. "
                     f"Still to fetch: {pending}. Skipped (no usable item number): {unusable}.")

    if commit:
        cur.execute(PENDING_SQL + " order by cl.id limit %s", (max_calls,))
    else:
        cur.execute(PENDING_SQL + " order by random() limit %s", (sample,))
    todo = cur.fetchall()

    consecutive = 0
    try:
        for n, (lid, item_id) in enumerate(todo, 1):
            try:
                r = client.get_item_compat(item_id)
            except RateLimited:
                rep.stopped = ("eBay says the daily call limit has been reached (error 518). "
                               "Everything fetched so far is saved. Run the same command again tomorrow.")
                break
            rep.add(item_id, r)
            if commit:
                _upsert(cur, lid, item_id, r)
                if n % COMMIT_EVERY == 0:
                    conn.commit()
            consecutive = consecutive + 1 if r.status == "error" else 0
            if consecutive >= MAX_CONSECUTIVE_ERRORS:
                hint = ""
                if any("token" in (m or "").lower() for _, _, m in rep.errors):
                    hint = " The errors mention the token: it may be expired or for the wrong account."
                rep.stopped = f"{MAX_CONSECUTIVE_ERRORS} errors in a row, so I stopped to avoid wasting calls.{hint}"
                break
            if n < len(todo):
                sleep(delay)
        else:
            if commit and len(todo) == max_calls and pending > max_calls:
                rep.stopped = (f"reached this run's limit of {max_calls} calls. "
                               f"{pending - max_calls} listings remain; run again later.")
    finally:
        if commit:
            conn.commit()
    return rep


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--item", help="test ONE eBay item number; prints what eBay returns, no database needed")
    ap.add_argument("--commit", action="store_true", help="the real run: save results (default is a 25-listing sample)")
    ap.add_argument("--sample", type=int, default=25, help="listings to try in sample mode (default 25)")
    ap.add_argument("--max-calls", type=int, default=4500,
                    help="most calls this run (default 4500, under eBay's usual 5,000/day)")
    ap.add_argument("--delay", type=float, default=0.6, help="seconds between calls (default 0.6)")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = ap.parse_args(argv)

    client = client_from_env()
    if args.item:
        r = client.get_item_compat(args.item.strip())
        rep = PullReport()
        rep.add(args.item, r)
        print(rep.render())
        if r.status == "error":
            sys.exit(f"eBay returned an error: [{r.code}] {r.message}")
        return
    if not args.database_url:
        sys.exit("No database. Set DATABASE_URL (or pass --database-url).")

    url = args.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    conn = psycopg.connect(url, prepare_threshold=None)
    try:
        rep = run_pull(conn, client, commit=args.commit, sample=args.sample,
                       max_calls=args.max_calls, delay=args.delay)
    except KeyboardInterrupt:
        conn.commit() if args.commit else conn.rollback()
        sys.exit("\nInterrupted. Anything already fetched is saved; run again to continue.")
    print(rep.render())
    if not args.commit:
        print("\nSAMPLE ONLY: nothing was saved. If this looks right, run again with --commit.")
    conn.close()


if __name__ == "__main__":
    main()
