"""Scan with a PHONE while working on the computer.

On the computer, a scan box's "Phone" button makes a short-lived PAIRING (a secret link, shown as a QR code). The phone opens that link,
reads barcodes with its camera and sends each one here. The computer's scan box asks every second for new codes and hands each one to the
same place a typed or Zebra-scanned code goes, so a scan lands in the screen (and the step) that made the QR.

  POST   /admin/scan-pair                     computer: start a pairing {token, expires_at}      (signed-in staff with an inventory permission)
  GET    /admin/scan-pair/{token}?after=N     computer: codes sent since id N, and whether the phone has connected
  DELETE /admin/scan-pair/{token}             computer: close the pairing
  GET    /scan-pair/{token}                   phone: is this link still good (no sign-in; the secret link is the permission)
  POST   /scan-pair/{token}/scan              phone: send one code {code}

The link is a random 192-bit secret that only lets whoever holds it SEND CODES into that one pairing. It stops working when the computer
closes it or after PAIR_HOURS. Tables are created at startup (ensure_tables), never in a request.
"""
import logging
import re
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, needs, require_route_access
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)

PAIR_HOURS = 6
MAX_CODE = 200
MAX_SCANS_PER_PAIR = 5000
MAX_SCANS_PER_MINUTE = 120
SCAN_PERMS = ("inventory.receive", "inventory.ship", "inventory.barcodes", "inventory.adjust")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")

# under /admin (main.py): every route needs a @needs(...) marker or it is admin-only
router = APIRouter(prefix="/scan-pair", tags=["admin-scan-pair"], dependencies=[Depends(require_route_access)])
# NOT under /admin: the phone has no sign-in, the secret link is what it holds
public_router = APIRouter(prefix="/scan-pair", tags=["scan-pair-phone"])

_ready = False


def ensure_tables() -> None:
    """Create the two tables if they are missing. Called once at startup, on its own connection."""
    global _ready
    if _ready:
        return
    db = _session_factory()()
    try:
        db.execute(
            text(
                "create table if not exists scan_pair ("
                "token text primary key, user_id text not null, created_at timestamptz not null default now(), "
                "expires_at timestamptz not null, closed boolean not null default false, phone_seen_at timestamptz)"
            )
        )
        db.execute(
            text(
                "create table if not exists scan_pair_scans ("
                "id bigint generated always as identity primary key, token text not null, code text not null, "
                "created_at timestamptz not null default now())"
            )
        )
        db.execute(text("create index if not exists scan_pair_scans_token_idx on scan_pair_scans (token, id)"))
        db.commit()
        _ready = True
    except Exception:
        db.rollback()
        log.exception("could not create the phone scan tables; scanning with a phone will not work until the next restart")
    finally:
        db.close()


def _live(db: Session, token: str):
    """The pairing row if the link is well formed, open and not expired; else None."""
    if not _TOKEN.match(token or ""):
        return None
    return db.execute(
        text("select token, user_id, expires_at from scan_pair where token = :t and not closed and expires_at > now()"), {"t": token}
    ).first()


# --------------------------------------------------------------------------
# the computer
# --------------------------------------------------------------------------
@router.post("")
@needs(*SCAN_PERMS)
def start_pairing(db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """Start a pairing for this signed-in user. Old and finished ones are tidied away here."""
    db.execute(text("delete from scan_pair_scans where token in (select token from scan_pair where expires_at < now() - interval '1 day')"))
    db.execute(text("delete from scan_pair where expires_at < now() - interval '1 day'"))
    token = secrets.token_urlsafe(24)
    row = db.execute(
        text(
            "insert into scan_pair (token, user_id, expires_at) values (:t, :u, now() + make_interval(hours => cast(:h as integer))) returning expires_at"
        ),
        {"t": token, "u": user.user_id, "h": PAIR_HOURS},
    ).first()
    db.commit()
    return {"token": token, "expires_at": row.expires_at.isoformat() if row else None, "hours": PAIR_HOURS}


@router.get("/{token}")
@needs(*SCAN_PERMS)
def poll(token: str, after: int = Query(0, ge=0), db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    """Codes the phone sent after id `after` (oldest first). 404 when the pairing is closed, expired or not this user's."""
    p = _live(db, token)
    if not p or p.user_id != user.user_id:
        raise HTTPException(404, "This phone link has ended. Make a new one.")
    rows = db.execute(
        text("select id, code from scan_pair_scans where token = :t and id > :a order by id limit 100"), {"t": token, "a": after}
    ).all()
    seen = db.execute(text("select phone_seen_at is not null from scan_pair where token = :t"), {"t": token}).scalar()
    return {"scans": [{"id": int(r.id), "code": r.code} for r in rows], "phone_connected": bool(seen)}


@router.delete("/{token}")
@needs(*SCAN_PERMS)
def close(token: str, db: Session = Depends(get_db), user: AuthUser = Depends(require_route_access)):
    db.execute(text("update scan_pair set closed = true where token = :t and user_id = :u"), {"t": token, "u": user.user_id})
    db.commit()
    return {"closed": True}


# --------------------------------------------------------------------------
# the phone
# --------------------------------------------------------------------------
class ScanIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=MAX_CODE * 2)


@public_router.get("/{token}")
def phone_status(token: str, db: Session = Depends(get_db)):
    """The phone page asks this when it opens: is the link still good? Also tells the computer the phone has connected."""
    if not _live(db, token):
        raise HTTPException(410, "This link has ended. Make a new QR code on the computer.")
    db.execute(text("update scan_pair set phone_seen_at = now() where token = :t"), {"t": token})
    db.commit()
    return {"ok": True}


@public_router.post("/{token}/scan")
def phone_scan(token: str, body: ScanIn, db: Session = Depends(get_db)):
    if not _live(db, token):
        raise HTTPException(410, "This link has ended. Make a new QR code on the computer.")
    code = _CONTROL.sub("", body.code).strip()
    if not code or len(code) > MAX_CODE:
        raise HTTPException(400, "That code could not be read.")
    n_all, n_min = db.execute(
        text(
            "select count(*), count(*) filter (where created_at > now() - interval '1 minute') from scan_pair_scans where token = :t"
        ),
        {"t": token},
    ).first()
    if int(n_all) >= MAX_SCANS_PER_PAIR or int(n_min) >= MAX_SCANS_PER_MINUTE:
        raise HTTPException(429, "Too many scans too fast. Wait a moment, or make a new QR code.")
    sid = db.execute(
        text("insert into scan_pair_scans (token, code) values (:t, :c) returning id"), {"t": token, "c": code}
    ).scalar()
    db.execute(text("update scan_pair set phone_seen_at = now() where token = :t"), {"t": token})
    db.commit()
    return {"ok": True, "id": int(sid), "code": code}
