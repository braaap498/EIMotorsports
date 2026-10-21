"""
Reads the store's eBay feedback numbers (positive %, feedback score) so the About
page can show them as a SNAPSHOT.

eBay is an optional channel here, so nothing on the website ever waits on it:

  * The About page only reads the last saved numbers from the database.
  * A refresh asks eBay for fresh numbers. If that works, the snapshot is replaced.
    If it fails for any reason (eBay down, rate limit, keys missing, no active
    listings, store closed), the old numbers are kept and only the error is noted.

How the numbers are read: eBay's Browse API (the same public, application-token
access the old desktop app used) returns the seller's feedback percentage and score
on every listing it returns, so one tiny search for this seller's listings is enough.
No eBay login (user token) is involved.

Settings, as environment variables on the API (nothing is committed):
    EBAY_CLIENT_ID, EBAY_CLIENT_SECRET   the production keys from developer.ebay.com
    EBAY_SELLER_USERNAME                 optional, defaults to iacarlso_0
"""
import base64
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DEFAULT_SELLER = "iacarlso_0"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
CATEGORY_IDS = "6000"  # Vehicle Parts & Accessories, same default the old app used


class FeedbackError(Exception):
    """A readable reason the refresh did not work. Safe to show in the admin area."""


def seller_username() -> str:
    return (os.environ.get("EBAY_SELLER_USERNAME") or DEFAULT_SELLER).strip()


def configured() -> bool:
    return bool(os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"))


def _open(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        log.warning("eBay %s -> %s %s", req.full_url.split("?")[0], e.code, detail)
        raise FeedbackError(f"eBay answered with an error ({e.code}).")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.warning("eBay unreachable: %s", e)
        raise FeedbackError("Could not reach eBay.")


def _token() -> str:
    if not configured():
        raise FeedbackError("EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API.")
    cred = f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}".encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode(),
        method="POST",
        headers={
            "Authorization": "Basic " + base64.b64encode(cred).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    tok = _open(req).get("access_token")
    if not tok:
        raise FeedbackError("eBay did not return an access token.")
    return tok


def fetch_feedback() -> dict:
    """Ask eBay now. Returns {"percent": float, "score": int}; raises FeedbackError."""
    user = seller_username()
    q = urllib.parse.urlencode(
        {"filter": f"sellers:{{{user}}}", "category_ids": CATEGORY_IDS, "limit": 1}
    )
    req = urllib.request.Request(
        f"{SEARCH_URL}?{q}",
        headers={"Authorization": f"Bearer {_token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
    )
    items = _open(req).get("itemSummaries") or []
    if not items:
        raise FeedbackError(f"eBay returned no active listings for {user}, so there were no numbers to read.")
    seller = items[0].get("seller") or {}
    try:
        percent = float(seller.get("feedbackPercentage"))
        score = int(seller.get("feedbackScore"))
    except (TypeError, ValueError):
        raise FeedbackError("eBay's answer did not include feedback numbers.")
    return {"percent": percent, "score": score}


# ------------------------------------------------------------------ snapshot
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def next_snapshot(old: dict | None) -> dict:
    """The snapshot to save after trying eBay once. Old numbers survive a failed try."""
    snap = dict(old or {})
    snap["last_attempt_at"] = _now()
    try:
        got = fetch_feedback()
    except FeedbackError as e:
        snap["last_error"] = str(e)
        return snap
    except Exception:  # never let anything here reach a visitor
        log.exception("unexpected error reading eBay feedback")
        snap["last_error"] = "Something unexpected went wrong reading eBay."
        return snap
    snap.update(got)
    snap["fetched_at"] = snap["last_attempt_at"]
    snap["last_error"] = None
    return snap


def is_stale(snap: dict | None, hours: float = 24.0) -> bool:
    """True when it has been a while since we last TRIED (not just since we last succeeded,
    so a broken connection is not retried on every page view)."""
    stamp = (snap or {}).get("last_attempt_at")
    if not stamp:
        return True
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - then).total_seconds() > hours * 3600


# one refresh at a time per server process
refresh_lock = threading.Lock()
