"""
Amazon Buy Box and pricing (Sales channels > Amazon > Pricing). Mounted at /admin/amazon-buybox/* (owner/admin only).

  GET  /rows         listings with floor price, last read, and the recommendation
  POST /refresh      {account}   read Amazon's featured-offer expected price for linked listings (batched, 40 at a time)
  POST /{id}/apply   {price}     send one price to Amazon (needs Live pushes ON; never below the floor)
  PUT  /{id}         {max_price?}   a ceiling for that listing

THE RULES (decide):
  * FLOOR = what we cannot go under: (cost + shipping estimate) / (1 - referral fee - target margin). No cost -> no floor -> no advice.
  * Amazon sells the item itself: match only if we can do it at or above the floor; otherwise STAND DOWN (do not compete).
  * We hold the Buy Box: raise toward Amazon's expected price (never above max_price); otherwise leave alone.
  * Someone else holds it: win at the featured-offer expected price if it is at or above the floor; else match the holder if that
    is at or above the floor; else HOLD at the floor (we would lose money chasing it).
Recommendations only. Nothing is sent unless a person presses Apply on that listing.
The batch request/response shapes are from Amazon's Product Pricing API v2022-05-01 docs and are UNVERIFIED against a live call.
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.amazon_core import MARKETPLACES, NA_ENDPOINT, _row, access_token
from app.auth import require_admin
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/amazon-buybox", tags=["admin-amazon-buybox"], dependencies=[Depends(require_admin)])


def ensure_tables() -> None:
    db = _session_factory()()
    try:
        for stmt in (
            "alter table amazon_listings add column if not exists expected_price numeric",
            "alter table amazon_listings add column if not exists bb_price numeric",
            "alter table amazon_listings add column if not exists bb_is_ours boolean",
            "alter table amazon_listings add column if not exists amazon_on_listing boolean not null default false",
            "alter table amazon_listings add column if not exists amazon_price numeric",
            "alter table amazon_listings add column if not exists max_price numeric",
            "alter table amazon_listings add column if not exists bb_checked_at timestamptz",
        ):
            db.execute(text(stmt))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not set up the Buy Box columns")
    finally:
        db.close()
    ensure_price_queue()


def floor_price(cost: Optional[float], ship: float, fee_pct: float, margin_pct: float, pieces: int = 1) -> Optional[float]:
    if cost is None or cost <= 0:
        return None
    keep = 1 - (fee_pct + margin_pct) / 100.0
    if keep <= 0.05:
        return None
    return round((cost * max(1, pieces) + ship) / keep, 2)


def decide(l, floor: Optional[float]) -> dict:
    """l has: price, bb_checked_at, expected_price, bb_price, max_price, amazon_on_listing, amazon_price, bb_is_ours."""
    def out(action, price, why):
        return {"action": action, "price": price, "why": why}

    f = lambda v: None if v is None else float(v)
    if l.bb_checked_at is None:
        return out("unknown", None, "Not read from Amazon yet.")
    if floor is None:
        return out("unknown", None, "No floor: add the product's cost first.")
    cur, exp, bb, mx, am = f(l.price), f(l.expected_price), f(l.bb_price), f(l.max_price), f(l.amazon_price)
    if l.amazon_on_listing:
        if am is not None and am < floor:
            return out("stand_down", None, f"Amazon sells this at about {am:.2f}, under our floor of {floor:.2f}. Competing would lose money.")
        target = round(max(floor, min(x for x in (am, exp) if x is not None) if (am is not None or exp is not None) else floor), 2)
        return out("match", target, f"Amazon sells this. We can match at {target:.2f} (floor {floor:.2f}).")
    if l.bb_is_ours:
        if exp is not None and cur is not None and exp > cur + 0.01:
            t = round(min(exp, mx) if mx else exp, 2)
            if t > cur + 0.01:
                return out("raise", t, f"We hold the Buy Box and Amazon's data says it holds up to {exp:.2f}. Raising from {cur:.2f} adds margin.")
        return out("none", None, "We hold the Buy Box at a good price.")
    if exp is not None and exp >= floor:
        return out("win", round(exp, 2), f"Another seller holds the Buy Box. {exp:.2f} is the price at or below which our offer can be featured.")
    if bb is not None and bb >= floor:
        return out("win", round(bb, 2), f"Match the Buy Box holder ({bb:.2f}); that is above our floor of {floor:.2f}.")
    ref = bb if bb is not None else exp
    return out("hold", floor, f"The Buy Box is at {(ref or 0):.2f}, under our floor. Hold at {floor:.2f}; winning would mean selling at a loss.")


_COST = "nullif(to_jsonb(p)->>'cost', '')::numeric"


@router.get("/rows")
def rows(account: str = "", only: str = "", offset: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    where, p = ["l.product_id is not null", "l.fulfillment = 'MFN'"], {"lim": min(limit, 200), "off": max(offset, 0)}
    if account.strip():
        where.append("l.account = :a")
        p["a"] = account.strip()
    res = db.execute(
        text(
            f"select l.id, l.account, l.sku, l.title, l.price, l.expected_price, l.bb_price, l.bb_is_ours, l.amazon_on_listing, l.amazon_price, "
            f"l.max_price, l.bb_checked_at, l.pieces_per_sale, {_COST} as cost, a.referral_fee_pct, a.target_margin_pct, a.ship_cost_est "
            f"from amazon_listings l join products p on p.id = l.product_id join amazon_accounts a on a.account = l.account "
            f"where {' and '.join(where)} order by l.id desc limit :lim offset :off"
        ),
        p,
    ).all()
    items = []
    for r in res:
        fl = floor_price(None if r.cost is None else float(r.cost), float(r.ship_cost_est), float(r.referral_fee_pct), float(r.target_margin_pct), int(r.pieces_per_sale or 1))
        d = decide(r, fl)
        if only and d["action"] != only:
            continue
        items.append({"id": r.id, "account": r.account, "sku": r.sku, "title": r.title, "price": r.price, "floor": fl, "buy_box": r.bb_price,
                      "bb_is_ours": r.bb_is_ours, "amazon_on_listing": r.amazon_on_listing, "checked": r.bb_checked_at, "max_price": r.max_price,
                      "action": d["action"], "suggested": d["price"], "why": d["why"]})
    return {"items": items}


class AccountIn(BaseModel):
    account: str


@router.post("/refresh")
def refresh(body: AccountIn, db: Session = Depends(get_db)):
    a = _row(db, body.account)
    ls = db.execute(text("select id, sku, marketplace from amazon_listings where account = :a and product_id is not null and fulfillment = 'MFN' order by bb_checked_at nulls first limit 200"), {"a": a.account}).all()
    done = 0
    for i in range(0, len(ls), 40):
        chunk = ls[i : i + 40]
        reqs = [{"uri": "/products/pricing/2022-05-01/offer/featuredOfferExpectedPrice", "method": "POST", "marketplaceId": MARKETPLACES.get(c.marketplace, MARKETPLACES["US"]), "sku": c.sku} for c in chunk]
        req = urllib.request.Request(
            NA_ENDPOINT + "/batches/products/pricing/2022-05-01/offer/featuredOfferExpectedPrice",
            data=json.dumps({"requests": reqs}).encode(), method="POST",
            headers={"x-amz-access-token": access_token(db, a.account), "Content-Type": "application/json"},
        )
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise HTTPException(502, f"Amazon refused the price read ({e.code}): {e.read().decode('utf-8', 'replace')[:200]}")
        except (urllib.error.URLError, TimeoutError):
            raise HTTPException(502, "Could not reach Amazon.")
        for c, resp in zip(chunk, data.get("responses") or []):
            body_ = (resp or {}).get("body") or {}
            results = body_.get("featuredOfferExpectedPriceResults") or []
            exp = None
            try:
                exp = float(((results[0].get("featuredOfferExpectedPrice") or {}).get("listingPrice") or {}).get("amount"))
            except (IndexError, TypeError, ValueError):
                pass
            db.execute(text("update amazon_listings set expected_price = :e, bb_checked_at = now() where id = :i"), {"e": exp, "i": c.id})
            done += 1
        db.commit()
    return {"ok": True, "read": done}


class ApplyIn(BaseModel):
    price: float = Field(..., gt=0, le=100000)


_LOAD = (
    "select l.id, l.account, l.sku, l.marketplace, l.product_type, l.pieces_per_sale, {cost} as cost, a.referral_fee_pct, a.target_margin_pct, "
    "a.ship_cost_est, a.live_pushes_enabled, a.seller_id from amazon_listings l join products p on p.id = l.product_id "
    "join amazon_accounts a on a.account = l.account where l.id = :i"
)


def _load_for_push(db: Session, listing_id: int):
    l = db.execute(text(_LOAD.format(cost=_COST)), {"i": listing_id}).first()
    if not l:
        raise HTTPException(404, "Listing not found (it must be linked to a product).")
    if not l.live_pushes_enabled:
        raise HTTPException(409, "Live pushes are OFF for this Amazon account.")
    return l


def _check_price(l, price: float) -> float:
    """The floor check shared by Apply and the queue. Returns the floor; raises when the price must not go out."""
    fl = floor_price(None if l.cost is None else float(l.cost), float(l.ship_cost_est), float(l.referral_fee_pct), float(l.target_margin_pct), int(l.pieces_per_sale or 1))
    if fl is None:
        raise HTTPException(409, "The product has no cost, so there is no floor to protect. Add the cost first.")
    if price < fl:
        raise HTTPException(409, f"{price:.2f} is under the floor of {fl:.2f}. Nothing was sent.")
    if not l.product_type:
        raise HTTPException(409, "Sync the listings again so Amazon's product type is known.")
    return fl


def _send_price(db: Session, l, price: float) -> None:
    patch = {"productType": l.product_type, "patches": [{"op": "replace", "path": "/attributes/purchasable_offer", "value": [
        {"marketplace_id": MARKETPLACES.get(l.marketplace, MARKETPLACES["US"]), "currency": "USD", "our_price": [{"schedule": [{"value_with_tax": round(price, 2)}]}]}]}]}
    url = f"{NA_ENDPOINT}/listings/2021-08-01/items/{l.seller_id}/{urllib.parse.quote(l.sku, safe='')}?marketplaceIds={MARKETPLACES.get(l.marketplace, MARKETPLACES['US'])}"
    req = urllib.request.Request(url, data=json.dumps(patch).encode(), method="PATCH", headers={"x-amz-access-token": access_token(db, l.account), "Content-Type": "application/json"})
    try:
        res = json.loads(urllib.request.urlopen(req, timeout=25).read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"Amazon refused it ({e.code}): {e.read().decode('utf-8', 'replace')[:200]}")
    except (urllib.error.URLError, TimeoutError):
        raise HTTPException(502, "Could not reach Amazon.")
    if str(res.get("status", "")).upper() == "INVALID":
        raise HTTPException(502, "Amazon marked the price invalid: " + json.dumps(res.get("issues"))[:200])
    db.execute(text("update amazon_listings set price = :p where id = :i"), {"p": price, "i": l.id})


@router.post("/{listing_id}/apply")
def apply(listing_id: int, body: ApplyIn, db: Session = Depends(get_db)):
    l = _load_for_push(db, listing_id)
    fl = _check_price(l, body.price)
    _send_price(db, l, body.price)
    db.commit()
    return {"ok": True, "price": body.price, "floor": fl}


# ---- queued price pushes ----------------------------------------------------------------------------
def ensure_price_queue() -> None:
    db = _session_factory()()
    try:
        db.execute(text(
            """create table if not exists amazon_price_queue (
                 id bigserial primary key,
                 listing_id bigint not null,
                 price numeric not null,
                 status text not null default 'pending',
                 attempts integer not null default 0,
                 last_error text,
                 created_at timestamptz not null default now(),
                 done_at timestamptz
               )"""))
        db.execute(text("create unique index if not exists amazon_price_queue_one_pending on amazon_price_queue (listing_id) where status = 'pending'"))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not set up the Amazon price queue")
    finally:
        db.close()


@router.post("/{listing_id}/queue")
def queue_price(listing_id: int, body: ApplyIn, db: Session = Depends(get_db)):
    """Line a price up to be sent by the background loop. Checked against the floor now and again when it is sent."""
    l = _load_for_push(db, listing_id)
    fl = _check_price(l, body.price)
    db.execute(text(
        "insert into amazon_price_queue (listing_id, price) values (:i, :p) "
        "on conflict (listing_id) where status = 'pending' do update set price = excluded.price, attempts = 0, last_error = null, created_at = now()"),
        {"i": listing_id, "p": body.price})
    db.commit()
    return {"ok": True, "queued": True, "price": body.price, "floor": fl}


@router.get("/queue")
def queue_list(db: Session = Depends(get_db)):
    rows = db.execute(text(
        "select q.id, q.listing_id, l.sku, l.account, q.price, q.status, q.attempts, q.last_error, q.created_at, q.done_at "
        "from amazon_price_queue q left join amazon_listings l on l.id = q.listing_id order by q.id desc limit 100")).all()
    return {"rows": [{**r._mapping} for r in rows]}


def run_price_queue(db: Session, limit: int = 10) -> dict:
    """Send pending prices one at a time. Each is re-checked against the floor, because cost or settings may have changed since it was queued."""
    sent = failed = 0
    for q in db.execute(text("select id, listing_id, price from amazon_price_queue where status = 'pending' order by id limit :n"), {"n": limit}).all():
        try:
            l = _load_for_push(db, q.listing_id)
            price = float(q.price)
            _check_price(l, price)
            _send_price(db, l, price)
            db.execute(text("update amazon_price_queue set status = 'done', done_at = now(), last_error = null where id = :i"), {"i": q.id})
            sent += 1
        except HTTPException as e:
            db.rollback()
            retry = e.status_code == 502
            n = db.execute(text("select attempts from amazon_price_queue where id = :i"), {"i": q.id}).scalar() or 0
            db.execute(text("update amazon_price_queue set attempts = :n, last_error = :e, status = :s where id = :i"),
                       {"n": n + 1, "e": str(e.detail)[:300], "s": "pending" if retry and n + 1 < 5 else "failed", "i": q.id})
            failed += 1
        except Exception as e:
            db.rollback()
            log.exception("queued Amazon price %s", q.id)
            db.execute(text("update amazon_price_queue set status = 'failed', last_error = :e where id = :i"), {"e": str(e)[:300], "i": q.id})
            failed += 1
        db.commit()
    return {"sent": sent, "failed": failed}


@router.post("/queue/run")
def queue_run(db: Session = Depends(get_db)):
    return run_price_queue(db)


class MaxIn(BaseModel):
    max_price: Optional[float] = Field(None, gt=0, le=100000)


@router.put("/{listing_id}")
def set_max(listing_id: int, body: MaxIn, db: Session = Depends(get_db)):
    if not db.execute(text("update amazon_listings set max_price = :m where id = :i returning id"), {"m": body.max_price, "i": listing_id}).first():
        raise HTTPException(404, "Listing not found.")
    db.commit()
    return {"ok": True}
