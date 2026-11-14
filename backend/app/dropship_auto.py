"""
Automatic drop shipping, step 1: an eBay order that has to be drop shipped makes its purchase order by itself,
then WAITS for a person to look at the PO and confirm it before anything goes further.

Chain (a temporary review stop at every step while it is being proved out):
  1. eBay order paid        -> drop ship PO made (buyer name, address, and the shipping service the buyer picked)
  2. review_po   a person checks the PO              <- BUILT (this file)
  3. quote       PO sent to Automatic Distributors for price and shipping options   (next)
  4. review_quote a person checks the quote           (next)
  5. place       order placed for real                (next)
  6. tracking goes on the order and to eBay           (tracking and the eBay update already exist)

Nothing runs by itself until "Make POs automatically" is switched on (default OFF).
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, needs, require_route_access
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
    ):
        db.execute(text(stmt))
    db.commit()
    _ready = True


def _settings(db: Session) -> dict:
    _ensure(db)
    r = db.execute(text("select auto_po, last_scan_at, last_scan_note from dropship_settings where id = 1")).first()
    return {"auto_po": bool(r.auto_po), "last_scan_at": r.last_scan_at, "last_scan_note": r.last_scan_note}


def _flagged_short_lines(db: Session, order_id: str) -> list[int]:
    """Products on this eBay order that are marked Drop ship and not fully in stock on our shelf."""
    from app.admin_purchase_orders import _ebay_order_and_lines

    _o, lines = _ebay_order_and_lines(db, order_id)
    ids = [l.product_id for l in lines if l.product_id is not None]
    if not ids:
        return []
    flagged = {
        r.id
        for r in db.execute(
            text(
                "select p.id from products p where p.id = any(cast(:ids as bigint[])) and (coalesce(p.dropship_ok, false) or exists "
                "(select 1 from channel_listings cl where cl.product_id = p.id and coalesce(cl.dropship_ok, false)))"
            ),
            {"ids": ids},
        ).all()
    }
    return sorted({l.product_id for l in lines if l.product_id in flagged and int(l.on_hand or 0) < int(l.quantity or 1)})


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
            "select id, ebay_order_id, store, po_id, po_number, stage, ship_service, buyer_name, problem, created_at, confirmed_by, confirmed_at "
            "from dropship_jobs " + ("where stage = :s " if stage else "where stage <> 'dismissed' ") + "order by id desc limit 100"
        ),
        {"s": stage} if stage else {},
    ).all()
    out = []
    for r in rows:
        j = dict(r._mapping)
        if r.po_id and r.stage in ("review_po", "po_confirmed"):
            try:
                j["po"] = _po_detail(db, r.po_id)
            except HTTPException:
                j["po"] = None
        out.append(j)
    return {"jobs": out}


@router.post("/scan")
@needs(*PERMS)
def scan_now(db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    return scan_new_orders(db, user)


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
    picks = _flagged_short_lines(db, o.ebay_order_id)
    if not picks:
        raise HTTPException(
            409,
            "Nothing on that order needs drop shipping: no line is a product marked Drop ship that is out of stock on our shelf.",
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
    return {"ok": True, "stage": "po_confirmed"}


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
