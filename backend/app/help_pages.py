"""
The Help pages (Shipping, Returns, Refunds, Privacy and Contact): their text is stored in the database and edited
from the admin area, the same way the About page is.

  GET /store/help         public        both pages, for the tabs on the storefront's /help page
  GET /admin/help         admin/owner   the same, for the editor
  PUT /admin/help/{key}   admin/owner   save one page (it shows on the site straight away)

`key` is "shipping", "returns", "refunds", "privacy" or "contact". Each page is one row in the
site_pages table (slugs "help-shipping", "help-returns", "help-refunds", "help-privacy", "help-contact"),
so no new table or migration is needed. To add another tab later, add it to PAGES below.

Until a page is saved for the first time it shows a modest starter policy written in this file:
  * Shipping: the methods and prices are built from app/shipping.py, so they always match checkout.
  * Returns, Refunds, Privacy: general starter terms. Contact: a short how-to-reach-us page. They are the shop's own terms, so read
    them, change anything that is not how the shop works, and save your version in the admin editor.
  * Set SUPPORT_EMAIL (Render environment variable) and the starter text tells customers to email
    that address; without it the text just says "contact us".
Once a page has been saved, what was saved is what shows, even if it was cleared on purpose.

The page text is rich text, edited with the same visual toolbar as product descriptions.
It is stored as cleaned HTML (app/pagetext.py): the server cleans it when it is saved and
again when it is served. The starter policies below are written as simple plain text
("## " for a heading, "- " for a bullet) and are converted to HTML when read.
"""
import os
from html import escape
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db
from app.pagetext import clean_page_input, page_html
from app.shipping import RATES

admin_router = APIRouter(prefix="/help", tags=["admin-help"], dependencies=[Depends(require_admin)])
store_router = APIRouter(prefix="/store", tags=["help"])


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _reach_us() -> str:
    """How the starter text tells customers to get in touch (lower case, used mid-sentence)."""
    email = os.getenv("SUPPORT_EMAIL", "").strip()
    return f"email us at {email}" if email else "contact us"


def _default_shipping() -> str:
    """Starter shipping text. The methods and prices come from the list checkout uses."""
    methods = "\n".join(
        f"- {label}: {'Free' if cents == 0 else _money(cents) + ' per item'}" for _code, label, cents in RATES
    )
    return "\n".join(
        [
            "## Where we ship",
            "We currently ship within the United States only.",
            "",
            "## Shipping methods and prices",
            "You choose a shipping method at checkout, and the price is shown before you pay. "
            "Shipping is priced per item.",
            methods,
            "",
            "## Delivery times",
            "We ship orders as quickly as we can once your payment is confirmed. Delivery times are "
            "estimates from USPS and are not guaranteed. Delays can happen, especially around holidays "
            "or in bad weather.",
            "",
            "## Your shipping address",
            "We ship to the address you enter at checkout, so please double-check it. If you notice a "
            f"mistake, {_reach_us()} right away and we will try to help. We cannot promise to change "
            "the address once an order has shipped.",
            "",
            "## Taxes",
            "Sales tax, where it applies, is calculated at checkout.",
            "",
            "## Lost or damaged packages",
            f"If your order arrives damaged or does not arrive, {_reach_us()} and we will help sort it "
            "out. See the Refunds tab for what to expect.",
        ]
    )


def _default_contact() -> str:
    """Starter contact page. Add your phone, hours and address in the admin editor."""
    email = os.getenv("SUPPORT_EMAIL", "").strip()
    if email:
        e = escape(email, quote=True)
        reach = f'<p>Email us at <a href="mailto:{e}">{escape(email, quote=False)}</a>.</p>'
    else:
        reach = "<p>Contact details are coming soon.</p>"
    return (
        "<h3>Contact us</h3>"
        "<p>Questions about an order, a part, or a return? We are happy to help.</p>"
        f"{reach}"
        "<h3>Before you write</h3>"
        "<p>Please include your order number. For a question about whether a part fits, include "
        "your vehicle's year, make and model.</p>"
        "<h3>More help</h3>"
        "<p>Shipping, returns, refunds and privacy information is on the other tabs of this page.</p>"
    )


def _default_returns() -> str:
    return "\n".join(
        [
            "## Return window",
            "You can return most items within 30 days of the day your order was delivered.",
            "",
            "## What can be returned",
            "- Items that are unused and uninstalled",
            "- In their original packaging, with all parts included",
            "- Items that were damaged, defective, or not what you ordered (see below)",
            "",
            "## How to start a return",
            f"Please do not send anything back before you hear from us. First, {_reach_us()} with your "
            "order number and the reason for the return. We will reply with the next steps and the "
            "address to send the item to.",
            "",
            "## Return shipping",
            "If you are returning an item because you changed your mind or ordered the wrong part, you "
            "pay for return shipping. If we sent the wrong item, or it arrived damaged or defective, we "
            "will cover it.",
            "",
            "## Checking fit",
            "Use the Year, Make and Model selector and compare part numbers before you order. A part "
            "ordered by mistake can still be returned under the terms above.",
            "",
            "## Damaged or incorrect items",
            f"{_reach_us()[0].upper()}{_reach_us()[1:]} within 7 days of delivery and include your order number and a "
            "few photos of the item and the packaging.",
        ]
    )


def _default_refunds() -> str:
    return "\n".join(
        [
            "## When we refund",
            "Once we receive and check a returned item, we refund the item price to the card you paid "
            "with. You will get a message from us when the refund has been issued.",
            "",
            "## How long it takes",
            "Refunds are sent to your original payment method. Depending on your bank or card issuer, "
            "it can take about 5 to 10 business days for the money to show up on your statement.",
            "",
            "## Shipping costs",
            "The original shipping charge is not refunded unless the return is because of our mistake "
            "(the wrong item, or an item that arrived damaged or defective).",
            "",
            "## Cancelling an order",
            f"To cancel an order, {_reach_us()} as soon as you can. If it has not shipped yet, we will "
            "cancel it and refund the full amount. If it has already shipped, you can return it under "
            "our returns policy.",
            "",
            "## Lost packages",
            f"If a package never arrives, {_reach_us()}. We will look into it with the carrier and "
            "either send a replacement or refund your order.",
        ]
    )


def _default_privacy() -> str:
    return "\n".join(
        [
            "## What we collect",
            "When you place an order or create an account, we collect:",
            "- Your name and email address",
            "- Your shipping address and, if you give it, a phone number",
            "- The billing address you enter when paying",
            "- Your order history",
            "",
            "## Payments",
            "Payments are handled by Stripe. You enter your card details on Stripe's secure payment "
            "page, and your card number never reaches our website or our servers.",
            "",
            "## How we use your information",
            "We use it to process and ship your order, send receipts and order updates, answer your "
            "questions, and keep our records. We do not sell your personal information.",
            "",
            "## Who we share it with",
            "We share information only with the services that help us run the store, such as payment "
            "processing, account sign-in and data storage, and shipping carriers, and only what they "
            "need to do their job. We may also share it if the law requires.",
            "",
            "## Cookies and your device",
            "The site stores some information in your browser to work properly, such as keeping you "
            "signed in and saving the items in your cart on your device.",
            "",
            "## Keeping your information",
            "We keep order records for as long as we need them for accounting, tax, and customer "
            f"service. To ask for a copy of your information or to have your account information "
            f"deleted, {_reach_us()}. We may need to keep some order records after that.",
            "",
            "## Children",
            "This store is not meant for children under 13, and we do not knowingly collect their "
            "information.",
            "",
            "## Changes",
            "We may update this page from time to time. The version on this page is the current one.",
        ]
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
        "default_body": _default_returns,
    },
    "refunds": {
        "slug": "help-refunds",
        "label": "Refunds",
        "title": "Refunds",
        "default_body": _default_refunds,
    },
    "privacy": {
        "slug": "help-privacy",
        "label": "Privacy",
        "title": "Privacy policy",
        "default_body": _default_privacy,
    },
    "contact": {
        "slug": "help-contact",
        "label": "Contact",
        "title": "Contact us",
        "default_body": _default_contact,
    },
}


class HelpPageIn(BaseModel):
    title: str = Field(default="", max_length=120)
    body: str = Field(default="", max_length=60000)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        return v.strip()

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        return clean_page_input(v)


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
                "body": page_html(row.body) if row else page_html(default_body()),
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
