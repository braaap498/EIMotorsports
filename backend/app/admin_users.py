"""
Back-office accounts: list the people who can sign in to the admin, add a new
one (a display name is REQUIRED), and edit someone's display name or role.

How it works: these endpoints call Supabase's Auth admin API using the
project's secret key. Only this API server holds that key (the setting
SUPABASE_SERVICE_KEY on Render); the browser never sees it.

  - The display name is stored on the account as user_metadata.display_name.
    The storefront header shows it ("Hi, Eric") and falls back to the part of
    the email before the @ when an account has none.
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

Customer support (the Customers tab on the Team page): search customer
accounts (by name, email, phone or saved address), block / unblock them, resend the confirmation email, send a
password-reset email, or delete the account. Only accounts WITHOUT a staff
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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from app.auth import STAFF_ROLES, AuthUser, require_admin
from app.config import get_settings
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


def _clean_permissions(v: Optional[list[str]]) -> Optional[list[str]]:
    return None if v is None else normalize(v)


class NewUser(BaseModel):
    display_name: str = Field(max_length=40)
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
    role: Optional[Literal["admin", "staff"]] = None
    permissions: Optional[list[str]] = Field(default=None, max_length=100)

    @field_validator("display_name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _clean_name(v)

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
            "user_metadata": {"display_name": body.display_name},
        },
    )
    return _public(created)


@router.patch("/{user_id}")
def edit_staff(user_id: str, body: EditUser, me: AuthUser = Depends(require_admin)):
    if body.display_name is None and body.role is None and body.permissions is None:
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
    if body.display_name is not None:
        update["user_metadata"] = {**(current.get("user_metadata") or {}), "display_name": body.display_name}
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
    return _public(_call("PUT", f"/users/{user_id}", update))


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
CUSTOMER_LIST_CAP = 50
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


def _format_address(a: dict) -> str:
    parts = [a.get("line1"), a.get("line2"), a.get("city")]
    region_zip = " ".join(x for x in (a.get("region"), a.get("postal_code")) if x)
    parts.append(region_zip)
    country = (a.get("country") or "").strip()
    if country and country.upper() not in ("US", "USA"):
        parts.append(country)
    return ", ".join(p.strip() for p in parts if p and p.strip())


def _address_search_ids(needle: str) -> set[str]:
    """Ids of customers with a saved address matching the search text. Never breaks the list if the lookup fails."""
    n = re.sub(r"[^\w \-#./']", " ", needle).strip()
    if len(n) < 2:
        return set()
    cols = ("full_name", "line1", "line2", "city", "region", "postal_code", "phone")
    flt = "(" + ",".join(f"{c}.ilike.*{n}*" for c in cols) + ")"
    try:
        rows = _rest_get("customer_addresses", {"select": "user_id", "or": flt, "limit": "500"})
    except Exception:
        log.warning("address search failed; falling back to name/email/phone only", exc_info=True)
        return set()
    return {r["user_id"] for r in rows if r.get("user_id")}


def _attach_addresses(rows: list[dict]) -> None:
    """Set row["address"] to each customer's default shipping address (or the best one they have)."""
    ids = [r["id"] for r in rows if r.get("id") and UUID_RE.fullmatch(r["id"])]
    if not ids:
        return
    try:
        found = _rest_get(
            "customer_addresses",
            {
                "select": "user_id,kind,line1,line2,city,region,postal_code,country,is_default",
                "user_id": "in.(" + ",".join(ids) + ")",
                "limit": "1000",
            },
        )
    except Exception:
        log.warning("address lookup failed; showing customers without addresses", exc_info=True)
        return
    best: dict[str, dict] = {}
    for a in found:
        # Prefer a default shipping address, then any default, then any shipping, then anything.
        rank = (a.get("kind") == "shipping" and bool(a.get("is_default")), bool(a.get("is_default")), a.get("kind") == "shipping")
        uid = a.get("user_id")
        if uid and (uid not in best or rank > best[uid]["rank"]):
            best[uid] = {"rank": rank, "addr": a}
    for r in rows:
        hit = best.get(r["id"])
        if hit:
            r["address"] = _format_address(hit["addr"]) or None


def _load_customer(user_id: str) -> dict:
    if not UUID_RE.fullmatch(user_id):
        raise HTTPException(400, "Invalid account id.")
    u = _call("GET", f"/users/{user_id}")
    if not _is_customer(u):
        raise HTTPException(404, "That is not a customer account.")
    return u


@router.get("/customers")
def list_customers(q: str = ""):
    """Newest customers first; `q` filters by name, email, phone or a saved address."""
    needle = q.strip().lower()
    address_ids = _address_search_ids(needle) if needle else set()
    matches: list[dict] = []
    total = 0
    for page in range(1, 21):  # the list is paged; stop after 20 pages
        data = _call("GET", f"/users?page={page}&per_page=200")
        users = data.get("users") or []
        for u in users:
            if not _is_customer(u):
                continue
            total += 1
            if needle:
                um = u.get("user_metadata") or {}
                hay = " ".join(
                    str(x or "") for x in (u.get("email"), um.get("full_name"), um.get("display_name"), u.get("phone"))
                ).lower()
                if needle not in hay and u.get("id") not in address_ids:
                    continue
            matches.append(u)
        if len(users) < 200:
            break
    matches.sort(key=lambda u: u.get("created_at") or "", reverse=True)
    rows = [_public_customer(u) for u in matches[:CUSTOMER_LIST_CAP]]
    _attach_addresses(rows)
    return {"customers": rows, "matched": len(matches), "total": total}


@router.post("/customers/{user_id}/block")
def block_customer(user_id: str, me: AuthUser = Depends(require_admin)):
    """Stop them signing in. Their records are kept; undo with /unblock."""
    _load_customer(user_id)
    out = _call("PUT", f"/users/{user_id}", {"ban_duration": BLOCK_FOR})
    log.info("customer %s blocked by %s", user_id, me.email or me.user_id)
    return _public_customer(out)


@router.post("/customers/{user_id}/unblock")
def unblock_customer(user_id: str, me: AuthUser = Depends(require_admin)):
    _load_customer(user_id)
    out = _call("PUT", f"/users/{user_id}", {"ban_duration": "none"})
    log.info("customer %s unblocked by %s", user_id, me.email or me.user_id)
    return _public_customer(out)


@router.post("/customers/{user_id}/resend-confirmation", status_code=204)
def resend_confirmation(user_id: str, me: AuthUser = Depends(require_admin)):
    u = _load_customer(user_id)
    if _is_confirmed(u):
        raise HTTPException(400, "That customer has already confirmed their email.")
    base = _storefront_url()
    _auth_public_call("/resend", {"type": "signup", "email": u.get("email")}, f"{base}/auth/confirmed" if base else None)
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
def delete_customer(user_id: str, body: ConfirmDelete, me: AuthUser = Depends(require_admin)):
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
    log.info("customer %s deleted by %s", user_id, me.email or me.user_id)
    return Response(status_code=204)
