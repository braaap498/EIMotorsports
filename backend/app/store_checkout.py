"""
Checkout: price the cart, create the order, send the customer to Stripe, and
mark the order paid only when Stripe confirms the payment.
Mounted at /store/*.

  POST /store/checkout/quote    public   cart prices, shipping choices, tax
  POST /store/checkout/start    public   creates the order, returns the Stripe payment URL
                                         (a signed-in customer's token is used if sent)
  GET  /store/checkout/order    public   order summary for the confirmation page
                                         (needs the unguessable Stripe session id)
  POST /store/stripe/webhook    Stripe   signature checked; the only thing that marks an order paid

Rules this file follows:
  * Prices, stock and shipping are decided HERE from the database. The browser only
    says which products, how many, and which shipping choice.
  * Cart pricing is the cart's own function (store_extras.cart_price), so the cart and
    checkout cannot disagree about price or availability.
  * An order is 'paid' only when Stripe's signed webhook says so AND the amount matches.
  * Card details never touch this server; they are entered on Stripe's page.

Settings (environment variables, set in Render, never in code or chat):
  STRIPE_SECRET_KEY       checkout stays closed (503) until this is set
  STRIPE_WEBHOOK_SECRET   from the Stripe webhook endpoint you create
  SITE_URL                the storefront address, e.g. https://your-site.vercel.app
  STRIPE_TAX_ENABLED      leave unset/false until you register for Ohio sales tax
"""
import json
import logging
import os
import re
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, decode_token
from app.config import Settings, get_settings
from app.database import get_db
from app.shipping import find_option, shipping_options
from app.store_extras import AddressIn, CartItemIn, CartPriceIn, _clean_address, cart_price

log = logging.getLogger(__name__)
router = APIRouter(prefix="/store", tags=["checkout"])

_bearer = HTTPBearer(auto_error=False)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _tax_on() -> bool:
    return _env("STRIPE_TAX_ENABLED").lower() in ("1", "true", "yes")


def _open_stripe() -> None:
    key = _env("STRIPE_SECRET_KEY")
    if not key or not _env("SITE_URL"):
        raise HTTPException(503, "Checkout is not open yet")
    stripe.api_key = key


def optional_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> Optional[AuthUser]:
    """Signed-in customer if a valid token was sent, otherwise None (guest)."""
    if creds is None:
        return None
    claims = decode_token(creds.credentials, settings)  # a bad/expired token is a 401
    return AuthUser(user_id=claims["sub"], email=claims.get("email"), role="")


def _to_cents(price: str) -> int:
    return int((Decimal(price) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _price_cart(items: list[CartItemIn], db: Session) -> list[dict]:
    """Run the cart's own pricing. Returns one dict per product with cents added."""
    merged: dict[int, int] = {}
    for it in items:
        merged[it.product_id] = merged.get(it.product_id, 0) + it.qty
    if not merged:
        raise HTTPException(400, "Your cart is empty")
    priced = cart_price(
        CartPriceIn(items=[CartItemIn(product_id=pid, qty=min(q, 999)) for pid, q in merged.items()]),
        db,
    )["items"]
    lines = []
    for r in priced:
        line = dict(r)
        if r.get("price") is not None and not r.get("problem"):
            line["unit_price_cents"] = _to_cents(r["price"])
            line["line_total_cents"] = line["unit_price_cents"] * r["qty"]
        lines.append(line)
    return lines


def _totals(lines: list[dict]) -> tuple[int, int]:
    good = [l for l in lines if "unit_price_cents" in l]
    return sum(l["line_total_cents"] for l in good), sum(l["qty"] for l in good)


# --------------------------------------------------------------------------
# quote
# --------------------------------------------------------------------------
class ShipTo(BaseModel):
    region: Optional[str] = Field(None, max_length=50)
    postal_code: Optional[str] = Field(None, max_length=12)
    country: str = Field("US", max_length=2)


class QuoteIn(BaseModel):
    items: list[CartItemIn] = Field(..., max_length=50)
    ship_to: Optional[ShipTo] = None


@router.post("/checkout/quote")
def checkout_quote(body: QuoteIn, db: Session = Depends(get_db)):
    lines = _price_cart(body.items, db)
    subtotal, units = _totals(lines)
    ok = all(not l.get("problem") for l in lines)
    options = shipping_options(units, subtotal, body.ship_to.model_dump() if body.ship_to else None)
    return {
        "ok": ok,  # false means something in the cart changed: send the customer back to the cart
        "items": lines,
        "subtotal_cents": subtotal,
        "shipping_options": [{"code": o.code, "label": o.label, "cents": o.cents} for o in options],
        # None means "worked out by Stripe at payment" (only when tax is switched on)
        "tax_cents": None if _tax_on() else 0,
    }


# --------------------------------------------------------------------------
# start (create order + Stripe payment page)
# --------------------------------------------------------------------------
class CheckoutAddress(BaseModel):
    full_name: str = Field(..., max_length=100)
    line1: str = Field(..., max_length=120)
    line2: Optional[str] = Field(None, max_length=120)
    city: str = Field(..., max_length=80)
    region: str = Field(..., max_length=50)
    postal_code: str = Field(..., max_length=12)
    country: str = Field("US", min_length=2, max_length=2)
    phone: Optional[str] = Field(None, max_length=25)


class StartIn(BaseModel):
    items: list[CartItemIn] = Field(..., max_length=50)
    shipping_option: str = Field(..., max_length=40)
    email: Optional[str] = Field(None, max_length=200)
    shipping_address: CheckoutAddress


def _stripe_address(a: dict) -> dict:
    return {
        "line1": a["line1"],
        "line2": a["line2"] or "",
        "city": a["city"],
        "state": a["region"],
        "postal_code": a["postal_code"],
        "country": a["country"],
    }


@router.post("/checkout/start")
def checkout_start(
    body: StartIn,
    user: Optional[AuthUser] = Depends(optional_user),
    db: Session = Depends(get_db),
):
    _open_stripe()

    email = (body.email or (user.email if user else "") or "").strip().lower()
    if not _EMAIL.match(email):
        raise HTTPException(400, "Enter a valid email address")

    addr = _clean_address(AddressIn(kind="shipping", is_default=False, **body.shipping_address.model_dump()))
    if addr["country"] != "US":
        raise HTTPException(400, "We currently ship within the United States only")

    lines = _price_cart(body.items, db)
    if any(l.get("problem") for l in lines):
        raise HTTPException(
            409,
            "Some items in your cart changed or are no longer available. Please go back to your cart to review it.",
        )
    subtotal, units = _totals(lines)
    option = find_option(body.shipping_option, units, subtotal, addr)
    if option is None:
        raise HTTPException(400, "Please choose a shipping method")
    total = subtotal + option.cents  # tax, when switched on, is added by Stripe and recorded at payment

    order = db.execute(
        text(
            "insert into orders (user_id, status, subtotal_cents, shipping_cents, tax_cents, total_cents, "
            "customer_email, customer_name, shipping_address, payment_provider, notes) "
            "values (cast(:uid as uuid), 'pending_payment', :sub, :ship, 0, :total, "
            ":email, :name, cast(:addr as jsonb), 'stripe', :notes) returning id, order_number"
        ),
        {
            "uid": user.user_id if user else None,
            "sub": subtotal,
            "ship": option.cents,
            "total": total,
            "email": email,
            "name": addr["full_name"],
            "addr": json.dumps(addr),
            "notes": f"Shipping: {option.label}",
        },
    ).first()
    db.execute(
        text(
            "insert into order_items (order_id, product_id, title, part_number, unit_price_cents, quantity) "
            "values (:o, :p, :t, :pn, :u, :q)"
        ),
        [
            {"o": order.id, "p": l["product_id"], "t": l["title"], "pn": l.get("sku"),
             "u": l["unit_price_cents"], "q": l["qty"]}
            for l in lines
        ],
    )
    db.commit()

    site = _env("SITE_URL").rstrip("/")
    ship_name = {"name": addr["full_name"], "address": _stripe_address(addr)}
    params = dict(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": l["unit_price_cents"],
                    "tax_behavior": "exclusive",  # prices are before tax; Stripe adds tax on top
                    "product_data": {"name": (l["title"] or "Item")[:250]},
                },
                "quantity": l["qty"],
            }
            for l in lines
        ],
        shipping_options=[
            {
                "shipping_rate_data": {
                    "type": "fixed_amount",
                    "fixed_amount": {"amount": option.cents, "currency": "usd"},
                    "display_name": option.label,
                    "tax_behavior": "exclusive",
                }
            }
        ],
        client_reference_id=str(order.id),
        metadata={"order_id": str(order.id), "order_number": order.order_number},
        payment_intent_data={
            "metadata": {"order_id": str(order.id), "order_number": order.order_number},
            "receipt_email": email,
            "shipping": ship_name,
        },
        billing_address_collection="auto",
        expires_at=int(time.time()) + 3600,
        success_url=f"{site}/checkout/confirmation?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{site}/checkout",
    )
    try:
        if _tax_on():
            # Stripe works out sales tax from the customer's shipping address.
            customer = stripe.Customer.create(
                email=email, name=addr["full_name"], address=_stripe_address(addr), shipping=ship_name
            )
            params["customer"] = customer.id
            params["automatic_tax"] = {"enabled": True}
        else:
            params["customer_email"] = email
        session = stripe.checkout.Session.create(**params, idempotency_key=f"order-{order.id}")
    except Exception:
        log.exception("Stripe could not create a session for order %s", order.order_number)
        db.execute(text("update orders set status = 'cancelled' where id = :id"), {"id": order.id})
        db.commit()
        raise HTTPException(502, "We couldn't reach the payment provider. Please try again in a moment.")

    db.execute(
        text("update orders set payment_reference = :ref where id = :id"),
        {"ref": session.id, "id": order.id},
    )
    db.commit()
    return {"url": session.url, "order_number": order.order_number}


# --------------------------------------------------------------------------
# confirmation page data
# --------------------------------------------------------------------------
@router.get("/checkout/order")
def checkout_order(session_id: str, db: Session = Depends(get_db)):
    if not session_id.startswith("cs_") or len(session_id) > 200:
        raise HTTPException(404, "Order not found")
    o = db.execute(
        text(
            "select id, order_number, status, customer_email, subtotal_cents, shipping_cents, tax_cents, "
            "total_cents, shipping_address, notes from orders "
            "where payment_provider = 'stripe' and payment_reference = :ref"
        ),
        {"ref": session_id},
    ).first()
    if not o:
        raise HTTPException(404, "Order not found")
    items = db.execute(
        text(
            "select title, unit_price_cents, quantity, line_total_cents from order_items "
            "where order_id = :id order by id"
        ),
        {"id": o.id},
    ).all()
    addr = o.shipping_address or {}
    return {
        "order_number": o.order_number,
        "status": o.status,  # 'pending_payment' just after paying means the confirmation is still arriving
        "email": o.customer_email,
        "subtotal_cents": o.subtotal_cents,
        "shipping_cents": o.shipping_cents,
        "tax_cents": o.tax_cents,
        "total_cents": o.total_cents,
        "shipping_method": (o.notes or "").removeprefix("Shipping: ") or None,
        "ship_to": {"name": addr.get("full_name"), "city": addr.get("city"), "region": addr.get("region")},
        "items": [dict(i._mapping) for i in items],
    }


# --------------------------------------------------------------------------
# Stripe webhook
# --------------------------------------------------------------------------
def _order_id_of(session: dict) -> int:
    raw = (session.get("metadata") or {}).get("order_id") or session.get("client_reference_id") or "0"
    try:
        return int(raw)
    except ValueError:
        return 0


def _mark_paid(db: Session, session: dict) -> None:
    order = db.execute(
        text(
            "select id, status, subtotal_cents, shipping_cents from orders "
            "where id = :id and payment_provider = 'stripe' for update"
        ),
        {"id": _order_id_of(session)},
    ).first()
    if not order:
        log.error("Stripe payment for unknown order: %s", session.get("id"))
        return
    if order.status != "pending_payment":
        return  # webhook retry or duplicate: already handled

    tax = (session.get("total_details") or {}).get("amount_tax") or 0
    amount_total = session.get("amount_total")
    expected = order.subtotal_cents + order.shipping_cents
    if session.get("currency") != "usd" or amount_total is None or amount_total - tax != expected:
        log.error("Amount mismatch on order %s: paid %s (tax %s), expected %s", order.id, amount_total, tax, expected)
        db.execute(
            text("update orders set notes = coalesce(notes, '') || :n where id = :id"),
            {"id": order.id, "n": f" | CHECK PAYMENT: Stripe took {amount_total} cents, expected {expected} + tax"},
        )
        db.commit()
        return

    details = session.get("customer_details") or {}
    a = details.get("address") or {}
    billing = None
    if a.get("line1"):
        billing = {
            "full_name": details.get("name"), "line1": a.get("line1"), "line2": a.get("line2"),
            "city": a.get("city"), "region": a.get("state"), "postal_code": a.get("postal_code"),
            "country": a.get("country"),
        }
    db.execute(
        text(
            "update orders set status = 'paid', paid_at = now(), tax_cents = :tax, total_cents = :total, "
            "billing_address = coalesce(cast(:bill as jsonb), billing_address) where id = :id"
        ),
        {"id": order.id, "tax": tax, "total": amount_total, "bill": json.dumps(billing) if billing else None},
    )
    # Take the stock now that the money is in. Never goes below zero.
    db.execute(
        text(
            "update products p set quantity_on_hand = greatest(0, coalesce(p.quantity_on_hand, 0) - oi.quantity) "
            "from order_items oi where oi.order_id = :id and oi.product_id = p.id"
        ),
        {"id": order.id},
    )
    db.commit()


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    secret = _env("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(503, "Webhook not configured")
    payload = await request.body()
    try:
        # Checks Stripe's signature; anything not signed by Stripe is rejected here.
        stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), secret)
    except Exception:
        raise HTTPException(400, "Invalid webhook")

    event = json.loads(payload)
    kind = event.get("type")
    session = (event.get("data") or {}).get("object") or {}

    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if session.get("payment_status") == "paid":
            _mark_paid(db, session)
    elif kind in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        db.execute(
            text("update orders set status = 'cancelled' where id = :id and status = 'pending_payment'"),
            {"id": _order_id_of(session)},
        )
        db.commit()
    return {"received": True}
