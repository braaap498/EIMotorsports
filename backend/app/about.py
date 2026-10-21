"""
The About page: its text is stored in the database and edited from the admin area.

  GET /store/about   public        what the storefront's /about page shows
  GET /admin/about   admin/owner   the same, for the editor
  PUT /admin/about   admin/owner   save changes (they show on the site straight away)

The page text is plain text. The storefront turns it into headings, paragraphs and
bullets itself (a line starting with "## " is a heading, "- " is a bullet), so no
HTML from the editor is ever put on the page.
"""
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db

SLUG = "about"
DEFAULT_TITLE = "About us"

admin_router = APIRouter(prefix="/about", tags=["admin-about"], dependencies=[Depends(require_admin)])
store_router = APIRouter(prefix="/store", tags=["about"])


def clean_ebay_url(v: Optional[str]) -> Optional[str]:
    """Empty means 'none'. Anything else must be an https link on eBay."""
    v = (v or "").strip()
    if not v:
        return None
    u = urlparse(v)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not (host == "ebay.com" or host.endswith(".ebay.com")):
        raise ValueError("Enter a full eBay link that starts with https:// (for example https://www.ebay.com/...)")
    return v


class AboutIn(BaseModel):
    title: str = Field(default=DEFAULT_TITLE, max_length=120)
    body: str = Field(default="", max_length=20000)
    ebay_feedback_url: Optional[str] = Field(default=None, max_length=500)
    ebay_store_url: Optional[str] = Field(default=None, max_length=500)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        return v.strip() or DEFAULT_TITLE

    @field_validator("ebay_feedback_url", "ebay_store_url")
    @classmethod
    def _urls(cls, v: Optional[str]) -> Optional[str]:
        return clean_ebay_url(v)


def _load(db: Session) -> dict:
    row = db.execute(
        text("select title, body, ebay_feedback_url, ebay_store_url, updated_at from site_pages where slug = :s"),
        {"s": SLUG},
    ).first()
    if not row:
        return {"title": DEFAULT_TITLE, "body": "", "ebay_feedback_url": None, "ebay_store_url": None, "updated_at": None}
    return {
        "title": row.title or DEFAULT_TITLE,
        "body": row.body or "",
        "ebay_feedback_url": row.ebay_feedback_url,
        "ebay_store_url": row.ebay_store_url,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@store_router.get("/about")
def public_about(db: Session = Depends(get_db)):
    return _load(db)


@admin_router.get("")
def admin_get_about(db: Session = Depends(get_db)):
    return _load(db)


@admin_router.put("")
def admin_save_about(body: AboutIn, db: Session = Depends(get_db)):
    db.execute(
        text(
            "insert into site_pages (slug, title, body, ebay_feedback_url, ebay_store_url, updated_at) "
            "values (:s, :t, :b, :f, :st, now()) "
            "on conflict (slug) do update set title = excluded.title, body = excluded.body, "
            "ebay_feedback_url = excluded.ebay_feedback_url, ebay_store_url = excluded.ebay_store_url, "
            "updated_at = now()"
        ),
        {"s": SLUG, "t": body.title, "b": body.body, "f": body.ebay_feedback_url, "st": body.ebay_store_url},
    )
    db.commit()
    return _load(db)
