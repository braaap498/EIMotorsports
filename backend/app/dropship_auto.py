"""
Automatic drop shipping, step 1: an eBay order that has to be drop shipped makes its purchase order by itself,
then WAITS for a person to look at the PO and confirm it before anything goes further.

Chain. The switch "Manual checks" (Drop ship page, default OFF) decides whether each step has a temporary review stop:
  OFF = everything below runs by itself, one step after another. ON = it stops at the three review steps (2, 4, 6) for a person.
  1. eBay order paid        -> drop ship PO made (buyer name, address, and the shipping service the buyer picked)
  2. review_po   a person checks the PO              <- BUILT (this file)
  3. quote       confirming the PO gets a quote from Automatic Distributors; the shipping option closest to the buyer's choice is picked
  4. review_quote a person checks the quote (and can change the shipping option), then places the order (owner/admin only)
  5. place       the order is placed through the order API (the existing safeguards still apply: test/live server, live switch)
  6. review_tracking tracking comes back (existing automatic check); a person checks it, then eBay is told it shipped, with the
                  tracking number, and eBay notifies the buyer

A listing ticked Drop ship is drop shipped automatically, whatever our shelf holds (and only a product whose parts are all matched to
the distributor catalog can be ticked). An unticked listing that is short on our shelf asks a person on Pick & ship instead.
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
        "alter table dropship_settings add column if not exists auto_ship boolean not null default false",
        "alter table dropship_settings add column if not exists manual_checks boolean not null default false",
        "alter table dropship_jobs add column if not exists tries integer not null default 0",
        "alter table dropship_jobs add column if not exists note text",
        "alter table dropship_jobs add column if not exists placed_at timestamptz",
        "alter table dropship_jobs add column if not exists shipped_at timestamptz",
        "create index if not exists dropship_jobs_created_idx on dropship_jobs (created_at)",
        "create index if not exists dropship_jobs_placed_idx on dropship_jobs (placed_at)",
        "create index if not exists dropship_jobs_shipped_idx on dropship_jobs (shipped_at)",
        "create index if not exists dropship_jobs_stage_idx on dropship_jobs (stage)",
    ):
        db.execute(text(stmt))
    db.commit()
    _ready = True
    _backfill_line_states(db)


def _backfill_line_states(db: Session) -> None:
    """Once per start: eBay order lines of orders that already have a drop ship PO move from Set aside to Drop ship (only lines still
    set aside are touched, so this does nothing once they have moved)."""
    try:
        from app import ebay_sales

        rows = db.execute(
            text(
                "select j.ebay_order_id, array_agg(distinct pl.product_id) as pids from dropship_jobs j "
                "join po_headers h on h.id = j.po_id and h.status <> 'cancelled' "
                "join po_lines pl on pl.po_id = h.id and pl.product_id is not null "
                "where j.stage not in ('dismissed', 'problem') group by j.ebay_order_id"
            )
        ).all()
        for r in rows:
            ebay_sales.mark_lines_dropship(db, r.ebay_order_id, list(r.pids or []))
    except Exception:
        db.rollback()
        log.exception("could not move existing drop ship orders' lines to the Drop ship state")


def _settings(db: Session) -> dict:
    _ensure(db)
    r = db.execute(text("select manual_checks, last_scan_at, last_scan_note from dropship_settings where id = 1")).first()
    return {"manual_checks": bool(r.manual_checks), "last_scan_at": r.last_scan_at, "last_scan_note": r.last_scan_note}


def _flagged_short_lines(db: Session, order_id: str, honor_decision: bool = False) -> list[int]:
    """Products on this eBay order that are to be drop shipped: the listing the buyer bought from is ticked Drop ship (our shelf does
    not matter), and the product is matched to the distributor's catalog. A flag on another listing of the same product does not
    count. An UNticked listing is never made automatically: when it is short on our shelf Pick & ship asks a person. With
    honor_decision (the manual button only), an order a person approved as 'drop ship' there counts too, if it is short on our shelf."""
    from app import fulfillment
    from app.admin_purchase_orders import _ebay_order_and_lines
    from app.availability import has_distributor

    _o, lines = _ebay_order_and_lines(db, order_id)
    if not any(l.product_id is not None for l in lines):
        return []
    flags = fulfillment.ebay_line_dropship_flags(db, [_o.ebay_order_id])
    approved = False
    if honor_decision:
        approved = (fulfillment.decisions_for(db, [fulfillment.key_of("eBay", _o.ebay_order_id)]).get(fulfillment.key_of("eBay", _o.ebay_order_id)) or {}).get("decision") == "dropship"
    picks = {
        int(l.product_id)
        for l in lines
        if l.product_id is not None
        and (flags.get((_o.ebay_order_id, int(l.product_id))) or (approved and int(l.on_hand or 0) < int(l.quantity or 1)))
    }
    # a product without a matched distributor part cannot be drop shipped: it stays with Pick & ship
    return sorted(pid for pid in picks if has_distributor(db, pid))


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
    """Called by the eBay sales timer after it has fetched orders: new orders for ticked listings make their PO, and every waiting
    drop ship order moves on."""
    db = _session_factory()()
    try:
        scan_new_orders(db)
        run_automatic(db)
    except Exception:
        db.rollback()
        log.exception("automatic drop ship check failed")
    finally:
        db.close()


class SettingsIn(BaseModel):
    manual_checks: Optional[bool] = None  # ON = stop for a person at the PO, quote and tracking steps; OFF (default) = all automatic


@router.get("/settings")
@needs(*PERMS)
def get_settings(db: Session = Depends(get_db)):
    return _settings(db)


@router.put("/settings")
@needs(*PERMS)
def put_settings(body: SettingsIn, db: Session = Depends(get_db)):
    _ensure(db)
    if body.manual_checks is not None:
        db.execute(text("update dropship_settings set manual_checks = :a where id = 1"), {"a": body.manual_checks})
    db.commit()
    return _settings(db)


@router.get("/jobs")
@needs(*PERMS)
def list_jobs(stage: Optional[str] = Query(None, max_length=20), db: Session = Depends(get_db)):
    from app.admin_purchase_orders import _po_detail

    _ensure(db)
    rows = db.execute(
        text(
            "select j.id, j.ebay_order_id, j.store, j.po_id, coalesce(h.po_number, j.po_number) as po_number, j.stage, j.ship_service, "
            "j.buyer_name, j.problem, j.created_at, j.confirmed_by, j.confirmed_at, "
            "j.quote, j.ship_method, j.supplier_order, j.supplier_mode, j.tracking_number, j.tracking_carrier, j.note, j.tries "
            "from dropship_jobs j left join po_headers h on h.id = j.po_id "
            + ("where j.stage in ('review_po', 'po_confirmed', 'review_quote', 'review_tracking') " if stage == "review"
               else "where j.stage = :s " if stage else "where j.stage <> 'dismissed' ")
            + "order by j.id desc limit 100"
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
    out["automatic"] = run_automatic(db)
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
    run_automatic(db)
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


# eBay shipping services that always go out on one specific distributor option (compared with letters and digits only).
# USPSParcel is the free shipping service on the listings: it is shipped as USPS Ground Advantage.
FIXED_OPTIONS = {"uspsparcel": "uspsgroundadvantage"}


def pick_shipping_option(buyer_service: Optional[str], options: list[dict]) -> Optional[str]:
    """The distributor's shipping option closest to the buyer's eBay choice, or None when there is no safe match
    (a person then picks it at the quote check). A match needs the same speed; the same carrier is preferred.
    A service in FIXED_OPTIONS uses its own option when the distributor offers it."""
    if not buyer_service or not options:
        return None
    fixed = FIXED_OPTIONS.get(_norm(buyer_service))
    if fixed:
        hits = [str(o.get("name") or "") for o in options if _norm(str(o.get("name") or "")) == fixed]
        if hits:
            return hits[0]
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


def method_code(method: Optional[str], options: list) -> str:
    """What the order API wants as the shipping method: the option's carrier CODE (the same value the Orders screen sends), not its
    display name. `method` may be a name (what the page shows and saves) or already a code; anything unknown is passed through."""
    m = (method or "").strip()
    if not m:
        return ""
    for o in options or []:
        if not isinstance(o, dict):
            continue
        name, code = str(o.get("name") or "").strip(), str(o.get("carrier_code") or "").strip()
        if m == name or m == code:
            return code or name
    return m


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
        fallback = False
        if not chosen and options and not _settings(db)["manual_checks"]:
            # no option matches what the buyer picked: with Manual checks OFF use the cheapest the distributor offers
            def _cost(o):
                try:
                    return float(o.get("cost"))
                except (TypeError, ValueError):
                    return 1e9

            named = [o for o in options if isinstance(o, dict) and o.get("name")]
            if named:
                chosen = str(min(named, key=_cost)["name"])
                fallback = True
        quote = first
        code = method_code(chosen, options)  # the order API takes the option's code, not its display name
        if code and code != (body.shipping_method or ""):
            body.shipping_method = code
            quote = api_order_quote(body)
        quote["options"] = options
        quote["method"] = chosen
        quote["fallback"] = fallback
        quote["server_mode"] = first.get("mode")
    except HTTPException as e:
        db.rollback()
        msg = ("Quote failed: " + str(e.detail))[:400]
        db.execute(text("update dropship_jobs set problem = :m, updated_at = now() where id = :i"), {"m": msg, "i": job_id})
        db.commit()
        return {"ok": False, "stage": j.stage, "problem": msg}
    db.execute(
        text(
            "update dropship_jobs set stage = 'review_quote', quote = :q, ship_method = :m, problem = null, note = :n, updated_at = now() where id = :i"
        ),
        {
            "q": json.dumps(quote, default=str), "m": quote["method"], "i": job_id,
            "n": (f"No option matched the buyer's choice ({j.ship_service or 'none given'}); the cheapest, {quote['method']}, was used." if quote.get("fallback") else None),
        },
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
    full = db.execute(
        text(
            "select j.id, j.po_id, j.ebay_order_id, j.ship_method, j.quote, coalesce(cast(h.po_number as text), cast(j.po_number as text)) as po_number_text "
            "from dropship_jobs j left join po_headers h on h.id = j.po_id where j.id = :i"
        ),
        {"i": job_id},
    ).first()
    if not full.ship_method:
        raise HTTPException(409, "Pick a shipping option first.")
    try:
        saved_options = (json.loads(full.quote) if full.quote else {}).get("options") or []
    except ValueError:
        saved_options = []
    body = _order_in(db, full, method_code(full.ship_method, saved_options), True)
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
        detail = str(e.detail)
        low = detail.lower()
        if new_stage == "review_quote" and "po number" in low and ("exist" in low or "duplicate" in low):
            msg = (
                f"Not placed: the distributor already has an order with PO number {full.po_number_text}. Nothing was ordered. "
                "Give this PO a new, unused number in the purchase order editor, then place it again."
            )
        else:
            msg = ("Not placed: " if new_stage == "review_quote" else "CHECK WITH THE DISTRIBUTOR BEFORE TRYING AGAIN: ") + detail[:350]
        db.execute(
            text("update dropship_jobs set stage = :s, problem = :m, updated_at = now() where id = :i"),
            {"s": new_stage, "m": msg[:400], "i": job_id},
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
            "update dropship_jobs set stage = 'placed', supplier_order = :o, supplier_mode = :m, problem = null, placed_at = now(), updated_at = now() where id = :i"
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
    'review_tracking'. With "Manual checks" OFF it is then sent to eBay straight away; with it ON
    nothing is sent here and a person checks it first."""
    _ensure(db)
    rows = db.execute(
        text(
            "update dropship_jobs j set stage = 'review_tracking', problem = null, tracking_number = o.tracking_number, "
            "tracking_carrier = o.tracking_carrier, updated_at = now() "
            "from ebay_orders o where j.stage = 'placed' and o.ebay_order_id = j.ebay_order_id "
            "and coalesce(o.tracking_number, '') <> '' returning j.id"
        )
    ).all()
    db.commit()
    if rows and not _settings(db)["manual_checks"]:
        for r in rows:
            try:
                _send_tracking_now(db, r.id, SYSTEM_USER)
            except Exception as e:
                db.rollback()
                msg = str(getattr(e, "detail", e))[:300]
                log.warning("automatic send of tracking to eBay failed for job %s: %s", r.id, msg)
                db.execute(
                    text("update dropship_jobs set problem = :m, updated_at = now() where id = :i"),
                    {"m": "Tracking was NOT sent to eBay automatically: " + msg + " Check it here and send it yourself.", "i": r.id},
                )
                db.commit()
    return len(rows)


def run_automatic(db: Session) -> dict:
    """With "Manual checks" OFF, carry every waiting drop ship order through the review steps by itself:
    PO check -> quote -> place the order -> (tracking, handled by advance_tracking) -> tell eBay.
    An order that has a problem noted (a failed quote, a refused order, eBay refusing the tracking) is NOT retried here; it waits for a
    person, so nothing is hammered or ordered twice. Order placing keeps every existing safeguard (test/live server, live switch,
    duplicate PO number)."""
    _ensure(db)
    if _settings(db)["manual_checks"]:
        return {"manual": True}
    done = {"confirmed": 0, "placed": 0, "stopped": 0}
    for _ in range(2):  # a second pass lets an order that was just confirmed continue to the quote and the order in the same run
        ids = db.execute(
            text("select id, stage, ship_method from dropship_jobs where stage in ('review_po', 'review_quote') and problem is null order by id limit 25")
        ).all()
        if not ids:
            break
        for r in ids:
            try:
                if r.stage == "review_po":
                    confirm_po(r.id, db=db, user=SYSTEM_USER)  # confirms, then gets the quote
                    done["confirmed"] += 1
                elif r.stage == "review_quote":
                    if not r.ship_method:
                        db.execute(
                            text("update dropship_jobs set problem = 'No shipping option matched the buyer''s choice. Pick one and place the order yourself.', updated_at = now() where id = :i"),
                            {"i": r.id},
                        )
                        db.commit()
                        done["stopped"] += 1
                        continue
                    place_job(r.id, db=db, user=SYSTEM_USER)
                    done["placed"] += 1
            except HTTPException as e:
                db.rollback()
                done["stopped"] += 1
                log.warning("automatic drop ship step failed for job %s: %s", r.id, e.detail)
                # place_job and the quote already record their own reason; make sure there is one so the loop does not retry it
                db.execute(
                    text("update dropship_jobs set problem = coalesce(problem, :m), updated_at = now() where id = :i and stage in ('review_po', 'review_quote')"),
                    {"m": ("Automatic step stopped: " + str(e.detail))[:400], "i": r.id},
                )
                db.commit()
            except Exception:
                db.rollback()
                log.exception("automatic drop ship step crashed for job %s", r.id)
                db.execute(
                    text("update dropship_jobs set problem = coalesce(problem, 'Automatic step crashed; check the log and do it yourself.'), updated_at = now() where id = :i and stage in ('review_po', 'review_quote')"),
                    {"i": r.id},
                )
                db.commit()
    done["retried"] = _retry_failures(db)
    done["tracking_sent"] = advance_tracking(db)
    _flag_freight(db)
    return done


MAX_TRIES = 3


def _retry_failures(db: Session) -> int:
    """With Manual checks OFF, a quote that failed or a tracking number eBay refused is tried again on the next checks, up to
    MAX_TRIES times in all (they are often a passing glitch). The order itself is never re-sent to the distributor here."""
    n = 0
    for r in db.execute(
        text(
            "select id, stage, problem from dropship_jobs where tries < :m and problem is not null and "
            "((stage in ('review_po', 'po_confirmed') and problem like 'Quote failed%') or "
            " (stage = 'review_tracking' and problem like 'Tracking was NOT sent%')) order by id limit 20"
        ),
        {"m": MAX_TRIES},
    ).all():
        db.execute(text("update dropship_jobs set tries = tries + 1, updated_at = now() where id = :i"), {"i": r.id})
        db.commit()
        n += 1
        try:
            if r.stage == "review_tracking":
                _send_tracking_now(db, r.id, SYSTEM_USER)
            else:
                _run_quote(db, r.id, None)
        except Exception as e:
            db.rollback()
            msg = str(getattr(e, "detail", e))[:300]
            if r.stage == "review_tracking":
                db.execute(
                    text("update dropship_jobs set problem = :m where id = :i"),
                    {"m": f"Tracking was NOT sent to eBay automatically: {msg} Check it and send it yourself.", "i": r.id},
                )
                db.commit()
    return n


def _flag_freight(db: Session) -> None:
    """A placed order whose only tracking is a freight PRO number (never a parcel number eBay accepts) would wait forever.
    Say so on the order, so a person can enter the carrier and tracking number by hand."""
    try:
        rows = db.execute(
            text(
                "select j.id, so.tracking from dropship_jobs j join supplier_orders so on so.order_number = j.supplier_order and so.supplier = 'autodist' "
                "where j.stage = 'placed' and j.problem is null and so.tracking is not null"
            )
        ).all()
    except Exception:
        db.rollback()
        return
    for r in rows:
        try:
            items = r.tracking if isinstance(r.tracking, list) else json.loads(r.tracking or "[]")
        except ValueError:
            continue
        if not isinstance(items, list):
            continue
        pro = next((i for i in items if isinstance(i, dict) and i.get("kind") == "pro" and i.get("number")), None)
        if pro and not any(isinstance(i, dict) and i.get("kind") == "tracking" and i.get("number") for i in items):
            db.execute(
                text("update dropship_jobs set problem = :m, updated_at = now() where id = :i and stage = 'placed' and problem is null"),
                {"m": f"Shipping by freight (PRO number {str(pro['number'])[:30]}): there is no parcel tracking number eBay accepts. Enter the carrier and tracking number yourself.", "i": r.id},
            )
    db.commit()


def _send_tracking_now(db: Session, job_id: int, user: AuthUser) -> None:
    """Tell eBay the order shipped with this job's tracking number (eBay then notifies the buyer), and mark the job done."""
    from app.ebay_sales import ShipIn, mark_shipped

    r = db.execute(
        text("select j.tracking_number, j.tracking_carrier, o.id as order_pk from dropship_jobs j join ebay_orders o on o.ebay_order_id = j.ebay_order_id where j.id = :i"),
        {"i": job_id},
    ).first()
    if not r or not r.tracking_number:
        raise HTTPException(409, "No tracking number is on that order yet.")
    mark_shipped(r.order_pk, ShipIn(carrier=r.tracking_carrier or "Other", tracking_number=r.tracking_number), db=db, user=user)
    db.execute(text("update dropship_jobs set stage = 'done', problem = null, shipped_at = now(), updated_at = now() where id = :i"), {"i": job_id})
    db.commit()


@router.post("/jobs/{job_id}/send-tracking")
@needs(*PERMS)
def send_tracking(job_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """The tracking is correct: tell eBay the order shipped, with the tracking number. eBay then notifies the buyer."""
    j = _job(db, job_id)
    if j.stage != "review_tracking":
        raise HTTPException(409, "That order is not waiting for the tracking check.")
    _send_tracking_now(db, job_id, user)
    return {"ok": True, "stage": "done"}


@router.get("/health")
@needs(*PERMS)
def health(db: Session = Depends(get_db)):
    """Warnings for the top of the Drop ship page: things that stop a ticked listing from running by itself."""
    import os

    from app import autodist

    _ensure(db)
    warnings: list[str] = []
    try:
        ticked = db.execute(
            text(
                "select cl.store, count(*) as n from channel_listings cl join products p on p.id = cl.product_id "
                "where (coalesce(cl.dropship_ok, false) or coalesce(p.dropship_ok, false)) group by cl.store"
            )
        ).all()
    except Exception:
        db.rollback()
        ticked = []
    total = sum(int(r.n) for r in ticked)
    if total:
        # 1. the automatic tracking check
        try:
            autodist._ensure_tracking_settings(db)
            cfg = db.execute(
                text("select tracking_enabled, tracking_interval_minutes from supplier_sync_settings where supplier = :s"), {"s": autodist.SUPPLIER}
            ).first()
            if cfg is None or not cfg.tracking_enabled:
                warnings.append(
                    "The automatic tracking check is OFF (Distributor data feed tab). Tracking will not come back by itself, so eBay will not be told the orders shipped."
                )
        except Exception:
            db.rollback()
        # 2. the eBay order check, store by store
        try:
            accts = {
                (r.store or "").lower(): r
                for r in db.execute(text("select store, sales_polling_enabled, refresh_token_enc is not null as connected from ebay_accounts")).all()
            }
            for r in ticked:
                a = accts.get((r.store or "").lower())
                if a is None or not a.connected:
                    warnings.append(f"{r.store}: the store is not connected to eBay, so its orders are never checked.")
                elif not a.sales_polling_enabled:
                    warnings.append(f"{r.store}: the eBay order check is OFF. New orders will not be seen, so nothing there will drop ship by itself.")
        except Exception:
            db.rollback()
        # 3. ticked but no distributor part
        try:
            from app.availability import linked_sql

            bad = db.execute(
                text(f"select count(*) from products p where (coalesce(p.dropship_ok, false) or exists (select 1 from channel_listings c where c.product_id = p.id and c.dropship_ok)) and not coalesce({linked_sql('p')}, false)")
            ).scalar()
            if bad:
                warnings.append(f"{int(bad)} product(s) are ticked Drop ship but have no distributor part linked. Their orders cannot drop ship; they go to Pick & ship.")
        except Exception:
            db.rollback()
        # 4. real orders switched off
        try:
            url, _key = autodist._api_settings()
            if autodist._api_mode(url) == "live" and os.environ.get("AUTOMATIC_DIST_ALLOW_LIVE_ORDERS", "").strip().lower() not in ("1", "true", "yes"):
                warnings.append("This is the live server and real orders are switched off. Drop ship orders will stop at the order step until AUTOMATIC_DIST_ALLOW_LIVE_ORDERS=1 is set on Render.")
        except Exception:
            db.rollback()
    return {"ticked": total, "warnings": warnings}


class CarrierTrackingIn(BaseModel):
    carrier: str = Field(..., min_length=2, max_length=20)
    tracking_number: str = Field(..., min_length=6, max_length=80)


@router.post("/jobs/{job_id}/tracking")
@needs(*PERMS)
def enter_tracking(job_id: int, body: CarrierTrackingIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """For an order the distributor shipped by freight (or whose tracking never came back): a person types the carrier and tracking
    number, and eBay is told it shipped with them."""
    import re

    j = _job(db, job_id)
    if j.stage not in ("placed", "review_tracking"):
        raise HTTPException(409, "That order has not been placed with the distributor, or is already finished.")
    num = re.sub(r"\s+", "", body.tracking_number)
    db.execute(
        text("update dropship_jobs set tracking_number = :t, tracking_carrier = :c, stage = 'review_tracking', problem = null, updated_at = now() where id = :i"),
        {"t": num, "c": body.carrier.strip(), "i": job_id},
    )
    db.commit()
    try:
        _send_tracking_now(db, job_id, user)
    except HTTPException as e:
        db.rollback()
        db.execute(text("update dropship_jobs set problem = :m where id = :i"), {"m": f"eBay did not accept that tracking: {str(e.detail)[:300]}", "i": job_id})
        db.commit()
        raise
    return {"ok": True, "stage": "done"}


@router.post("/jobs/{job_id}/ship-from-stock")
@needs(*PERMS)
def ship_from_stock(job_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """The way out for an order that cannot be drop shipped (Global Shipping Program, incomplete address, no matching shipping option,
    a refused order): cancel its PO if it has one, put its lines back on our shelf, and hand the order to Pick & ship as 'ship from
    our own stock'. Not possible once the distributor has the order."""
    from app import fulfillment
    from app.admin_purchase_orders import CancelIn, cancel_po
    from app.ebay_sales import undo_lines_dropship

    j = db.execute(text("select id, stage, po_id, ebay_order_id from dropship_jobs where id = :i"), {"i": job_id}).first()
    if not j:
        raise HTTPException(404, "That drop ship order is not in the list.")
    if j.stage in ("placing", "placed", "review_tracking", "done", "dismissed"):
        raise HTTPException(409, "The distributor already has this order, so it cannot be moved to our own stock from here.")
    pids: list[int] = []
    if j.po_id:
        pids = [int(r.product_id) for r in db.execute(text("select product_id from po_lines where po_id = :p and product_id is not null"), {"p": j.po_id}).all()]
        st = db.execute(text("select status from po_headers where id = :p"), {"p": j.po_id}).scalar()
        if st and st != "cancelled":
            cancel_po(j.po_id, CancelIn(reason=f"Shipping eBay order {j.ebay_order_id} from our own stock instead"), db=db, user=user)
    if pids:
        undo_lines_dropship(db, j.ebay_order_id, pids)
    fulfillment.record_decision(db, "eBay", j.ebay_order_id, "stock", user.email, ["Could not be drop shipped; shipping from our own stock"])
    db.execute(text("update dropship_jobs set stage = 'dismissed', updated_at = now() where id = :i"), {"i": job_id})
    db.commit()
    return {"ok": True}


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


# --------------------------------------------------------------------------
# activity: what drop shipped in a time frame (built to stay quick with thousands of orders a day: every number is counted
# in the database over an indexed time window, and the list is paged)
# --------------------------------------------------------------------------
_PLACED = "coalesce(j.placed_at, case when j.stage in ('placed', 'review_tracking', 'done') then j.updated_at end)"
_SHIPPED = "coalesce(j.shipped_at, case when j.stage = 'done' then j.updated_at end)"
_BASIS = {"received": "j.created_at", "placed": _PLACED, "shipped": _SHIPPED}
_WAITING_STAGES = ("review_po", "po_confirmed", "review_quote", "placing", "placed", "review_tracking", "held", "problem")


def _when(raw: str, default):
    from datetime import datetime, timezone

    if not (raw or "").strip():
        return default
    try:
        d = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "The dates must look like 2026-11-15T09:30:00Z.")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@router.get("/activity")
@needs(*PERMS)
def activity(
    since: str = "",
    until: str = "",
    basis: str = Query("received", pattern="^(received|placed|shipped)$"),
    stage: str = "",
    q: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Drop ship orders in a time frame. since / until are ISO date-times (the page sends its own local day and hour boundaries).
    basis says which moment decides if an order is in the frame: received (the eBay order reached us), placed (we placed it with the
    distributor) or shipped (eBay was told it shipped). The summary counts all three for the frame, plus what is waiting right now."""
    from datetime import datetime, timedelta, timezone

    _ensure(db)
    now = datetime.now(timezone.utc)
    until_d = _when(until, now)
    since_d = _when(since, until_d - timedelta(hours=24))
    if since_d >= until_d:
        raise HTTPException(422, "The start of the time frame must be before its end.")
    params = {"s": since_d, "u": until_d}
    # the three moments all fall within weeks of an order being received, which keeps every scan on the indexed column
    bound = "j.created_at >= cast(:s as timestamptz) - interval '90 days' and j.stage <> 'dismissed'"
    summ = db.execute(
        text(
            f"""
            select count(*) filter (where j.created_at >= :s and j.created_at < :u) as received,
                   count(*) filter (where {_PLACED} >= :s and {_PLACED} < :u) as placed,
                   count(*) filter (where {_SHIPPED} >= :s and {_SHIPPED} < :u) as shipped,
                   count(*) filter (where j.stage = 'problem' and j.updated_at >= :s and j.updated_at < :u) as problems
            from dropship_jobs j where {bound}
            """
        ),
        params,
    ).first()
    waiting = {
        r.stage: int(r.n)
        for r in db.execute(
            text("select stage, count(*) as n from dropship_jobs where stage = any(cast(:st as text[])) group by stage"),
            {"st": list(_WAITING_STAGES)},
        ).all()
    }
    col = _BASIS[basis]
    where = [bound, f"{col} >= :s", f"{col} < :u"]
    if stage.strip():
        where.append("j.stage = :stage")
        params["stage"] = stage.strip()
    if q.strip():
        where.append(
            "(j.ebay_order_id ilike :like or j.buyer_name ilike :like or cast(coalesce(h.po_number, j.po_number) as text) ilike :like "
            "or j.supplier_order ilike :like or j.tracking_number ilike :like)"
        )
        params["like"] = f"%{q.strip()}%"
    w = " and ".join(where)
    frm = "from dropship_jobs j left join po_headers h on h.id = j.po_id"
    total = db.execute(text(f"select count(*) {frm} where {w}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            select j.id, j.ebay_order_id, j.store, coalesce(h.po_number, j.po_number) as po_number, j.stage, j.buyer_name, j.ship_service,
                   j.supplier_order, j.supplier_mode, j.tracking_number, j.tracking_carrier, j.problem, j.created_at,
                   {_PLACED} as placed_at, {_SHIPPED} as shipped_at,
                   (select coalesce(sum(l.quantity), 0) from ebay_order_lines l where l.ebay_order_id = j.ebay_order_id) as units,
                   (select min(coalesce(l.title, '')) from ebay_order_lines l where l.ebay_order_id = j.ebay_order_id) as title
            {frm} where {w} order by {col} desc nulls last, j.id desc limit :lim offset :off
            """
        ),
        {**params, "lim": limit, "off": offset},
    ).all()
    return {
        "since": since_d,
        "until": until_d,
        "basis": basis,
        "summary": {
            "received": int(summ.received or 0), "placed": int(summ.placed or 0), "shipped": int(summ.shipped or 0),
            "problems": int(summ.problems or 0), "waiting": waiting,
        },
        "total": int(total or 0),
        "rows": [dict(r._mapping) for r in rows],
    }
