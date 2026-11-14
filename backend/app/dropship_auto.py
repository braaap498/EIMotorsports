"""
Automatic drop shipping, step 1: an eBay order that has to be drop shipped makes its purchase order by itself,
then WAITS for a person to look at the PO and confirm it before anything goes further.

Chain (a temporary review stop at every step while it is being proved out):
  1. eBay order paid        -> drop ship PO made (buyer name, address, and the shipping service the buyer picked)
  2. review_po   a person checks the PO              <- BUILT (this file)
  3. quote       confirming the PO gets a quote from Automatic Distributors; the shipping option closest to the buyer's choice is picked
  4. review_quote a person checks the quote (and can change the shipping option), then places the order (owner/admin only)
  5. place       the order is placed through the order API (the existing safeguards still apply: test/live server, live switch)
  6. review_tracking tracking comes back (existing automatic check); a person checks it, then eBay is told it shipped, with the
                  tracking number, and eBay notifies the buyer

Nothing runs by itself until "Make POs automatically" is switched on (default OFF).
"""
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import ADMIN_ROLES, AuthUser, needs, require_route_access
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/dropship", tags=["admin-dropship"], dependencies=[Depends(require_route_access)])

SYSTEM_USER = AuthUser(user_id="system", email="Automatic drop ship", role="admin", permissions=frozenset())
PERMS = ("inventory.purchase", "inventory.ship")
RECENT_DAYS = 10

_ready = False


def _ensure(db: Session) -> None:
    global _ready
    if _ready:
        return
    for stmt in (
        """create table if not exists dropship_jobs (
             id bigserial primary key,
             ebay_order_id text not null unique,
             store text,
             po_id bigint,
             po_number bigint,
             stage text not null default 'review_po',
             ship_service text,
             buyer_name text,
             problem text,
             created_by text,
             created_at timestamptz not null default now(),
             updated_at timestamptz not null default now(),
             confirmed_by text,
             confirmed_at timestamptz
           )""",
        """create table if not exists dropship_settings (
             id int primary key default 1,
             auto_po boolean not null default false,
             last_scan_at timestamptz,
             last_scan_note text
           )""",
        "insert into dropship_settings (id) values (1) on conflict (id) do nothing",
        "alter table dropship_jobs add column if not exists quote text",
        "alter table dropship_jobs add column if not exists ship_method text",
        "alter table dropship_jobs add column if not exists supplier_order text",
        "alter table dropship_jobs add column if not exists supplier_mode text",
        "alter table dropship_jobs add column if not exists tracking_number text",
        "alter table dropship_jobs add column if not exists tracking_carrier text",
    ):
        db.execute(text(stmt))
    db.commit()
    _ready = True


def _settings(db: Session) -> dict:
    _ensure(db)
    r = db.execute(text("select auto_po, last_scan_at, last_scan_note from dropship_settings where id = 1")).first()
    return {"auto_po": bool(r.auto_po), "last_scan_at": r.last_scan_at, "last_scan_note": r.last_scan_note}


def _flagged_short_lines(db: Session, order_id: str, honor_decision: bool = False) -> list[int]:
    """Products on this eBay order that are short on our shelf AND whose listing (the one the buyer bought from) is set to
    Drop ship. A flag on another listing of the same product does not count: those orders go to the Pick & ship decision
    instead. With honor_decision (the manual button only), an order a person approved as 'drop ship' there counts too."""
    from app import fulfillment
    from app.admin_purchase_orders import _ebay_order_and_lines

    _o, lines = _ebay_order_and_lines(db, order_id)
    if not any(l.product_id is not None for l in lines):
        return []
    flags = fulfillment.ebay_line_dropship_flags(db, [_o.ebay_order_id])
    approved = False
    if honor_decision:
        approved = (fulfillment.decisions_for(db, [fulfillment.key_of("eBay", _o.ebay_order_id)]).get(fulfillment.key_of("eBay", _o.ebay_order_id)) or {}).get("decision") == "dropship"
    return sorted(
        {
            l.product_id
            for l in lines
            if l.product_id is not None
            and (flags.get((_o.ebay_order_id, int(l.product_id))) or approved)
            and int(l.on_hand or 0) < int(l.quantity or 1)
        }
    )


def _make_job(db: Session, order_id: str, store: Optional[str], product_ids: list[int], user: AuthUser) -> dict:
    """Make the PO and record the job. A refusal (Global Shipping Program, incomplete address...) is recorded as 'problem'."""
    from app.admin_purchase_orders import EbayDropIn, po_from_ebay_order

    try:
        po = po_from_ebay_order(EbayDropIn(order=order_id, product_ids=product_ids), db=db, user=user)
    except HTTPException as e:
        db.rollback()
        msg = str(e.detail)[:400]
        db.execute(
            text(
                "insert into dropship_jobs (ebay_order_id, store, stage, problem, created_by) values (:o, :s, 'problem', :m, :u) "
                "on conflict (ebay_order_id) do nothing"
            ),
            {"o": order_id, "s": store, "m": msg, "u": user.email},
        )
        db.commit()
        return {"order": order_id, "problem": msg}
    db.execute(
        text(
            "insert into dropship_jobs (ebay_order_id, store, po_id, po_number, stage, ship_service, buyer_name, created_by) "
            "values (:o, :s, :pid, :pn, 'review_po', :svc, :bn, :u) on conflict (ebay_order_id) do nothing"
        ),
        {"o": order_id, "s": store, "pid": po["id"], "pn": po["po_number"], "svc": po.get("ship_service"), "bn": po.get("buyer_name"), "u": user.email},
    )
    db.commit()
    return {"order": order_id, "po_number": po["po_number"]}


def scan_new_orders(db: Session, user: AuthUser = SYSTEM_USER) -> dict:
    _ensure(db)
    rows = db.execute(
        text(
            "select o.ebay_order_id, o.store from ebay_orders o "
            "where o.paid_at is not null and not o.cancelled and o.shipped_at_ebay is null "
            "and coalesce(o.created_at_ebay, o.paid_at) > now() - make_interval(days => :d) "
            "and not exists (select 1 from dropship_jobs j where j.ebay_order_id = o.ebay_order_id) "
            "order by o.id limit 50"
        ),
        {"d": RECENT_DAYS},
    ).all()
    made, problems = [], []
    for r in rows:
        try:
            picks = _flagged_short_lines(db, r.ebay_order_id)
            if not picks:
                continue
            res = _make_job(db, r.ebay_order_id, r.store, picks, user)
            (problems if res.get("problem") else made).append(res)
        except Exception:
            db.rollback()
            log.exception("drop ship check failed for eBay order %s", r.ebay_order_id)
    note = f"Checked {len(rows)} new order(s): {len(made)} PO(s) made" + (f", {len(problems)} need attention" if problems else "") + "."
    db.execute(text("update dropship_settings set last_scan_at = now(), last_scan_note = :n where id = 1"), {"n": note})
    db.commit()
    return {"checked": len(rows), "made": made, "problems": problems, "note": note}


def scan_if_enabled() -> None:
    """Called by the eBay sales timer after it has fetched orders. Does nothing unless the switch is on."""
    db = _session_factory()()
    try:
        if _settings(db)["auto_po"]:
            scan_new_orders(db)
        advance_tracking(db)
    except Exception:
        db.rollback()
        log.exception("automatic drop ship check failed")
    finally:
        db.close()


class SettingsIn(BaseModel):
    auto_po: bool


@router.get("/settings")
@needs(*PERMS)
def get_settings(db: Session = Depends(get_db)):
    return _settings(db)


@router.put("/settings")
@needs(*PERMS)
def put_settings(body: SettingsIn, db: Session = Depends(get_db)):
    _ensure(db)
    db.execute(text("update dropship_settings set auto_po = :a where id = 1"), {"a": body.auto_po})
    db.commit()
    return _settings(db)


@router.get("/jobs")
@needs(*PERMS)
def list_jobs(stage: Optional[str] = Query(None, max_length=20), db: Session = Depends(get_db)):
    from app.admin_purchase_orders import _po_detail

    _ensure(db)
    rows = db.execute(
        text(
            "select id, ebay_order_id, store, po_id, po_number, stage, ship_service, buyer_name, problem, created_at, confirmed_by, confirmed_at, "
            "quote, ship_method, supplier_order, supplier_mode, tracking_number, tracking_carrier "
            "from dropship_jobs "
            + ("where stage in ('review_po', 'po_confirmed', 'review_quote', 'review_tracking') " if stage == "review"
               else "where stage = :s " if stage else "where stage <> 'dismissed' ")
            + "order by id desc limit 100"
        ),
        {"s": stage} if stage and stage != "review" else {},
    ).all()
    out = []
    for r in rows:
        j = dict(r._mapping)
        try:
            j["quote"] = json.loads(r.quote) if r.quote else None
        except ValueError:
            j["quote"] = None
        if r.po_id and r.stage in ("review_po", "po_confirmed", "review_quote", "review_tracking"):
            try:
                j["po"] = _po_detail(db, r.po_id)
            except HTTPException:
                j["po"] = None
        out.append(j)
    return {"jobs": out}


@router.post("/scan")
@needs(*PERMS)
def scan_now(db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    out = scan_new_orders(db, user)
    out["tracking_ready"] = advance_tracking(db)
    return out


class FromOrderIn(BaseModel):
    order: str = Field(..., min_length=3, max_length=40)


@router.post("/jobs/from-order", status_code=201)
@needs(*PERMS)
def job_from_order(body: FromOrderIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    from app.admin_purchase_orders import _ebay_order_and_lines

    _ensure(db)
    o, _lines = _ebay_order_and_lines(db, body.order)
    if db.execute(text("select 1 from dropship_jobs where ebay_order_id = :o and stage <> 'dismissed'"), {"o": o.ebay_order_id}).first():
        raise HTTPException(409, f"eBay order {o.ebay_order_id} is already in the list.")
    db.execute(text("delete from dropship_jobs where ebay_order_id = :o"), {"o": o.ebay_order_id})
    db.commit()
    picks = _flagged_short_lines(db, o.ebay_order_id, honor_decision=True)
    if not picks:
        raise HTTPException(
            409,
            "Nothing on that order can be drop shipped: no line is short on our shelf with its listing set to Drop ship, "
            "and nobody approved a drop ship for it on Pick & ship.",
        )
    res = _make_job(db, o.ebay_order_id, o.store, picks, user)
    if res.get("problem"):
        raise HTTPException(409, res["problem"])
    return res


def _job(db: Session, job_id: int):
    _ensure(db)
    j = db.execute(text("select id, stage, po_number from dropship_jobs where id = :i"), {"i": job_id}).first()
    if not j:
        raise HTTPException(404, "That drop ship order is not in the list.")
    return j


@router.post("/jobs/{job_id}/confirm")
@needs(*PERMS)
def confirm_po(job_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    j = _job(db, job_id)
    if j.stage != "review_po":
        raise HTTPException(409, "That order is not waiting for a PO check.")
    db.execute(
        text("update dropship_jobs set stage = 'po_confirmed', confirmed_by = :u, confirmed_at = now(), updated_at = now() where id = :i"),
        {"u": user.email, "i": job_id},
    )
    db.commit()
    return _run_quote(db, job_id, None)


# --------------------------------------------------------------------------
# step 3 and 4: quote, then place
# --------------------------------------------------------------------------
def _norm(v: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (v or "").lower())


def _service_words(name: str) -> tuple[Optional[str], Optional[str]]:
    """(carrier, speed) read out of a shipping service name, whichever side it comes from (eBay's or the distributor's)."""
    n = _norm(name)
    carrier = "fedex" if ("fedex" in n or "fdx" in n) else "ups" if "ups" in n else "usps" if ("usps" in n or "postal" in n) else None
    speed = None
    for key, words in (
        ("overnight", ("nextday", "overnight", "1day", "nextbusiness")),
        ("2day", ("2day", "secondday", "2ndday", "twoday")),
        ("3day", ("3day", "threeday", "express saver", "expresssaver")),
        ("ground", ("ground", "standard", "economy", "homedelivery", "surepost", "groundadvantage")),
        ("priority", ("priority",)),
    ):
        if any(w in n for w in words):
            speed = key
            break
    return carrier, speed


def pick_shipping_option(buyer_service: Optional[str], options: list[dict]) -> Optional[str]:
    """The distributor's shipping option closest to the buyer's eBay choice, or None when there is no safe match
    (a person then picks it at the quote check). A match needs the same speed; the same carrier is preferred."""
    if not buyer_service or not options:
        return None
    bc, bs = _service_words(buyer_service)
    if not bs:
        return None
    best, best_score = None, 0
    for o in options:
        name = str(o.get("name") or "")
        oc, os_ = _service_words(name)
        if os_ != bs:
            continue
        score = 1 + (2 if bc and oc == bc else 0)
        try:
            cost = float(o.get("cost"))
        except (TypeError, ValueError):
            cost = 1e9
        # a higher score wins; among equals the cheaper one
        if score > best_score or (score == best_score and best is not None and cost < best[1]):
            best, best_score = (name, cost), score
    return best[0] if best else None


def _order_in(db: Session, job, method: Optional[str], confirm: bool):
    from app.autodist import OrderAddress, OrderIn, OrderLine, api_po

    po = api_po(job.po_id, db=db)
    missing = [l["item"] or l["description"] for l in po["lines"] if not l["number"]]
    if missing:
        raise HTTPException(409, "These PO lines are not matched to a distributor part number: " + ", ".join(str(m) for m in missing)[:200])
    if not po["lines"]:
        raise HTTPException(409, "That PO has no open lines.")
    addr = {k: v for k, v in (po["ship_to"] or {}).items() if k in OrderAddress.model_fields}
    return OrderIn(
        po_number=str(po["po_number"]),
        comment=f"Drop ship for eBay order {job.ebay_order_id}"[:200],
        shipping_method=method or "",
        ship_to=OrderAddress(**addr),
        items=[OrderLine(number=l["number"], quantity=int(l["quantity"])) for l in po["lines"]],
        po_id=job.po_id,
        confirm=confirm,
    )


def _run_quote(db: Session, job_id: int, method: Optional[str]) -> dict:
    """Get the quote for a job's PO. With no method, the option closest to the buyer's eBay choice is picked when there is one.
    A failure keeps the job where it was and records the reason, so the page can show it and offer to try again."""
    from app.autodist import api_order_quote

    j = db.execute(
        text("select id, po_id, ebay_order_id, ship_service, stage from dropship_jobs where id = :i"), {"i": job_id}
    ).first()
    try:
        body = _order_in(db, j, method, False)
        first = api_order_quote(body)
        options = (first.get("totals") or {}).get("shipping_options") or []
        chosen = method or pick_shipping_option(j.ship_service, options)
        quote = first
        if chosen and chosen != (body.shipping_method or ""):
            body.shipping_method = chosen
            quote = api_order_quote(body)
        quote["options"] = options
        quote["method"] = chosen
        quote["server_mode"] = first.get("mode")
    except HTTPException as e:
        db.rollback()
        msg = ("Quote failed: " + str(e.detail))[:400]
        db.execute(text("update dropship_jobs set problem = :m, updated_at = now() where id = :i"), {"m": msg, "i": job_id})
        db.commit()
        return {"ok": False, "stage": j.stage, "problem": msg}
    db.execute(
        text(
            "update dropship_jobs set stage = 'review_quote', quote = :q, ship_method = :m, problem = null, updated_at = now() where id = :i"
        ),
        {"q": json.dumps(quote, default=str), "m": quote["method"], "i": job_id},
    )
    db.commit()
    return {"ok": True, "stage": "review_quote", "method": quote["method"]}


class QuoteIn(BaseModel):
    method: Optional[str] = Field(None, max_length=60)


@router.post("/jobs/{job_id}/quote")
@needs(*PERMS)
def quote_job(job_id: int, body: QuoteIn, db: Session = Depends(get_db)):
    """Get (or redo) the quote. With a method, that shipping option is used; used for the pick at the quote check."""
    j = _job(db, job_id)
    if j.stage not in ("po_confirmed", "review_quote"):
        raise HTTPException(409, "That order is not at the quote step.")
    return _run_quote(db, job_id, (body.method or "").strip() or None)


@router.post("/jobs/{job_id}/place")
@needs(*PERMS)
def place_job(job_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """Place the order for real. Owners and admins only. The existing safeguards in the order API still apply."""
    from app.autodist import api_order_place

    if user.role not in ADMIN_ROLES:
        raise HTTPException(403, "Only an owner or admin can place the order with the distributor.")
    j = _job(db, job_id)
    if j.stage != "review_quote":
        raise HTTPException(409, "That order is not waiting for the quote check.")
    full = db.execute(text("select id, po_id, ebay_order_id, ship_method from dropship_jobs where id = :i"), {"i": job_id}).first()
    if not full.ship_method:
        raise HTTPException(409, "Pick a shipping option first.")
    body = _order_in(db, full, full.ship_method, True)
    # claim the job first so a double click cannot place it twice
    claimed = db.execute(
        text("update dropship_jobs set stage = 'placing', updated_at = now() where id = :i and stage = 'review_quote' returning id"),
        {"i": job_id},
    ).first()
    db.commit()
    if not claimed:
        raise HTTPException(409, "That order is already being placed.")
    try:
        res = api_order_place(body, db=db, user=user)
    except HTTPException as e:
        db.rollback()
        # A 504 means the order MAY have been placed: keep it out of the normal flow until a person has checked.
        new_stage = "problem" if e.status_code == 504 else "review_quote"
        db.execute(
            text("update dropship_jobs set stage = :s, problem = :m, updated_at = now() where id = :i"),
            {"s": new_stage, "m": ("Not placed: " if new_stage == "review_quote" else "CHECK WITH THE DISTRIBUTOR BEFORE TRYING AGAIN: ") + str(e.detail)[:350], "i": job_id},
        )
        db.commit()
        raise
    if not res.get("placed"):
        db.execute(
            text("update dropship_jobs set stage = 'problem', problem = :m, updated_at = now() where id = :i"),
            {"m": "The distributor did not return an order number: " + str(res.get("message") or "")[:300], "i": job_id},
        )
        db.commit()
        raise HTTPException(502, "The distributor did not return an order number. Check with them before trying again.")
    db.execute(
        text(
            "update dropship_jobs set stage = 'placed', supplier_order = :o, supplier_mode = :m, problem = null, updated_at = now() where id = :i"
        ),
        {"o": str(res.get("order_number")), "m": res.get("mode"), "i": job_id},
    )
    db.commit()
    return {"ok": True, "stage": "placed", "order_number": res.get("order_number"), "mode": res.get("mode")}


# --------------------------------------------------------------------------
# step 6: tracking back, then eBay
# --------------------------------------------------------------------------
def advance_tracking(db: Session) -> int:
    """A placed order whose tracking has reached its eBay order (the Automatic Distributors tracking check does that) moves to
    'review_tracking'. Nothing is sent to eBay here."""
    _ensure(db)
    rows = db.execute(
        text(
            "update dropship_jobs j set stage = 'review_tracking', tracking_number = o.tracking_number, "
            "tracking_carrier = o.tracking_carrier, updated_at = now() "
            "from ebay_orders o where j.stage = 'placed' and o.ebay_order_id = j.ebay_order_id "
            "and coalesce(o.tracking_number, '') <> '' returning j.id"
        )
    ).all()
    db.commit()
    return len(rows)


@router.post("/jobs/{job_id}/send-tracking")
@needs(*PERMS)
def send_tracking(job_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """The tracking is correct: tell eBay the order shipped, with the tracking number. eBay then notifies the buyer."""
    from app.ebay_sales import ShipIn, mark_shipped

    j = _job(db, job_id)
    if j.stage != "review_tracking":
        raise HTTPException(409, "That order is not waiting for the tracking check.")
    r = db.execute(
        text("select j.tracking_number, j.tracking_carrier, o.id as order_pk from dropship_jobs j join ebay_orders o on o.ebay_order_id = j.ebay_order_id where j.id = :i"),
        {"i": job_id},
    ).first()
    if not r or not r.tracking_number:
        raise HTTPException(409, "No tracking number is on that order yet.")
    mark_shipped(r.order_pk, ShipIn(carrier=r.tracking_carrier or "Other", tracking_number=r.tracking_number), db=db, user=user)
    db.execute(text("update dropship_jobs set stage = 'done', updated_at = now() where id = :i"), {"i": job_id})
    db.commit()
    return {"ok": True, "stage": "done"}


@router.post("/jobs/{job_id}/hold")
@needs(*PERMS)
def hold_job(job_id: int, db: Session = Depends(get_db)):
    j = _job(db, job_id)
    if j.stage in ("held", "dismissed"):
        return {"ok": True, "stage": j.stage}
    db.execute(text("update dropship_jobs set stage = 'held', updated_at = now() where id = :i"), {"i": job_id})
    db.commit()
    return {"ok": True, "stage": "held"}


@router.post("/jobs/{job_id}/retry")
@needs(*PERMS)
def retry_job(job_id: int, db: Session = Depends(get_db)):
    j = _job(db, job_id)
    if j.stage != "problem":
        raise HTTPException(409, "Only an order that needs attention can be tried again.")
    db.execute(text("delete from dropship_jobs where id = :i"), {"i": job_id})
    db.commit()
    return {"ok": True}
