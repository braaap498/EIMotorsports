"""
The Help pages (Shipping, Returns, Refunds and Privacy): their text is stored in the database and edited
from the admin area, the same way the About page is.

  GET /store/help         public        both pages, for the tabs on the storefront's /help page
  GET /admin/help         admin/owner   the same, for the editor
  PUT /admin/help/{key}   admin/owner   save one page (it shows on the site straight away)

`key` is "shipping", "returns", "refunds" or "privacy". Each page is one row in the
site_pages table (slugs "help-shipping", "help-returns", "help-refunds", "help-privacy"),
so no new table or migration is needed. To add another tab later, add it to PAGES below.

Until a page is saved for the first time it shows a starter text:
  * Shipping: built from app/shipping.py, so the methods and prices always match checkout.
  * Returns, Refunds, Privacy: empty (the storefront shows "check back soon"), because these
    are the shop's own terms. Write them in the admin editor.
Once a page has been saved, what was saved is what shows, even if it was cleared on purpose.

The page text is plain text. The storefront turns it into headings, paragraphs and
bullets itself (a line starting with "## " is a heading, "- " is a bullet), so no
HTML from the editor is ever put on the page.
"""
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db
from app.shipping import RATES

admin_router = APIRouter(prefix="/help", tags=["admin-help"], dependencies=[Depends(require_admin)])
store_router = APIRouter(prefix="/store", tags=["help"])


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _default_shipping() -> str:
    """Starter shipping text, built from the same price list checkout uses."""
    methods = "\n".join(
        f"- {label}: {'Free' if cents == 0 else _money(cents) + ' per item'}" for _code, label, cents in RATES
    )
    return (
        "## Where we ship\n"
        "We currently ship within the United States only.\n\n"
        "## Shipping methods and prices\n"
        "You choose a shipping method at checkout, and the price is shown before you pay. "
        "Shipping is priced per item.\n"
        f"{methods}"
    )


# key -> what the page is. Order here is the order of the tabs on the site.
# `label` is the (fixed) tab name; `title` is the editable heading shown above the text.
PAGES: dict[str, dict] = {
    "shipping": {
        "slug": "help-shipping",
        "label": "Shipping",
        "title": "Shipping information",
        "default_body": _default_shipping,
    },
    "returns": {
        "slug": "help-returns",
        "label": "Returns",
        "title": "Returns",
        "default_body": lambda: "",
    },
    "refunds": {
        "slug": "help-refunds",
        "label": "Refunds",
        "title": "Refunds",
        "default_body": lambda: "",
    },
    "privacy": {
        "slug": "help-privacy",
        "label": "Privacy",
        "title": "Privacy policy",
        "default_body": lambda: "",
    },
}


class HelpPageIn(BaseModel):
    title: str = Field(default="", max_length=120)
    body: str = Field(default="", max_length=20000)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        return v.strip()


def _load_all(db: Session) -> dict:
    slugs = [p["slug"] for p in PAGES.values()]
    rows = db.execute(
        text("select slug, title, body, updated_at from site_pages where slug in :slugs").bindparams(
            bindparam("slugs", expanding=True)
        ),
        {"slugs": slugs},
    ).all()
    by_slug = {r.slug: r for r in rows}

    pages = []
    for key, p in PAGES.items():
        row = by_slug.get(p["slug"])
        default_body: Callable[[], str] = p["default_body"]
        pages.append(
            {
                "key": key,
                "label": p["label"],
                "title": (row.title if row and row.title else None) or p["title"],
                # A saved page wins, even if its text was cleared on purpose.
                "body": (row.body or "") if row else default_body(),
                "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
            }
        )
    return {"pages": pages}


@store_router.get("/help")
def public_help(db: Session = Depends(get_db)):
    return _load_all(db)


@admin_router.get("")
def admin_get_help(db: Session = Depends(get_db)):
    return _load_all(db)


@admin_router.put("/{key}")
def admin_save_help(key: str, body: HelpPageIn, db: Session = Depends(get_db)):
    page = PAGES.get(key)
    if page is None:
        raise HTTPException(404, "Unknown help page")
    db.execute(
        text(
            "insert into site_pages (slug, title, body, updated_at) values (:s, :t, :b, now()) "
            "on conflict (slug) do update set title = excluded.title, body = excluded.body, updated_at = now()"
        ),
        {"s": page["slug"], "t": body.title or page["title"], "b": body.body},
    )
    db.commit()
    return _load_all(db)
