"""
eBay seller approval ("Connect eBay") for the Live listings tab.

Mounted at /admin/ebay-connect/* (owner/admin only).

  POST /start        {store}        -> {url}  the eBay consent page to send the browser to
  POST /callback     {code, state}  -> the one-time code eBay sent back is swapped for a long-lived
                                       key, checked against the store, encrypted and saved
  POST /disconnect   {store}        -> forgets the saved key
  POST /test         {store}        -> read-only calls that prove the connection works

No password or key is ever typed into the site. The long-lived key (about 18 months) is stored
encrypted in ebay_accounts.refresh_token_enc and never leaves the API.

Settings, as environment variables on the API:
    EBAY_CLIENT_ID, EBAY_CLIENT_SECRET   production keys (already set)
    EBAY_RUNAME                          the redirect name from eBay's User Tokens page
    EBAY_TOKEN_KEY                       32 random bytes, url-safe base64 (encrypts saved keys)
    EBAY_SCOPES                          optional; overrides the permission list below
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin
from app.database import get_db
from app.ebay_feedback import FeedbackError, configured, seller_username

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-connect", tags=["admin-ebay-connect"], dependencies=[Depends(require_admin)])

MAIN_STORE = "eandimotorsports"
AUTH_URL = "https://auth.ebay.com/oauth2/authorize"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
IDENTITY_URL = "https://apiz.ebay.com/commerce/identity/v1/user/"
PRIVILEGE_URL = "https://api.ebay.com/sell/account/v1/privilege"

# Asked for once now, because adding a permission later means every owner approves again.
DEFAULT_SCOPES = (
    "https://api.ebay.com/oauth/api_scope "
    "https://api.ebay.com/oauth/api_scope/sell.inventory "
    "https://api.ebay.com/oauth/api_scope/sell.account "
    "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly"
)
STATE_MINUTES = 15


def _scopes() -> str:
    return " ".join((os.environ.get("EBAY_SCOPES") or DEFAULT_SCOPES).split())


# --------------------------------------------------------------------------
# encryption and the signed "state" value
# --------------------------------------------------------------------------
def _key_bytes() -> bytes:
    raw = (os.environ.get("EBAY_TOKEN_KEY") or "").strip()
    if not raw:
        raise HTTPException(503, "EBAY_TOKEN_KEY is not set on the API.")
    try:
        key = base64.urlsafe_b64decode(raw.encode())
    except Exception:
        key = b""
    if len(key) != 32:
        raise HTTPException(503, "EBAY_TOKEN_KEY is not a valid key (it must be 32 random bytes, url-safe base64).")
    return key


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_key_bytes()))


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        raise HTTPException(
            500, "The saved eBay key cannot be read with the current EBAY_TOKEN_KEY. Disconnect the store and approve it again."
        )


def _sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(_key_bytes() + b"|state", body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def _unsign(value: str) -> dict:
    bad = HTTPException(400, "This approval link is not valid. Start again with the Connect button.")
    try:
        body, mac = value.rsplit(".", 1)
    except ValueError:
        raise bad
    good = hmac.new(_key_bytes() + b"|state", body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, good):
        raise bad
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        raise bad
    if not isinstance(data, dict) or data.get("exp", 0) < time.time():
        raise HTTPException(400, "This approval link has expired. Start again with the Connect button.")
    return data


# --------------------------------------------------------------------------
# talking to eBay
# --------------------------------------------------------------------------
def _basic() -> str:
    cred = f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}".encode()
    return "Basic " + base64.b64encode(cred).decode()


def _post_form(fields: dict) -> dict:
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(fields).encode(),
        method="POST",
        headers={"Authorization": _basic(), "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        log.warning("eBay token call -> %s %s", e.code, body)  # never logs the code or any key
        try:
            desc = json.loads(body).get("error_description") or json.loads(body).get("error") or ""
        except Exception:
            desc = ""
        raise HTTPException(502, f"eBay refused the request ({e.code}). {desc}".strip())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.warning("eBay unreachable: %s", e)
        raise HTTPException(502, "Could not reach eBay.")


def _get_json(url: str, access_token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Language": "en-US",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        log.warning("eBay %s -> %s %s", url.split("?")[0], e.code, body)
        raise HTTPException(502, f"eBay answered with an error ({e.code}) on {url.split('?')[0].rsplit('/', 2)[-2]}.")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.warning("eBay unreachable: %s", e)
        raise HTTPException(502, "Could not reach eBay.")


def _need_config() -> str:
    if not configured():
        raise HTTPException(503, "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API.")
    runame = (os.environ.get("EBAY_RUNAME") or "").strip()
    if not runame:
        raise HTTPException(503, "EBAY_RUNAME is not set on the API.")
    _key_bytes()  # raises a readable error if the encryption key is missing or malformed
    return runame


# --------------------------------------------------------------------------
# store rows
# --------------------------------------------------------------------------
def _store_row(db: Session, store: str):
    row = db.execute(
        text(
            "select store, seller_username, refresh_token_enc, scopes, refresh_token_expires_at "
            "from ebay_accounts where lower(store) = lower(:s)"
        ),
        {"s": (store or "").strip()},
    ).first()
    if not row:
        raise HTTPException(404, "That store is not in the list yet. Use Add store first.")
    return row


def _expected_username(row) -> str:
    return (row.seller_username or (seller_username() if row.store == MAIN_STORE else row.store)).strip()


# One short-lived access token per store, kept in memory only.
_access: dict[str, tuple[str, float]] = {}


def get_user_token(db: Session, store: str) -> str:
    """An access token for this store's seller account (used by the editing code that comes next)."""
    row = _store_row(db, store)
    if not row.refresh_token_enc:
        raise HTTPException(409, f"{row.store} has not been approved yet.")
    if row.refresh_token_expires_at and row.refresh_token_expires_at < datetime.now(timezone.utc):
        raise HTTPException(409, f"The approval for {row.store} has expired. Connect it again.")
    hit = _access.get(row.store)
    if hit and hit[1] > time.time() + 60:
        return hit[0]
    if not configured():
        raise HTTPException(503, "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API.")
    data = _post_form(
        {"grant_type": "refresh_token", "refresh_token": _decrypt(row.refresh_token_enc), "scope": row.scopes or _scopes()}
    )
    tok = data.get("access_token")
    if not tok:
        raise HTTPException(502, "eBay did not return an access token.")
    _access[row.store] = (tok, time.time() + int(data.get("expires_in") or 7200))
    return tok


def _identity(access_token: str) -> dict:
    return _get_json(IDENTITY_URL, access_token)


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
class StoreBody(BaseModel):
    store: str


class CallbackBody(BaseModel):
    code: str
    state: str


@router.post("/start")
def start(body: StoreBody, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    runame = _need_config()
    row = _store_row(db, body.store)
    state = _sign(
        {"store": row.store, "uid": user.user_id, "n": secrets.token_hex(8), "exp": int(time.time()) + STATE_MINUTES * 60}
    )
    params = {
        "client_id": os.environ["EBAY_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": runame,
        "scope": _scopes(),
        "state": state,
        "prompt": "login",  # always ask who is signing in, so the wrong account is not picked up silently
    }
    return {"url": f"{AUTH_URL}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}", "store": row.store}


@router.post("/callback")
def callback(body: CallbackBody, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    runame = _need_config()
    st = _unsign(body.state)
    if st.get("uid") != user.user_id:
        raise HTTPException(403, "This approval was started by a different person. Start again from your own sign-in.")
    row = _store_row(db, st.get("store", ""))

    data = _post_form({"grant_type": "authorization_code", "code": body.code.strip(), "redirect_uri": runame})
    refresh, access = data.get("refresh_token"), data.get("access_token")
    if not refresh or not access:
        raise HTTPException(502, "eBay did not return the keys. Start again with the Connect button.")

    # Who really signed in? Refuse (and save nothing) if it is not this store's account.
    ident = _identity(access)
    actual = str(ident.get("username") or "").strip()
    expected = _expected_username(row)
    if not actual:
        raise HTTPException(502, "eBay did not say which account signed in, so nothing was saved.")
    if actual.lower() != expected.lower():
        raise HTTPException(
            409,
            f"You signed in to eBay as '{actual}', but {row.store} expects '{expected}'. Nothing was saved. "
            f"Use a private window, sign in as the right account, and try again. If '{actual}' really is "
            f"this store's account, set the store's eBay user name to '{actual}' and connect again.",
        )

    granted = " ".join(str(data.get("scope") or "").split()) or _scopes()
    expires = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("refresh_token_expires_in") or 47304000))
    db.execute(
        text(
            """
            update ebay_accounts
               set refresh_token_enc = :tok, scopes = :sc, refresh_token_expires_at = :exp,
                   authorized_at = now(), authorized_by = :by, is_active = true, updated_at = now()
             where store = :s
            """
        ),
        {"tok": _encrypt(refresh), "sc": granted, "exp": expires, "by": user.email or user.user_id, "s": row.store},
    )
    db.commit()
    _access[row.store] = (access, time.time() + int(data.get("expires_in") or 7200))
    return {"ok": True, "store": row.store, "ebay_username": actual, "refresh_token_expires_at": expires.isoformat()}


@router.post("/disconnect")
def disconnect(body: StoreBody, db: Session = Depends(get_db)):
    row = _store_row(db, body.store)
    db.execute(
        text(
            """
            update ebay_accounts
               set refresh_token_enc = null, scopes = null, refresh_token_expires_at = null,
                   authorized_at = null, authorized_by = null, live_pushes_enabled = false, updated_at = now()
             where store = :s
            """
        ),
        {"s": row.store},
    )
    db.commit()
    _access.pop(row.store, None)
    return {"ok": True, "store": row.store}


@router.post("/test")
def test(body: StoreBody, db: Session = Depends(get_db)):
    """Read-only: asks eBay who this key belongs to and what the account is allowed to sell."""
    row = _store_row(db, body.store)
    try:
        tok = get_user_token(db, row.store)
        ident = _identity(tok)
    except FeedbackError as e:
        raise HTTPException(502, str(e))
    actual = str(ident.get("username") or "").strip()
    expected = _expected_username(row)
    out = {
        "ok": actual.lower() == expected.lower(),
        "store": row.store,
        "ebay_username": actual,
        "expected_username": expected,
        "account_type": ident.get("accountType"),
        "selling_limit": None,
        "selling_limit_error": None,
    }
    try:
        priv = _get_json(PRIVILEGE_URL, tok)
        out["selling_limit"] = priv.get("sellingLimit")
    except HTTPException as e:
        out["selling_limit_error"] = e.detail  # the identity check passed; only this extra read failed
    return out
