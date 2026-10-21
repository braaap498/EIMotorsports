"""
Reads ALL of the store's eBay feedback comments (not just the percentage) so the About page
can list them in its scrollable "From eBay" tab.

It uses eBay's Feedback API (GET /commerce/feedback/v1/feedback, FEEDBACK_RECEIVED) with the SAME
app keys as app/ebay_feedback.py (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET) and a client-credentials
token with the read-only feedback scope. eBay documents that flow as "access another user's
feedback without logging in", so no eBay sign-in is needed. Buyer names are masked by eBay and
are never stored here.

Needs, on developer.ebay.com, that your app keys list this scope under Application Keys > OAuth
Scopes:  https://api.ebay.com/oauth/api_scope/commerce.feedback.readonly
(Set EBAY_FEEDBACK_SCOPE on the API only if eBay tells you to use a different one.)

Like the other eBay code here, nothing on the website ever waits on eBay:

  * visitors only read the comments saved in our own database
  * the first run imports everything; later runs only look at the newest page(s) until they hit
    comments we already have
  * a failed run keeps what is saved and just records the reason (shown in the admin area)
  * runs automatically about once a day when someone views the About page, or on demand from the
    admin "Sync from eBay now" button

Comments typed in by hand earlier are removed automatically once the same comment arrives from
eBay (matched on the start of the text, because eBay's own page cuts long comments short).
"""
import base64
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import ebay_feedback
from app.database import _session_factory
from app.ebay_feedback import FeedbackError, configured, is_stale, seller_username

log = logging.getLogger(__name__)

SLUG = "ebay-comments"  # status is kept on its own site_pages row, like the About snapshot
FEEDBACK_URL = "https://api.ebay.com/commerce/feedback/v1/feedback"
DEFAULT_SCOPE = "https://api.ebay.com/oauth/api_scope/commerce.feedback.readonly"
PAGE = 200  # the most eBay allows per request
MAX_PAGES = 100  # safety stop: 20,000 comments

run_lock = threading.Lock()  # one sync at a time per server process

__all__ = ["configured", "seller_username", "is_stale", "load_status", "run", "refresh_in_background"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ talking to eBay
def _token() -> str:
    if not configured():
        raise FeedbackError("EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API.")
    scope = os.environ.get("EBAY_FEEDBACK_SCOPE") or DEFAULT_SCOPE
    cred = f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}".encode()
    req = urllib.request.Request(
        ebay_feedback.TOKEN_URL,
        data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": scope}).encode(),
        method="POST",
        headers={
            "Authorization": "Basic " + base64.b64encode(cred).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        tok = ebay_feedback._open(req).get("access_token")
    except FeedbackError as e:
        raise FeedbackError(
            f"{e} Your eBay app keys may not include the feedback scope ({scope}). "
            "Check Application Keys, then OAuth Scopes, on developer.ebay.com."
        )
    if not tok:
        raise FeedbackError("eBay did not return an access token.")
    return tok


def _page(token: str, user: str, offset: int) -> dict:
    q = urllib.parse.urlencode(
        {"user_id": user, "feedback_type": "FEEDBACK_RECEIVED", "sort": "TIME", "limit": PAGE, "offset": offset}
    )
    req = urllib.request.Request(
        f"{FEEDBACK_URL}?{q}",
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
    )
    try:
        return ebay_feedback._open(req)
    except FeedbackError as e:
        raise FeedbackError(f"{e} The eBay Feedback API may not be enabled for your app keys yet.")


def _when(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _entry(e: dict) -> Optional[dict]:
    """One eBay feedback entry as our row, or None if it has no usable comment."""
    fid = e.get("feedbackId")
    c = e.get("feedbackComment") or {}
    comment = (c.get("commentText") or "").strip()
    if not fid or not comment or comment == "--" or c.get("commentTextRemovedPerPolicy"):
        return None  # rating-only entries and comments eBay removed are not shown
    rating = str(e.get("commentType") or "POSITIVE").lower()
    if rating not in ("positive", "neutral", "negative"):
        rating = "positive"
    return {"fid": str(fid), "rating": rating, "comment": comment[:1000], "when": _when(e.get("feedbackEnteredDate"))}


# ------------------------------------------------------------------ the sync
def sync(db: Session, full: bool) -> int:
    """Save comments we do not have yet. Returns how many were added; raises FeedbackError."""
    token = _token()
    user = seller_username()
    existing = {
        r[0]
        for r in db.execute(text("select ebay_feedback_id from ebay_feedback_items where ebay_feedback_id is not null"))
    }
    added = 0
    offset = 0
    for _ in range(MAX_PAGES):
        data = _page(token, user, offset)
        entries = data.get("feedbackEntries") or []
        if not entries:
            break
        fresh = 0
        for raw in entries:
            row = _entry(raw)
            if not row or row["fid"] in existing:
                continue
            db.execute(
                text(
                    "insert into ebay_feedback_items (rating, comment, period, sort_order, ebay_feedback_id, entered_at) "
                    "values (:r, :c, 'Past month', 0, :f, :w) on conflict (ebay_feedback_id) do nothing"
                ),
                {"r": row["rating"], "c": row["comment"], "f": row["fid"], "w": row["when"]},
            )
            existing.add(row["fid"])
            fresh += 1
        added += fresh
        db.commit()

        offset += len(entries)
        # Newest first: once a whole page holds nothing new, we are caught up (unless doing a full import).
        if not full and fresh == 0:
            break
        pg = data.get("pagination") or {}
        total = pg.get("total")
        if pg.get("next"):
            more = True
        elif total is not None:
            more = offset < total
        else:
            more = len(entries) >= PAGE
        if not more:
            break

    if existing:
        # Drop hand-typed copies of comments that have now arrived from eBay.
        db.execute(
            text(
                "delete from ebay_feedback_items m where m.ebay_feedback_id is null and exists ("
                "select 1 from ebay_feedback_items s where s.ebay_feedback_id is not null "
                "and left(s.comment, char_length(m.comment)) = m.comment)"
            )
        )
        db.commit()
    return added


# ------------------------------------------------------------------ status (kept on a site_pages row)
def load_status(db: Session) -> dict:
    row = db.execute(text("select ebay_snapshot from site_pages where slug = :s"), {"s": SLUG}).first()
    snap = row.ebay_snapshot if row else None
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except ValueError:
            snap = None
    return snap if isinstance(snap, dict) else {}


def _save_status(db: Session, snap: dict) -> None:
    db.execute(
        text(
            "insert into site_pages (slug, title, body, ebay_snapshot, updated_at) "
            "values (:s, 'eBay comments', '', cast(:j as jsonb), now()) "
            "on conflict (slug) do update set ebay_snapshot = excluded.ebay_snapshot"
        ),
        {"s": SLUG, "j": json.dumps(snap)},
    )
    db.commit()


def run(db: Session) -> dict:
    """Try one sync and record the outcome. Never raises. Returns the saved status."""
    if not run_lock.acquire(blocking=False):
        return load_status(db)  # another sync is already running
    try:
        snap = load_status(db)
        snap["last_attempt_at"] = _now()
        try:
            has_any = db.execute(
                text("select 1 from ebay_feedback_items where ebay_feedback_id is not null limit 1")
            ).first()
            snap["last_added"] = sync(db, full=not has_any)
            snap["last_success_at"] = snap["last_attempt_at"]
            snap["last_error"] = None
        except FeedbackError as e:
            db.rollback()
            snap["last_error"] = str(e)
        except Exception:  # never let anything here reach a visitor
            log.exception("unexpected error syncing eBay comments")
            db.rollback()
            snap["last_error"] = "Something unexpected went wrong reading eBay."
        _save_status(db, snap)
        return snap
    finally:
        run_lock.release()


def refresh_in_background() -> None:
    db = _session_factory()()
    try:
        run(db)
    except Exception:
        log.exception("background eBay comment sync failed")
    finally:
        db.close()
