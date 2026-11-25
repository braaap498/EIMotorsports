"""
Amazon seller connection (Selling Partner API), modeled on ebay_oauth.py. Mounted at /admin/amazon-connect/* (owner/admin only).

  GET  /accounts                  the Amazon accounts (never returns a token)
  POST /accounts                  add an account {account, display_name}
  POST /accounts/{account}/token  save the long-lived LWA refresh token (encrypted); checked by a read-only call
  POST /accounts/{account}/test   read-only: asks Amazon for the account's marketplace participations
  PUT  /settings                  per-account switches and numbers (marketplaces, pushes, margin, shipping estimate)

Environment variables on the API:
    AMAZON_LWA_CLIENT_ID, AMAZON_LWA_CLIENT_SECRET   from the app registered in Seller Central / Developer Console
    AMAZON_TOKEN_KEY                                 32 random bytes, url-safe base64 (may reuse the eBay key's format)
Self-authorization (your own seller account) gives a refresh token directly, so no browser redirect is needed here.

Nothing is sent to Amazon except reads until "Live pushes" is turned ON for the account.
"""
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/amazon-connect", tags=["admin-amazon-connect"], dependencies=[Depends(require_admin)])

LWA_URL = "https://api.amazon.com/auth/o2/token"
NA_ENDPOINT = "https://sellingpartnerapi-na.amazon.com"
# North America region covers all four marketplaces.
MARKETPLACES = {"US": "ATVPDKIKX0DER", "CA": "A2EUQ1WTGCTBG2", "MX": "A1AM78C64UM0Y8", "BR": "A2Q3Y263D00KWC"}

_ready = False


def ensure_tables() -> None:
    """Called once at startup (never in a request: creating a table commits the open transaction)."""
    global _ready
    db = _session_factory()()
    try:
        for stmt in (
            """create table if not exists amazon_accounts (
                 account text primary key,
                 display_name text,
                 seller_id text,
                 refresh_token_enc text,
                 authorized_at timestamptz,
                 authorized_by text,
                 live_pushes_enabled boolean not null default false,
                 sales_polling_enabled boolean not null default false,
                 sales_poll_minutes integer not null default 10,
                 sales_qty_push_enabled boolean not null default false,
                 marketplaces text not null default 'US',
                 dropship_policy_ack boolean not null default false,
                 dropship_policy_ack_by text,
                 dropship_policy_ack_at timestamptz,
                 referral_fee_pct numeric not null default 15,
                 target_margin_pct numeric not null default 10,
                 ship_cost_est numeric not null default 0,
                 stand_down_enabled boolean not null default false,
                 created_at timestamptz not null default now(),
                 updated_at timestamptz not null default now()
               )""",
            # per-account buyer-address switch: null = follow AMAZON_BUYER_ADDRESS_ROLE, 'on' / 'off' = override for this account
            "alter table amazon_accounts add column if not exists buyer_address_role text",
        ):
            db.execute(text(stmt))
        db.commit()
        _ready = True
    except Exception:
        db.rollback()
        log.exception("could not set up the Amazon tables")
    finally:
        db.close()


# ---- encryption of the saved key -------------------------------------------------------------
def _fernet() -> Fernet:
    raw = (os.environ.get("AMAZON_TOKEN_KEY") or "").strip()
    if not raw:
        raise HTTPException(503, "AMAZON_TOKEN_KEY is not set on the API.")
    try:
        key = base64.urlsafe_b64decode(raw.encode())
    except Exception:
        key = b""
    if len(key) != 32:
        raise HTTPException(503, "AMAZON_TOKEN_KEY must be 32 random bytes, url-safe base64.")
    return Fernet(base64.urlsafe_b64encode(key))


def _decrypt(v: str) -> str:
    try:
        return _fernet().decrypt(v.encode()).decode()
    except InvalidToken:
        raise HTTPException(500, "The saved Amazon key cannot be read with the current AMAZON_TOKEN_KEY. Save the token again.")


# ---- talking to Amazon -------------------------------------------------------------------------
_access: dict[str, tuple[str, float]] = {}   # account -> (access token, expiry); memory only


def _row(db: Session, account: str):
    r = db.execute(text("select * from amazon_accounts where lower(account) = lower(:a)"), {"a": (account or "").strip()}).first()
    if not r:
        raise HTTPException(404, "That Amazon account is not in the list yet. Add it first.")
    return r


def _lwa(fields: dict) -> dict:
    cid, sec = os.environ.get("AMAZON_LWA_CLIENT_ID"), os.environ.get("AMAZON_LWA_CLIENT_SECRET")
    if not cid or not sec:
        raise HTTPException(503, "AMAZON_LWA_CLIENT_ID and AMAZON_LWA_CLIENT_SECRET are not set on the API.")
    body = urllib.parse.urlencode({**fields, "client_id": cid, "client_secret": sec}).encode()
    req = urllib.request.Request(LWA_URL, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        log.warning("Amazon LWA -> %s", e.code)   # never log the token
        raise HTTPException(502, f"Amazon refused the key ({e.code}). Check the refresh token and app keys.")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise HTTPException(502, "Could not reach Amazon.")


def access_token(db: Session, account: str) -> str:
    row = _row(db, account)
    if not row.refresh_token_enc:
        raise HTTPException(409, f"{row.account} has no Amazon key saved yet.")
    hit = _access.get(row.account)
    if hit and hit[1] > time.time() + 60:
        return hit[0]
    data = _lwa({"grant_type": "refresh_token", "refresh_token": _decrypt(row.refresh_token_enc)})
    tok = data.get("access_token")
    if not tok:
        raise HTTPException(502, "Amazon did not return an access token.")
    _access[row.account] = (tok, time.time() + int(data.get("expires_in") or 3600))
    return tok


def sp_get(db: Session, account: str, path: str, params: Optional[dict] = None) -> dict:
    """One read-only SP-API call. Writes (PATCH/PUT/POST) go through helpers that check live_pushes_enabled first."""
    url = NA_ENDPOINT + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, headers={"x-amz-access-token": access_token(db, account), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        log.warning("Amazon %s -> %s %s", path, e.code, detail)
        raise HTTPException(502, f"Amazon answered with an error ({e.code}) on {path}. {detail[:160]}")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise HTTPException(502, "Could not reach Amazon.")


# ---- routes ------------------------------------------------------------------------------------
@router.get("/accounts")
def accounts(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select account, display_name, seller_id, (refresh_token_enc is not null) as authorized, authorized_at, authorized_by, "
            "live_pushes_enabled, sales_polling_enabled, sales_poll_minutes, sales_qty_push_enabled, marketplaces, "
            "dropship_policy_ack, dropship_policy_ack_by, dropship_policy_ack_at, referral_fee_pct, target_margin_pct, "
            "ship_cost_est, stand_down_enabled, buyer_address_role from amazon_accounts order by account"
        )
    ).all()
    return {"accounts": [dict(r._mapping) for r in rows], "marketplaces": MARKETPLACES}


class AddIn(BaseModel):
    account: str = Field(..., min_length=2, max_length=60)
    display_name: Optional[str] = Field(None, max_length=80)


@router.post("/accounts")
def add_account(body: AddIn, db: Session = Depends(get_db)):
    name = body.account.strip().lower().replace(" ", "-")
    db.execute(
        text("insert into amazon_accounts (account, display_name) values (:a, :d) on conflict (account) do nothing"),
        {"a": name, "d": body.display_name},
    )
    db.commit()
    return {"ok": True, "account": name}


class TokenIn(BaseModel):
    refresh_token: str = Field(..., min_length=20, max_length=2000)
    seller_id: Optional[str] = Field(None, max_length=40)


@router.post("/accounts/{account}/token")
def save_token(account: str, body: TokenIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    row = _row(db, account)
    tok = body.refresh_token.strip()
    _lwa({"grant_type": "refresh_token", "refresh_token": tok})   # refuse (and save nothing) if Amazon rejects it
    db.execute(
        text(
            "update amazon_accounts set refresh_token_enc = :t, seller_id = coalesce(:s, seller_id), authorized_at = now(), "
            "authorized_by = :by, updated_at = now() where account = :a"
        ),
        {"t": _fernet().encrypt(tok.encode()).decode(), "s": (body.seller_id or "").strip() or None, "by": user.email or user.user_id, "a": row.account},
    )
    db.commit()
    _access.pop(row.account, None)
    return {"ok": True, "account": row.account}


@router.post("/accounts/{account}/test")
def test(account: str, db: Session = Depends(get_db)):
    row = _row(db, account)
    data = sp_get(db, row.account, "/sellers/v1/marketplaceParticipations")
    out = []
    for p in data.get("payload") or []:
        m, part = p.get("marketplace") or {}, p.get("participation") or {}
        out.append({"id": m.get("id"), "country": m.get("countryCode"), "name": m.get("name"), "participating": bool(part.get("isParticipating")), "suspended": bool(part.get("hasSuspendedListings"))})
    return {"ok": True, "account": row.account, "marketplaces": out}


class SettingsIn(BaseModel):
    account: str
    live_pushes_enabled: Optional[bool] = None
    sales_polling_enabled: Optional[bool] = None
    sales_poll_minutes: Optional[int] = Field(None, ge=1, le=1440)
    sales_qty_push_enabled: Optional[bool] = None
    marketplaces: Optional[str] = Field(None, max_length=40)
    referral_fee_pct: Optional[float] = Field(None, ge=0, le=45)
    target_margin_pct: Optional[float] = Field(None, ge=0, le=90)
    ship_cost_est: Optional[float] = Field(None, ge=0, le=1000)
    stand_down_enabled: Optional[bool] = None
    channel_id: Optional[int] = Field(None, ge=1)  # the Amazon row of your channels table; turns on writing Amazon sales to sales_transactions
    dropship_policy_ack: Optional[bool] = None
    buyer_address_role: Optional[str] = Field(None, max_length=10)  # "on", "off", or "default" (follow the AMAZON_BUYER_ADDRESS_ROLE env var)


@router.put("/settings")
def put_settings(body: SettingsIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    row = _row(db, body.account)
    sets, p = [], {"a": row.account}
    if body.marketplaces is not None:
        codes = [c for c in body.marketplaces.upper().replace(" ", "").split(",") if c]
        if not codes or any(c not in MARKETPLACES for c in codes):
            raise HTTPException(422, "Marketplaces must be from: " + ", ".join(MARKETPLACES))
        sets.append("marketplaces = :mk")
        p["mk"] = ",".join(codes)
    for field, col in (
        ("live_pushes_enabled", "live_pushes_enabled"), ("sales_polling_enabled", "sales_polling_enabled"),
        ("sales_poll_minutes", "sales_poll_minutes"), ("sales_qty_push_enabled", "sales_qty_push_enabled"),
        ("referral_fee_pct", "referral_fee_pct"), ("target_margin_pct", "target_margin_pct"),
        ("ship_cost_est", "ship_cost_est"), ("stand_down_enabled", "stand_down_enabled"), ("channel_id", "channel_id"),
    ):
        v = getattr(body, field)
        if v is not None:
            if field in ("live_pushes_enabled", "sales_polling_enabled") and v and not row.refresh_token_enc:
                raise HTTPException(409, "Save this account's Amazon key first.")
            if field == "sales_qty_push_enabled" and v and not (row.live_pushes_enabled or body.live_pushes_enabled):
                raise HTTPException(409, "Turn Live pushes ON first (it is the master switch for changing Amazon).")
            sets.append(f"{col} = :{field}")
            p[field] = v
    if body.buyer_address_role is not None:
        v = body.buyer_address_role.strip().lower()
        if v not in ("on", "off", "default"):
            raise HTTPException(422, "Buyer address must be on, off or default.")
        sets.append("buyer_address_role = :bar")
        p["bar"] = None if v == "default" else v
    if body.dropship_policy_ack is not None:
        sets.append("dropship_policy_ack = :ack, dropship_policy_ack_by = :ackby, dropship_policy_ack_at = case when :ack then now() end")
        p["ack"], p["ackby"] = body.dropship_policy_ack, (user.email or user.user_id) if body.dropship_policy_ack else None
    if sets:
        db.execute(text(f"update amazon_accounts set {', '.join(sets)}, updated_at = now() where account = :a"), p)
        db.commit()
    return {"ok": True}
