"""
Back-office accounts: list the people who can sign in to the admin, add a new
one (a display name is REQUIRED), and edit someone's display name or role.

How it works: these endpoints call Supabase's Auth admin API using the
project's secret key. Only this API server holds that key (the setting
SUPABASE_SERVICE_KEY on Render); the browser never sees it.

  - The display name is stored on the account as user_metadata.display_name.
    The storefront header shows it ("Hi, Eric") and falls back to the part of
    the email before the @ when an account has none.
  - An optional last name is stored as user_metadata.last_name. Purchase orders
    and the receiving reports show "First Last" (the storefront still only
    greets by the display name).
  - The role is stored as app_metadata.role, which a user cannot edit
    themselves.

Safeguards:
  - Admin or owner required for every route here.
  - The page can only hand out the roles "admin" and "staff". The "owner"
    role can only be given directly in the database, and an owner account can
    only be edited by another owner.
  - You cannot change your own role (so nobody locks themselves out).
  - An admin or owner can set a new password for any staff-side account, of
    any role. Only an owner can set an owner's password. The password itself
    is never written to any log.
  - Only accounts that already have a staff role can be edited here.
  - Employees ("staff") can be given individual permissions (see
    app/permissions.py). They are stored as app_metadata.permissions, are
    only kept for the "staff" role (admins and owners can do everything), and
    are cleared when someone is promoted to admin.

Customer support (the Customers tab on the Team page): lists EVERY customer (login accounts,
guest checkouts and the QuickBooks import) with Website / eBay / All filter buttons and sortable
columns, 50 per page. Search by name, email, phone or saved address. For login accounts you can also
block / unblock, resend the confirmation email, send a password-reset email or delete the account;
the whole list (accounts and guest checkouts) can be exported as Excel or PDF. Only accounts WITHOUT a staff
role are reachable through the /users/customers routes, so an owner or admin
can never be blocked or deleted from there. Deleting also requires the
customer's email to be typed back (checked here, not just in the browser).
Optional setting: STOREFRONT_URL (e.g. https://eandimotorsports.com) so the
emails sent from here bring the customer back to the store; without it
Supabase uses the Site URL from its URL Configuration.
"""
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import STAFF_ROLES, AuthUser, require_admin
from app.config import get_settings
from app.database import get_db
from app.permissions import IMPLIES, catalog, normalize

log = logging.getLogger("admin_users")

router = APIRouter(prefix="/users", tags=["admin-users"], dependencies=[Depends(require_admin)])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_name(v: str) -> str:
    v = re.sub(r"\s+", " ", (v or "").strip())
    if not v:
        raise ValueError("Display name is required")
    if re.search(r"[\x00-\x1f<>]", v):
        raise ValueError("Display name contains characters that are not allowed")
    return v


def _clean_last_name(v: Optional[str]) -> Optional[str]:
    """Optional. An empty string clears it. None means \"leave it alone\"."""
    if v is None:
        return None
    v = re.sub(r"\s+", " ", v.strip())
    if re.search(r"[\x00-\x1f<>]", v):
        raise ValueError("Last name contains characters that are not allowed")
    return v


def _forget_names() -> None:
    """Purchase orders cache staff names for a few minutes; drop that so a new or edited name shows at once."""
    try:
        from app.admin_purchase_orders import _NAMES

        _NAMES["at"] = 0.0
    except Exception:
        pass


def _clean_permissions(v: Optional[list[str]]) -> Optional[list[str]]:
    return None if v is None else normalize(v)


class NewUser(BaseModel):
    display_name: str = Field(max_length=40)
    last_name: str = Field(min_length=1, max_length=40)   # required for new accounts
    email: str = Field(max_length=200)
    role: Literal["admin", "staff"]
    password: str = Field(min_length=12, max_length=72)
    permissions: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("permissions")
    @classmethod
    def _perms(cls, v: list[str]) -> list[str]:
        return normalize(v)

    @field_validator("display_name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean_name(v)

    @field_validator("last_name")
    @classmethod
    def _last(cls, v: str) -> str:
        v = _clean_last_name(v) or ""
        if not v:
            raise ValueError("Last name is required")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("Enter a valid email address")
        return v


class SetPassword(BaseModel):
    password: str = Field(min_length=12, max_length=72)


class EditUser(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=40)
    last_name: Optional[str] = Field(default=None, max_length=40)
    role: Optional[Literal["admin", "staff"]] = None
    permissions: Optional[list[str]] = Field(default=None, max_length=100)

    @field_validator("display_name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _clean_name(v)

    @field_validator("last_name")
    @classmethod
    def _last(cls, v: Optional[str]) -> Optional[str]:
        return _clean_last_name(v)

    @field_validator("permissions")
    @classmethod
    def _perms(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        return _clean_permissions(v)


# ------------------------------------------------------------ Supabase calls
def _call(method: str, path: str, body: Optional[dict] = None) -> dict:
    s = get_settings()
    key = getattr(s, "supabase_service_key", None)
    if not key:
        raise HTTPException(
            503,
            "Account management is not set up yet: add SUPABASE_SERVICE_KEY to the API's environment settings.",
        )
    url = f"{s.supabase_url.rstrip('/')}/auth/v1/admin{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            err = {}
        code = err.get("error_code") or ""
        msg = err.get("msg") or err.get("message") or ""
        if code in ("email_exists", "user_already_exists") or "already" in msg.lower():
            raise HTTPException(409, "An account with that email already exists.")
        if code == "weak_password":
            raise HTTPException(400, msg or "That password is too weak. Use a longer one.")
        if e.code == 404:
            raise HTTPException(404, "That account was not found.")
        if e.code in (401, 403):
            raise HTTPException(502, "Supabase rejected the API's key. Check SUPABASE_SERVICE_KEY on the server.")
        raise HTTPException(502, f"Supabase could not complete that ({e.code}). {msg}".strip())
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Could not reach Supabase. Try again in a moment.")


def _public(u: dict) -> dict:
    um = u.get("user_metadata") or {}
    am = u.get("app_metadata") or {}
    return {
        "id": u.get("id"),
        "email": u.get("email"),
        "display_name": um.get("display_name") or "",
        "last_name": um.get("last_name") or "",
        "role": am.get("role") or "",
        "permissions": am.get("permissions") if isinstance(am.get("permissions"), list) else [],
        "last_sign_in_at": u.get("last_sign_in_at"),
        "created_at": u.get("created_at"),
    }


# ------------------------------------------------------------------ routes
@router.get("/permissions")
def list_permissions():
    """The checkbox list for employee permissions, plus which ones imply others."""
    return {"groups": catalog(), "implies": IMPLIES}


@router.get("")
def list_staff():
    """Everyone with a staff role. Customers are never listed."""
    out: list[dict] = []
    for page in range(1, 21):  # the list is paged; stop after 20 pages
        data = _call("GET", f"/users?page={page}&per_page=200")
        users = data.get("users") or []
        out += [_public(u) for u in users if (u.get("app_metadata") or {}).get("role") in STAFF_ROLES]
        if len(users) < 200:
            break
    out.sort(key=lambda u: (u["email"] or "").lower())
    return out


@router.post("", status_code=201)
def add_staff(body: NewUser):
    created = _call(
        "POST",
        "/users",
        {
            "email": body.email,
            "password": body.password,
            "email_confirm": True,
            "app_metadata": {"role": body.role, "permissions": body.permissions if body.role == "staff" else []},
            "user_metadata": {
                "display_name": body.display_name,
                **({"last_name": body.last_name} if body.last_name else {}),
            },
        },
    )
    _forget_names()
    return _public(created)


@router.patch("/{user_id}")
def edit_staff(user_id: str, body: EditUser, me: AuthUser = Depends(require_admin)):
    if body.display_name is None and body.last_name is None and body.role is None and body.permissions is None:
        raise HTTPException(400, "Nothing to change.")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", user_id):
        raise HTTPException(400, "Invalid account id.")
    current = _call("GET", f"/users/{user_id}")
    cur_role = (current.get("app_metadata") or {}).get("role")
    if cur_role not in STAFF_ROLES:
        raise HTTPException(404, "That is not a staff account.")
    if cur_role == "owner" and me.role != "owner":
        raise HTTPException(403, "Only an owner can edit an owner account.")
    if body.role is not None:
        if user_id == me.user_id:
            raise HTTPException(400, "You can't change your own role.")
        if cur_role == "owner":
            raise HTTPException(400, "An owner's role can't be changed here.")

    new_role = body.role or cur_role
    if body.permissions is not None and new_role != "staff":
        raise HTTPException(400, "Admins and owners can already do everything; permissions only apply to employees.")

    update: dict = {}
    if body.display_name is not None or body.last_name is not None:
        meta_u = {**(current.get("user_metadata") or {})}
        if body.display_name is not None:
            meta_u["display_name"] = body.display_name
        if body.last_name is not None:
            if body.last_name:
                meta_u["last_name"] = body.last_name
            else:
                meta_u.pop("last_name", None)
        update["user_metadata"] = meta_u
    if body.role is not None or body.permissions is not None:
        meta = {**(current.get("app_metadata") or {})}
        if body.role is not None:
            meta["role"] = body.role
        if new_role != "staff":
            meta["permissions"] = []  # promoted to admin: nothing left to limit
        elif body.permissions is not None:
            meta["permissions"] = body.permissions
        elif body.role is not None and cur_role != "staff":
            meta["permissions"] = []  # demoted to employee: start with nothing
        update["app_metadata"] = meta
    saved = _public(_call("PUT", f"/users/{user_id}", update))
    _forget_names()
    return saved


@router.post("/{user_id}/password", status_code=204)
def set_staff_password(user_id: str, body: SetPassword, me: AuthUser = Depends(require_admin)):
    """Set a new password for a staff-side account (any role)."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", user_id):
        raise HTTPException(400, "Invalid account id.")
    current = _call("GET", f"/users/{user_id}")
    cur_role = (current.get("app_metadata") or {}).get("role")
    if cur_role not in STAFF_ROLES:
        raise HTTPException(404, "That is not a staff account.")
    if cur_role == "owner" and me.role != "owner":
        raise HTTPException(403, "Only an owner can change an owner's password.")
    _call("PUT", f"/users/{user_id}", {"password": body.password})
    log.info("password set for account %s (%s) by %s", user_id, cur_role, me.email or me.user_id)
    return Response(status_code=204)


# =================================================================== CUSTOMERS
# Customer accounts are the Supabase users that do NOT have a staff role.
CUSTOMER_PAGE_SIZE = 50
AUTH_CACHE_SECONDS = 60  # how long the list of login accounts is reused between clicks (sort, filter, page)
BLOCK_FOR = "876000h"  # about 100 years; Supabase has no "forever"
UUID_RE = re.compile(r"[0-9a-fA-F-]{36}")


class ConfirmDelete(BaseModel):
    confirm_email: str = Field(max_length=200)


def _storefront_url() -> str:
    return (os.getenv("STOREFRONT_URL") or "").strip().rstrip("/")


def _auth_public_call(path: str, body: dict, redirect_to: Optional[str] = None) -> None:
    """
    Supabase's public Auth endpoints (/resend, /recover). These are the ones
    that actually SEND the email; the admin API can't. Called with the
    project's key, from the server only.
    """
    s = get_settings()
    key = getattr(s, "supabase_service_key", None)
    if not key:
        raise HTTPException(
            503,
            "Account management is not set up yet: add SUPABASE_SERVICE_KEY to the API's environment settings.",
        )
    url = f"{s.supabase_url.rstrip('/')}/auth/v1{path}"
    if redirect_to:
        url += "?" + urllib.parse.urlencode({"redirect_to": redirect_to})
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"apikey": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            return
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            err = {}
        msg = err.get("msg") or err.get("message") or ""
        if e.code == 429 or "rate" in (err.get("error_code") or ""):
            raise HTTPException(429, "Too many emails were sent recently. Wait a few minutes and try again.")
        raise HTTPException(502, f"Supabase could not send that email ({e.code}). {msg}".strip())
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Could not reach Supabase. Try again in a moment.")


def _is_customer(u: dict) -> bool:
    return (u.get("app_metadata") or {}).get("role") not in STAFF_ROLES


def _is_confirmed(u: dict) -> bool:
    return bool(u.get("email_confirmed_at") or u.get("confirmed_at"))


def _is_blocked(u: dict) -> bool:
    until = u.get("banned_until")
    if not until:
        return False
    try:
        return datetime.fromisoformat(str(until).replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except ValueError:
        return True  # a ban date we can't read is treated as still blocked


def _public_customer(u: dict) -> dict:
    um = u.get("user_metadata") or {}
    return {
        "id": u.get("id"),
        "email": u.get("email"),
        "name": um.get("full_name") or um.get("display_name") or "",
        "phone": u.get("phone") or "",
        "confirmed": _is_confirmed(u),
        "blocked": _is_blocked(u),
        "created_at": u.get("created_at"),
        "last_sign_in_at": u.get("last_sign_in_at"),
        "address": None,  # filled in by _attach_addresses
    }


def _rest_get(table: str, params: dict) -> list[dict]:
    """Read rows from the project's database through Supabase's REST API (server key, so it can see every customer's rows)."""
    s = get_settings()
    key = getattr(s, "supabase_service_key", None)
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_KEY is not set")
    url = f"{s.supabase_url.rstrip('/')}/rest/v1/{table}?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read() or b"[]")


def _describe_error(e: Exception) -> str:
    """A short, safe description of why a lookup failed, shown to the admin so a problem isn't silent."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            err = json.loads(e.read().decode("utf-8", "replace"))
            detail = err.get("message") or err.get("msg") or err.get("hint") or ""
        except Exception:
            detail = ""
        return f"Supabase returned {e.code}. {detail}".strip()[:200]
    return f"{type(e).__name__}: {e}"[:200]


def _format_address(a: dict) -> str:
    parts = [a.get("line1"), a.get("line2"), a.get("city")]
    region_zip = " ".join(x for x in (a.get("region"), a.get("postal_code")) if x)
    parts.append(region_zip)
    country = (a.get("country") or "").strip()
    if country and country.upper() not in ("US", "USA"):
        parts.append(country)
    return ", ".join(p.strip() for p in parts if p and p.strip())


def _attach_addresses(rows: list[dict], db: Session, limit: int = 1000) -> Optional[str]:
    """
    Set row["address"] to each customer's default shipping address (or the best one they have),
    and fill row["phone"] from that address when the login itself has no phone.
    Returns None on success, or a short error note if the lookup failed (the list still works).
    """
    ids = [r["id"] for r in rows if r.get("id") and UUID_RE.fullmatch(r["id"])]
    if not ids:
        return None
    try:
        found = [
            dict(r._mapping)
            for r in db.execute(
                text(
                    "select user_id, kind, line1, line2, city, region, postal_code, country, phone, is_default "
                    "from customer_addresses where user_id = any(cast(:ids as uuid[])) limit :lim"
                ),
                {"ids": ids, "lim": limit},
            ).all()
        ]
    except Exception as e:
        db.rollback()
        log.warning("address lookup failed; showing customers without addresses", exc_info=True)
        return _describe_error(e)
    best: dict[str, dict] = {}
    for a in found:
        # Prefer a default shipping address, then any default, then any shipping, then anything.
        rank = (a.get("kind") == "shipping" and bool(a.get("is_default")), bool(a.get("is_default")), a.get("kind") == "shipping")
        uid = str(a["user_id"]) if a.get("user_id") else None
        if uid and (uid not in best or rank > best[uid]["rank"]):
            best[uid] = {"rank": rank, "addr": a}
    for r in rows:
        hit = best.get(r["id"])
        if hit:
            r["address"] = _format_address(hit["addr"]) or None
            # Most customers have no phone on their login, so use the one saved with their address.
            if not r.get("phone") and hit["addr"].get("phone"):
                r["phone"] = hit["addr"]["phone"]
    return None


def _load_customer(user_id: str) -> dict:
    if not UUID_RE.fullmatch(user_id):
        raise HTTPException(400, "Invalid account id.")
    u = _call("GET", f"/users/{user_id}")
    if not _is_customer(u):
        raise HTTPException(404, "That is not a customer account.")
    return u


_AUTH_CACHE: dict = {"at": 0.0, "users": []}


def _auth_customers(fresh: bool = False) -> list[dict]:
    """Every login account without a staff role. Reused for a minute so sorting and paging stay quick."""
    if not fresh and _AUTH_CACHE["users"] and time.monotonic() - _AUTH_CACHE["at"] < AUTH_CACHE_SECONDS:
        return _AUTH_CACHE["users"]
    users: list[dict] = []
    for page in range(1, 51):  # 200 per page; up to 10,000 accounts
        data = _call("GET", f"/users?page={page}&per_page=200")
        batch = data.get("users") or []
        users.extend(u for u in batch if _is_customer(u))
        if len(batch) < 200:
            break
    _AUTH_CACHE["users"] = users
    _AUTH_CACHE["at"] = time.monotonic()
    return users


def _forget_auth_cache() -> None:
    _AUTH_CACHE["at"] = 0.0
    _AUTH_CACHE["users"] = []


def _ts(v) -> Optional[float]:
    """A date as a number, so dates from the login system and from the database sort together."""
    if not v:
        return None
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).timestamp()
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def _iso(v) -> Optional[str]:
    if not v:
        return None
    return v.isoformat() if isinstance(v, datetime) else str(v)


_SOURCE_LABELS = {"ebay": "eBay", "amazon": "Amazon"}


def _customer_directory(db: Session, fresh: bool = False) -> tuple[list[dict], dict]:
    """
    One list of everybody: login accounts (from Supabase) plus the rows of the `customers` table
    (guest checkouts and the QuickBooks import). A login account and a customers row with the same
    user id, or the same email, are one person and show as one row.
    Returns (rows, problems) where problems says what could not be loaded.
    """
    problems: dict = {"addresses": None, "customers_table": None}
    accounts = [_public_customer(u) for u in _auth_customers(fresh)]
    problems["addresses"] = _attach_addresses(accounts, db, limit=200000)

    records: list = []
    try:
        records = db.execute(
            text(
                "select id, user_id, email, full_name, phone, last_shipping_address, created_at, "
                "source, email_kind, marketplace from customers limit 200000"
            )
        ).all()
    except Exception:
        db.rollback()
        try:  # the QuickBooks columns may not exist yet
            records = db.execute(
                text(
                    "select id, user_id, email, full_name, phone, last_shipping_address, created_at, "
                    "null as source, null as email_kind, null as marketplace from customers limit 200000"
                )
            ).all()
        except Exception as e:
            db.rollback()
            log.warning("customers table could not be read for the Customers tab", exc_info=True)
            problems["customers_table"] = _describe_error(e)

    by_id = {a["id"]: a for a in accounts}
    by_email = {(a["email"] or "").strip().lower(): a for a in accounts if a.get("email")}
    claimed: dict[str, object] = {}
    loose: list = []
    for rec in records:
        acct = by_id.get(str(rec.user_id)) if rec.user_id else None
        if acct is None:
            acct = by_email.get((rec.email or "").strip().lower())
        if acct is not None and acct["id"] not in claimed:
            claimed[acct["id"]] = rec
        elif acct is None:
            loose.append(rec)

    def addr_text(rec) -> str:
        addr = rec.last_shipping_address if rec is not None else None
        if isinstance(addr, str):
            try:
                addr = json.loads(addr)
            except ValueError:
                addr = {}
        return _format_address(addr) if isinstance(addr, dict) else ""

    def source_of(rec, has_account: bool) -> tuple[str, str]:
        mkt = (getattr(rec, "marketplace", None) or "").strip().lower() if rec is not None else ""
        src = (getattr(rec, "source", None) or "").strip().lower() if rec is not None else ""
        if mkt == "ebay":
            return "ebay", "eBay"
        if has_account or src != "quickbooks":
            return "website", "Website"
        if mkt == "amazon":
            return "amazon", "Amazon"
        return "quickbooks", "QuickBooks"

    rows: list[dict] = []
    for a in accounts:
        rec = claimed.get(a["id"])
        group, label = source_of(rec, True)
        status = "Blocked" if a["blocked"] else ("Active" if a["confirmed"] else "Waiting for verification")
        rows.append({
            "id": a["id"], "has_account": True,
            "email": a["email"], "email_masked": (getattr(rec, "email_kind", None) or "") == "masked",
            "name": a["name"] or (getattr(rec, "full_name", None) or "") or "",
            "phone": a["phone"] or (getattr(rec, "phone", None) or "") or "",
            "status": status, "confirmed": a["confirmed"], "blocked": a["blocked"],
            "created_at": a["created_at"], "last_sign_in_at": a["last_sign_in_at"],
            "address": a["address"] or addr_text(rec) or None,
            "last_purchase_at": None, "is_staff": False,
            "group": group, "source": label,
        })
    for rec in loose:
        group, label = source_of(rec, False)
        rows.append({
            "id": str(rec.id), "has_account": False,
            "email": rec.email, "email_masked": (rec.email_kind or "") == "masked",
            "name": rec.full_name or "", "phone": rec.phone or "",
            "status": "No login", "confirmed": False, "blocked": False,
            "created_at": _iso(rec.created_at) if group == "website" else None, "last_sign_in_at": None,
            "address": addr_text(rec) or None,
            "last_purchase_at": None, "is_staff": False,
            "group": group, "source": label,
        })
    return rows, problems


_SORT_KEYS = {
    "customer": lambda r: (r["name"] or r["email"] or "").lower(),
    "source": lambda r: r["source"].lower(),
    "phone": lambda r: re.sub(r"\D", "", r["phone"] or ""),
    "status": lambda r: r["status"].lower(),
    "signed_up": lambda r: _ts(r["created_at"]),
    "last_sign_in": lambda r: _ts(r["last_sign_in_at"]),
    "address": lambda r: (r["address"] or "").lower(),
    "last_purchase": lambda r: _ts(r.get("last_purchase_at")),
}


_SYNC_READY = False


def _sync_columns_ready(db: Session) -> bool:
    """True once customers_sync.sql has added the account columns to the `customers` table."""
    global _SYNC_READY
    if _SYNC_READY:
        return True
    try:
        n = db.execute(
            text(
                "select count(*) from information_schema.columns where table_schema = 'public' "
                "and table_name = 'customers' and column_name in ('account_status', 'last_sign_in_at', 'account_created_at')"
            )
        ).scalar()
    except Exception:
        db.rollback()
        return False
    _SYNC_READY = n == 3
    return _SYNC_READY


_PURCHASE_READY = False


def _purchase_column_ready(db: Session) -> bool:
    """True once customers_last_purchase.sql has added customers.last_purchase_at."""
    global _PURCHASE_READY
    if _PURCHASE_READY:
        return True
    try:
        n = db.execute(
            text(
                "select count(*) from information_schema.columns where table_schema = 'public' "
                "and table_name = 'customers' and column_name = 'last_purchase_at'"
            )
        ).scalar()
    except Exception:
        db.rollback()
        return False
    _PURCHASE_READY = bool(n)
    return _PURCHASE_READY


# Everything the list shows, worked out inside the database so only one page of rows ever travels.
_LIST_CTE = """
with base as (
  select c.id as rec_id, c.user_id, c.email,
         nullif(btrim(coalesce(c.full_name, '')), '') as name,
         coalesce(nullif(btrim(coalesce(c.phone, '')), ''), nullif(btrim(coalesce(a.phone, '')), '')) as phone,
         coalesce(a.addr, cast(c.last_shipping_address as jsonb)) as addr,
         c.source, c.email_kind, c.marketplace, c.account_status, c.last_sign_in_at,
         coalesce(c.account_created_at, c.created_at) as signed_up_raw,
         __LP__ as last_purchase_at
  from customers c
  left join lateral (
    select jsonb_build_object('line1', x.line1, 'line2', x.line2, 'city', x.city, 'region', x.region,
                              'postal_code', x.postal_code, 'country', x.country) as addr, x.phone
    from customer_addresses x
    where x.user_id = c.user_id
    order by (x.kind = 'shipping' and x.is_default) desc, x.is_default desc, (x.kind = 'shipping') desc, x.id
    limit 1
  ) a on c.user_id is not null
), d as (
  select base.*,
         case when lower(coalesce(marketplace, '')) = 'ebay' then 'ebay'
              when user_id is not null or lower(coalesce(source, '')) <> 'quickbooks' then 'website'
              when lower(coalesce(marketplace, '')) = 'amazon' then 'amazon'
              else 'quickbooks' end as grp,
         concat_ws(', ', nullif(addr->>'line1', ''), nullif(addr->>'line2', ''), nullif(addr->>'city', ''),
                   nullif(btrim(concat_ws(' ', addr->>'region', addr->>'postal_code')), '')) as address_text
  from base
), e as (
  select d.*,
         case grp when 'ebay' then 'eBay' when 'website' then 'Website' when 'amazon' then 'Amazon'
              else 'QuickBooks' end as source_label,
         -- Only people who signed up on the site (or checked out as a guest) have a "signed up" date.
         -- For customers who only exist in QuickBooks / eBay / Amazon it would just be the date they were added there.
         case when grp = 'website' then signed_up_raw else null end as signed_up,
         (account_status = 'staff') as is_staff,
         case when account_status = 'staff' then 'Staff'
              when user_id is null then 'No login'
              when account_status = 'blocked' then 'Blocked'
              when account_status = 'unconfirmed' then 'Waiting for verification'
              else 'Active' end as status_label
  from d
)
"""
_LIST_SEARCH = "(:pat = '' or lower(concat_ws(' ', name, email, phone, address_text)) like :pat escape '!')"
_LIST_ORDER = {
    "customer": "lower(coalesce(name, email, ''))",
    "source": "lower(source_label)",
    "phone": r"nullif(regexp_replace(phone, '\D', '', 'g'), '')",
    "status": "lower(status_label)",
    "signed_up": "signed_up",
    "last_sign_in": "last_sign_in_at",
    "address": "nullif(lower(address_text), '')",
    "last_purchase": "last_purchase_at",
}


def _list_customers_fast(q, source, sort, direction, page, db: Session):
    needle = q.strip().lower()[:100]
    pat = "%" + re.sub(r"([%_!])", r"!\1", needle) + "%" if needle else ""
    cte = _LIST_CTE.replace("__LP__", "c.last_purchase_at" if _purchase_column_ready(db) else "cast(null as date)")
    counts = {"all": 0, "website": 0, "ebay": 0, "amazon": 0, "quickbooks": 0}
    for grp, n in db.execute(
        text(cte + f"select grp, count(*) from e where {_LIST_SEARCH} group by grp"), {"pat": pat}
    ).all():
        counts["all"] += n
        if grp in counts:
            counts[grp] = n
    matched = counts["all"] if source == "all" else counts[source]
    pages = max(1, -(-matched // CUSTOMER_PAGE_SIZE))
    page = min(page, pages)
    recs = db.execute(
        text(
            cte
            + "select rec_id, user_id, email, name, phone, addr, email_kind, status_label, grp, source_label, "
            "signed_up, last_sign_in_at, last_purchase_at, is_staff from e "
            f"where {_LIST_SEARCH} and (:grp = 'all' or grp = :grp) "
            f"order by {_LIST_ORDER[sort]} {direction} nulls last, rec_id "
            "limit :lim offset :off"
        ),
        {"pat": pat, "grp": source, "lim": CUSTOMER_PAGE_SIZE, "off": (page - 1) * CUSTOMER_PAGE_SIZE},
    ).all()
    rows = []
    for r in recs:
        addr = r.addr
        if isinstance(addr, str):
            try:
                addr = json.loads(addr)
            except ValueError:
                addr = None
        has_account = r.user_id is not None
        rows.append({
            "id": str(r.user_id) if has_account else str(r.rec_id),
            "has_account": has_account,
            "email": r.email,
            "email_masked": (r.email_kind or "") == "masked",
            "name": r.name or "",
            "phone": r.phone or "",
            "status": r.status_label,
            "confirmed": has_account and r.status_label != "Waiting for verification",
            "blocked": r.status_label == "Blocked",
            "created_at": _iso(r.signed_up),
            "last_sign_in_at": _iso(r.last_sign_in_at),
            "last_purchase_at": _iso(r.last_purchase_at),
            "is_staff": bool(r.is_staff),
            "address": (_format_address(addr) if isinstance(addr, dict) else "") or None,
            "group": r.grp,
            "source": r.source_label,
        })
    return {
        "customers": rows,
        "matched": matched,
        "counts": counts,
        "page": page,
        "pages": pages,
        "page_size": CUSTOMER_PAGE_SIZE,
        "addresses_error": None,
        "customers_error": None,
    }


@router.get("/customers")
def list_customers(
    q: str = "",
    source: Literal["all", "website", "ebay", "amazon", "quickbooks"] = "all",
    sort: Literal["customer", "source", "phone", "status", "signed_up", "last_purchase", "last_sign_in", "address"] = "signed_up",
    direction: Literal["asc", "desc"] = Query("desc", alias="dir"),
    page: int = Query(1, ge=1),
    refresh: bool = False,
    db: Session = Depends(get_db),
):
    """
    Everyone: login accounts, guest checkouts and the QuickBooks import, one page at a time.
    `q` searches name, email, phone and address; `source` narrows to Website, eBay, Amazon or QuickBooks; `sort` and `dir`
    order the whole list (not just the page on screen).
    Fast path: the `customers` table is kept in step with the logins by customers_sync.sql, so the
    database does the searching, sorting and paging. Without that script, the slower merge is used.
    """
    if _sync_columns_ready(db):
        try:
            return _list_customers_fast(q, source, sort, direction, page, db)
        except Exception as e:
            db.rollback()
            log.warning("fast customer list failed; falling back to the slow one", exc_info=True)
    return _list_customers_slow(q, source, sort, direction, page, refresh, db)


def _list_customers_slow(q, source, sort, direction, page, refresh, db: Session):
    """
    The original way: read every login account from Supabase and every row of `customers`, merge them in
    Python. Only used until customers_sync.sql has been run (see _sync_columns_ready).
    """
    rows, problems = _customer_directory(db, fresh=refresh)
    needle = q.strip().lower()
    if needle:
        rows = [
            r for r in rows
            if needle in " ".join(str(x or "") for x in (r["name"], r["email"], r["phone"], r["address"])).lower()
        ]
    counts = {
        "all": len(rows),
        "website": sum(1 for r in rows if r["group"] == "website"),
        "ebay": sum(1 for r in rows if r["group"] == "ebay"),
        "amazon": sum(1 for r in rows if r["group"] == "amazon"),
        "quickbooks": sum(1 for r in rows if r["group"] == "quickbooks"),
    }
    if source != "all":
        rows = [r for r in rows if r["group"] == source]

    key = _SORT_KEYS[sort]
    rows.sort(key=lambda r: (r["email"] or "").lower())  # steady order for ties
    filled = [r for r in rows if key(r) not in (None, "")]
    blank = [r for r in rows if key(r) in (None, "")]
    filled.sort(key=key, reverse=(direction == "desc"))
    rows = filled + blank  # empty cells always go last, whichever way it is sorted

    matched = len(rows)
    pages = max(1, -(-matched // CUSTOMER_PAGE_SIZE))
    page = min(page, pages)
    start = (page - 1) * CUSTOMER_PAGE_SIZE
    return {
        "customers": rows[start : start + CUSTOMER_PAGE_SIZE],
        "matched": matched,
        "counts": counts,
        "page": page,
        "pages": pages,
        "page_size": CUSTOMER_PAGE_SIZE,
        "addresses_error": problems["addresses"],
        "customers_error": problems["customers_table"],
    }


# ----------------------------------------------------------------- export
EXPORT_COLUMNS = ["Type", "Name", "Email", "Phone", "Status", "Signed up", "Last sign-in", "Address"]


def _fmt_date(v) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return str(v)[:10]


def _export_rows(db: Session) -> tuple[list[list[str]], int, int, list[str]]:
    """Every customer, uncapped: accounts (newest first) then guest checkouts (newest first).

    Returns (rows, account_count, guest_count, notes). `notes` says what could not be loaded, if anything,
    so a partial export is never mistaken for a complete one.
    """
    notes: list[str] = []
    users: list[dict] = []
    for page in range(1, 51):  # 200 per page; up to 10,000 accounts
        data = _call("GET", f"/users?page={page}&per_page=200")
        batch = data.get("users") or []
        users.extend(u for u in batch if _is_customer(u))
        if len(batch) < 200:
            break
    users.sort(key=lambda u: u.get("created_at") or "", reverse=True)
    accounts = [_public_customer(u) for u in users]
    if _attach_addresses(accounts, db, limit=200000):
        notes.append("Saved addresses could not be loaded, so the Address column is incomplete.")

    rows: list[list[str]] = []
    for c in accounts:
        status = "Blocked" if c["blocked"] else ("Active" if c["confirmed"] else "Waiting for verification")
        rows.append([
            "Account", c["name"] or "", c["email"] or "", c["phone"] or "", status,
            _fmt_date(c["created_at"]), _fmt_date(c["last_sign_in_at"]) if c["last_sign_in_at"] else "Never",
            c["address"] or "",
        ])

    guests = []
    try:
        guests = db.execute(
            text(
                "select email, full_name, phone, last_shipping_address, created_at from customers "
                "where user_id is null order by created_at desc"
            )
        ).all()
    except Exception:
        db.rollback()
        log.warning("guest customers could not be loaded for the export", exc_info=True)
        notes.append("Guest customers could not be loaded, so they are not in this file.")
    for g in guests:
        addr = g.last_shipping_address
        if isinstance(addr, str):
            try:
                addr = json.loads(addr)
            except ValueError:
                addr = {}
        rows.append([
            "Guest", g.full_name or "", g.email or "", g.phone or "", "", _fmt_date(g.created_at), "",
            _format_address(addr or {}),
        ])
    return rows, len(accounts), len(guests), notes


def _build_xlsx(rows: list[list[str]], notes: list[str]) -> bytes:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise HTTPException(503, "Excel export isn't installed on the server yet: add openpyxl to requirements.txt.")
    wb = Workbook()
    ws = wb.active
    ws.title = "Customers"
    ws.append(EXPORT_COLUMNS)
    for r in rows:
        ws.append(r)
    head_fill = PatternFill("solid", fgColor="111111")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center")
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.data_type = "s"  # always text, so a name or address starting with "=" can never run as a formula
            cell.alignment = Alignment(vertical="top", wrap_text=(cell.column == 8))
    widths = [10, 24, 32, 16, 22, 12, 13, 50]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    if notes:
        n = wb.create_sheet("Notes")
        for line in notes:
            n.append([line])
        n.column_dimensions["A"].width = 100
    from io import BytesIO

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_pdf(rows: list[list[str]], accounts: int, guests: int, notes: list[str]) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError:
        raise HTTPException(503, "PDF export isn't installed on the server yet: add reportlab to requirements.txt.")
    from io import BytesIO
    from xml.sax.saxutils import escape

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(letter), leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.6 * inch, title="Customers",
    )
    base = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=base["BodyText"], fontSize=7.5, leading=9.5)
    head = ParagraphStyle("head", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")
    story = [
        Paragraph("Customers", base["Title"]),
        Paragraph(
            escape(
                f"Exported {datetime.now(timezone.utc).strftime('%Y-%m-%d')} · {len(rows)} customers "
                f"({accounts} accounts, {guests} guests)"
            ),
            base["Normal"],
        ),
    ]
    for n in notes:
        story.append(Paragraph(escape("Note: " + n), ParagraphStyle("note", parent=base["Normal"], textColor=colors.HexColor("#b45309"))))
    story.append(Spacer(1, 10))
    data = [[Paragraph(escape(h), head) for h in EXPORT_COLUMNS]]
    for r in rows:
        data.append([Paragraph(escape(str(v or "")), cell) for v in r])
    widths = [44, 98, 148, 78, 74, 56, 58, 164]  # adds up to the 720pt between the margins
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111111")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    story.append(table)

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#6b7280"))
        canvas.drawRightString(landscape(letter)[0] - 0.5 * inch, 0.35 * inch, f"Page {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


@router.get("/customers/export")
def export_customers(
    fmt: Literal["xlsx", "pdf"] = Query("xlsx", alias="format"),
    db: Session = Depends(get_db),
    me: AuthUser = Depends(require_admin),
):
    """The whole customer list (accounts and guest checkouts, no cap, ignores any search) as Excel or PDF."""
    rows, accounts, guests, notes = _export_rows(db)
    if fmt == "xlsx":
        body = _build_xlsx(rows, notes)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        body = _build_pdf(rows, accounts, guests, notes)
        media = "application/pdf"
    log.info("customer list exported as %s (%d accounts, %d guests) by %s", fmt, accounts, guests, me.email or me.user_id)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="customers-{stamp}.{fmt}"', "Cache-Control": "no-store"},
    )


def _mirror_account(db: Session, user_id: str, status: Optional[str], unlink: bool = False) -> None:
    """Best effort: copy an admin action into the `customers` row at once. (customers_sync.sql's trigger does the same.)"""
    try:
        if unlink:
            db.execute(
                text(
                    "update customers set user_id = null, account_status = null, last_sign_in_at = null, "
                    "account_created_at = null where user_id = cast(:u as uuid)"
                ),
                {"u": user_id},
            )
        else:
            db.execute(
                text("update customers set account_status = :s, updated_at = now() where user_id = cast(:u as uuid)"),
                {"s": status, "u": user_id},
            )
        db.commit()
    except Exception:
        db.rollback()


@router.post("/customers/{user_id}/block")
def block_customer(user_id: str, me: AuthUser = Depends(require_admin), db: Session = Depends(get_db)):
    """Stop them signing in. Their records are kept; undo with /unblock."""
    _load_customer(user_id)
    out = _call("PUT", f"/users/{user_id}", {"ban_duration": BLOCK_FOR})
    _forget_auth_cache()
    _mirror_account(db, user_id, "blocked")
    log.info("customer %s blocked by %s", user_id, me.email or me.user_id)
    return _public_customer(out)


@router.post("/customers/{user_id}/unblock")
def unblock_customer(user_id: str, me: AuthUser = Depends(require_admin), db: Session = Depends(get_db)):
    _load_customer(user_id)
    out = _call("PUT", f"/users/{user_id}", {"ban_duration": "none"})
    _forget_auth_cache()
    _mirror_account(db, user_id, "active" if _is_confirmed(out) else "unconfirmed")
    log.info("customer %s unblocked by %s", user_id, me.email or me.user_id)
    return _public_customer(out)


@router.post("/customers/{user_id}/resend-confirmation", status_code=204)
def resend_confirmation(user_id: str, me: AuthUser = Depends(require_admin)):
    u = _load_customer(user_id)
    if _is_confirmed(u):
        raise HTTPException(400, "That customer has already confirmed their email.")
    base = _storefront_url()
    _auth_public_call("/resend", {"type": "signup", "email": u.get("email")}, f"{base}/auth/confirmed" if base else None)
    _forget_auth_cache()
    log.info("confirmation email re-sent for customer %s by %s", user_id, me.email or me.user_id)
    return Response(status_code=204)


@router.post("/customers/{user_id}/reset-password", status_code=204)
def send_customer_reset(user_id: str, me: AuthUser = Depends(require_admin)):
    """Email the customer a password-reset link. Nobody here ever sees a password."""
    u = _load_customer(user_id)
    base = _storefront_url()
    _auth_public_call("/recover", {"email": u.get("email")}, base or None)
    log.info("password reset email sent for customer %s by %s", user_id, me.email or me.user_id)
    return Response(status_code=204)


@router.post("/customers/{user_id}/delete", status_code=204)
def delete_customer(
    user_id: str, body: ConfirmDelete, me: AuthUser = Depends(require_admin), db: Session = Depends(get_db)
):
    """Permanently delete a customer account. The email must be typed back to confirm."""
    u = _load_customer(user_id)
    if (body.confirm_email or "").strip().lower() != (u.get("email") or "").strip().lower():
        raise HTTPException(400, "The email you typed doesn't match this account.")
    try:
        _call("DELETE", f"/users/{user_id}")
    except HTTPException as e:
        if e.status_code == 502:
            raise HTTPException(
                409,
                "Supabase refused to delete this account. Something else is probably still linked to it (such as "
                "an order). Block the account instead, or tell your developer what's linked to it.",
            )
        raise
    _forget_auth_cache()
    _mirror_account(db, user_id, None, unlink=True)  # the order history stays; the record just has no login now
    log.info("customer %s deleted by %s", user_id, me.email or me.user_id)
    return Response(status_code=204)
