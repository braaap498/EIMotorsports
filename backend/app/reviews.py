"""
Customer feedback on the About page.

Two kinds of feedback live here:

  * eBay feedback comments: ALL of them are pulled from eBay by app/ebay_comments.py (first run
    imports everything, then about once a day). An admin can also type one in by hand.
  * Website reviews: written by customers on the new site, held until an admin approves them.

  GET  /store/ebay-feedback         public   pages of eBay comments (offset, limit)
  GET  /store/reviews               public   pages of APPROVED website reviews + average
  POST /store/reviews               public   a customer sends a review (goes to "pending")

  GET    /admin/reviews?status=     admin    website reviews + counts (pending, approved, hidden)
  POST   /admin/reviews/{id}/status admin    approve / hide / put back to pending
  DELETE /admin/reviews/{id}        admin    delete a review for good
  GET    /admin/reviews/ebay        admin    sync status + the comments typed in by hand
  POST   /admin/reviews/ebay/sync   admin    pull the comments from eBay now
  POST   /admin/reviews/ebay        admin    add a comment by hand
  DELETE /admin/reviews/ebay/{id}   admin    remove a hand-typed comment (comments from eBay stay)

Spam protection for the public form (reviews are also never shown before approval):
  * a hidden "website" field that real people leave empty; bots that fill it are ignored
  * at most 5 reviews per visitor per 24 hours (only a salted hash of the IP is stored)

Settings (optional environment variables on the API):
    REVIEWS_AUTO_APPROVE=true   show new reviews immediately instead of waiting for approval
    REVIEW_HASH_SALT            any random text; makes the stored IP hashes unguessable

Review text is plain text. The storefront shows it as text, never as HTML.
"""
import hashlib
import logging
import os
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import ebay_comments
from app.auth import require_admin
from app.database import get_db

log = logging.getLogger(__name__)

MAX_PER_DAY = 5
PERIODS = ("Past month", "Past 6 months", "Past year")

admin_router = APIRouter(prefix="/reviews", tags=["admin-reviews"], dependencies=[Depends(require_admin)])
store_router = APIRouter(prefix="/store", tags=["reviews"])


def _auto_approve() -> bool:
    return os.environ.get("REVIEWS_AUTO_APPROVE", "").strip().lower() in ("1", "true", "yes")


def _ip_hash(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else ""
    if not ip:
        ip = request.client.host if request.client else "unknown"
    salt = os.environ.get("REVIEW_HASH_SALT", "ei-reviews")
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


# ------------------------------------------------------------------ public
# "Past month" etc. is worked out from the date eBay gave us, so it stays correct over time.
# Comments typed in by hand have no date and keep the label they were given.
_PERIOD_SQL = (
    "case when entered_at is null then period "
    "when entered_at > now() - interval '30 days' then 'Past month' "
    "when entered_at > now() - interval '180 days' then 'Past 6 months' "
    "when entered_at > now() - interval '365 days' then 'Past year' "
    "else 'Over a year ago' end"
)


# "Most relevant" first: a longer, more detailed comment scores higher (up to 150 characters),
# and newer ones score higher (fading to nothing over 2 years). The rating itself is not a factor.
# Comments typed in by hand have no date, so they get a middle recency score.
_RELEVANCE_SQL = (
    "(0.6 * least(char_length(comment), 150) / 150.0 "
    "+ 0.4 * case when entered_at is null then 0.5 "
    "else greatest(0.0, 1.0 - extract(epoch from (now() - entered_at)) / 63072000.0) end)"
)


@store_router.get("/ebay-feedback")
def public_ebay_feedback(
    background: BackgroundTasks,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    total = db.execute(text("select count(*) from ebay_feedback_items")).scalar() or 0
    rows = db.execute(
        text(
            f"select id, rating, comment, {_PERIOD_SQL} as period from ebay_feedback_items "
            f"order by {_RELEVANCE_SQL} desc, id desc limit :l offset :o"
        ),
        {"l": limit, "o": offset},
    ).all()
    # If the saved comments are old, look for new ones AFTER this reply is sent.
    if offset == 0 and ebay_comments.configured() and ebay_comments.is_stale(ebay_comments.load_status(db)):
        background.add_task(ebay_comments.refresh_in_background)
    return {
        "total": total,
        "items": [{"id": r.id, "rating": r.rating, "comment": r.comment, "period": r.period} for r in rows],
    }


@store_router.get("/reviews")
def public_reviews(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    stats = db.execute(
        text("select count(*) as n, round(avg(rating), 1) as avg from site_reviews where status = 'approved'")
    ).first()
    rows = db.execute(
        text(
            "select id, display_name, rating, title, body, created_at from site_reviews "
            "where status = 'approved' order by created_at desc, id desc limit :l offset :o"
        ),
        {"l": limit, "o": offset},
    ).all()
    return {
        "total": stats.n if stats else 0,
        "average": float(stats.avg) if stats and stats.avg is not None else None,
        "items": [
            {
                "id": r.id,
                "display_name": r.display_name,
                "rating": r.rating,
                "title": r.title,
                "body": r.body,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


class ReviewIn(BaseModel):
    display_name: str = Field(max_length=60)
    rating: int = Field(ge=1, le=5)
    title: Optional[str] = Field(default=None, max_length=100)
    body: str = Field(max_length=2000)
    website: str = Field(default="", max_length=200)  # honeypot: real visitors never fill this in

    @field_validator("display_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 2:
            raise ValueError("Please enter the name you want shown with your review.")
        return v

    @field_validator("title")
    @classmethod
    def _title(cls, v: Optional[str]) -> Optional[str]:
        v = " ".join((v or "").split())
        return v or None

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 10:
            raise ValueError("Tell us a bit more. A sentence or two is plenty.")
        return v


@store_router.post("/reviews")
def submit_review(body: ReviewIn, request: Request, db: Session = Depends(get_db)):
    status = "approved" if _auto_approve() else "pending"
    if body.website.strip():
        # A bot filled the hidden field. Pretend it worked and save nothing.
        return {"status": status}

    h = _ip_hash(request)
    recent = db.execute(
        text("select count(*) from site_reviews where ip_hash = :h and created_at > now() - interval '24 hours'"),
        {"h": h},
    ).scalar() or 0
    if recent >= MAX_PER_DAY:
        raise HTTPException(429, "You have sent a few reviews today already. Please try again tomorrow.")

    db.execute(
        text(
            "insert into site_reviews (display_name, rating, title, body, status, ip_hash, approved_at) "
            "values (:n, :r, :t, :b, :s, :h, case when :s = 'approved' then now() else null end)"
        ),
        {"n": body.display_name, "r": body.rating, "t": body.title, "b": body.body, "s": status, "h": h},
    )
    db.commit()
    return {"status": status}


# ------------------------------------------------------------------ admin: eBay comments
# (declared before the "/{rid}" routes so "ebay" is never read as a review id)
class EbayIn(BaseModel):
    rating: Literal["positive", "neutral", "negative"] = "positive"
    comment: str = Field(max_length=1000)
    period: str = "Past month"

    @field_validator("comment")
    @classmethod
    def _comment(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Paste the comment exactly as it appears on eBay.")
        return v

    @field_validator("period")
    @classmethod
    def _period(cls, v: str) -> str:
        if v not in PERIODS:
            raise ValueError("Pick one of: " + ", ".join(PERIODS))
        return v


def _ebay_admin(db: Session) -> dict:
    """Sync status plus the hand-typed comments. (Synced ones are many; they are not listed here.)"""
    rows = db.execute(
        text(
            "select id, rating, comment, period from ebay_feedback_items "
            "where ebay_feedback_id is null order by sort_order, id desc limit 200"
        )
    ).all()
    synced = db.execute(
        text("select count(*) from ebay_feedback_items where ebay_feedback_id is not null")
    ).scalar() or 0
    snap = ebay_comments.load_status(db)
    return {
        "items": [{"id": r.id, "rating": r.rating, "comment": r.comment, "period": r.period} for r in rows],
        "synced_count": synced,
        "status": {
            "configured": ebay_comments.configured(),
            "seller": ebay_comments.seller_username(),
            "last_attempt_at": snap.get("last_attempt_at"),
            "last_success_at": snap.get("last_success_at"),
            "last_added": snap.get("last_added"),
            "last_error": snap.get("last_error"),
        },
    }


@admin_router.get("/ebay")
def admin_ebay_list(db: Session = Depends(get_db)):
    return _ebay_admin(db)


@admin_router.post("/ebay/sync")
def admin_ebay_sync(db: Session = Depends(get_db)):
    """Ask eBay now. A failure is reported in status.last_error, not raised."""
    ebay_comments.run(db)
    return _ebay_admin(db)


@admin_router.post("/ebay")
def admin_ebay_add(body: EbayIn, db: Session = Depends(get_db)):
    # New comments go to the top of the list (lowest sort_order first).
    db.execute(
        text(
            "insert into ebay_feedback_items (rating, comment, period, sort_order) "
            "values (:r, :c, :p, (select coalesce(min(sort_order), 1) - 1 from ebay_feedback_items))"
        ),
        {"r": body.rating, "c": body.comment, "p": body.period},
    )
    db.commit()
    return _ebay_admin(db)


@admin_router.delete("/ebay/{eid}")
def admin_ebay_delete(eid: int, db: Session = Depends(get_db)):
    # Only hand-typed comments can be removed; comments that came from eBay mirror eBay.
    db.execute(text("delete from ebay_feedback_items where id = :i and ebay_feedback_id is null"), {"i": eid})
    db.commit()
    return _ebay_admin(db)


# ------------------------------------------------------------------ admin: website reviews
def _counts(db: Session) -> dict:
    rows = db.execute(text("select status, count(*) as n from site_reviews group by status")).all()
    out = {"pending": 0, "approved": 0, "hidden": 0}
    for r in rows:
        out[r.status] = r.n
    return out


@admin_router.get("")
def admin_list(
    status: Literal["pending", "approved", "hidden", "all"] = "pending",
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
):
    where = "" if status == "all" else "where status = :s"
    rows = db.execute(
        text(
            "select id, display_name, rating, title, body, status, created_at from site_reviews "
            f"{where} order by created_at desc, id desc limit :l"
        ),
        {"s": status, "l": limit},
    ).all()
    return {
        "counts": _counts(db),
        "items": [
            {
                "id": r.id,
                "display_name": r.display_name,
                "rating": r.rating,
                "title": r.title,
                "body": r.body,
                "status": r.status,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


class StatusIn(BaseModel):
    status: Literal["pending", "approved", "hidden"]


@admin_router.post("/{rid}/status")
def admin_set_status(rid: int, body: StatusIn, db: Session = Depends(get_db)):
    done = db.execute(
        text(
            "update site_reviews set status = :s, "
            "approved_at = case when :ok then coalesce(approved_at, now()) else approved_at end "
            "where id = :i returning id"
        ),
        {"s": body.status, "ok": body.status == "approved", "i": rid},
    ).first()
    if not done:
        raise HTTPException(404, "That review no longer exists.")
    db.commit()
    return {"counts": _counts(db)}


@admin_router.delete("/{rid}")
def admin_delete(rid: int, db: Session = Depends(get_db)):
    db.execute(text("delete from site_reviews where id = :i"), {"i": rid})
    db.commit()
    return {"counts": _counts(db)}
