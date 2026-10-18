"""
Back-office accounts: list the people who can sign in to the admin, add a new
one (a display name is REQUIRED), and edit someone's display name or role.

How it works: these endpoints call Supabase's Auth admin API using the
project's secret key. Only this API server holds that key (the setting
SUPABASE_SERVICE_KEY on Render); the browser never sees it.

  - The display name is stored on the account as user_metadata.display_name.
    The storefront header shows it ("Hi, Ian") and falls back to the part of
    the email before the @ when an account has none.
  - The role is stored as app_metadata.role, which a user cannot edit
    themselves.

Safeguards:
  - Admin or owner required for every route here.
  - The page can only hand out the roles "admin" and "staff". The "owner"
    role can only be given directly in the database, and an owner account can
    only be edited by another owner.
  - You cannot change your own role (so nobody locks themselves out).
  - Only accounts that already have a staff role can be edited here.
"""
import json
import re
import urllib.error
import urllib.request
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.auth import STAFF_ROLES, AuthUser, require_admin
from app.config import get_settings

router = APIRouter(prefix="/users", tags=["admin-users"], dependencies=[Depends(require_admin)])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_name(v: str) -> str:
    v = re.sub(r"\s+", " ", (v or "").strip())
    if not v:
        raise ValueError("Display name is required")
    if re.search(r"[\x00-\x1f<>]", v):
        raise ValueError("Display name contains characters that are not allowed")
    return v


class NewUser(BaseModel):
    display_name: str = Field(max_length=40)
    email: str = Field(max_length=200)
    role: Literal["admin", "staff"]
    password: str = Field(min_length=12, max_length=72)

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


class EditUser(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=40)
    role: Optional[Literal["admin", "staff"]] = None

    @field_validator("display_name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _clean_name(v)


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
        "last_sign_in_at": u.get("last_sign_in_at"),
        "created_at": u.get("created_at"),
    }


# ------------------------------------------------------------------ routes
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
            "app_metadata": {"role": body.role},
            "user_metadata": {"display_name": body.display_name},
        },
    )
    return _public(created)


@router.patch("/{user_id}")
def edit_staff(user_id: str, body: EditUser, me: AuthUser = Depends(require_admin)):
    if body.display_name is None and body.role is None:
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

    update: dict = {}
    if body.display_name is not None:
        update["user_metadata"] = {**(current.get("user_metadata") or {}), "display_name": body.display_name}
    if body.role is not None:
        update["app_metadata"] = {**(current.get("app_metadata") or {}), "role": body.role}
    return _public(_call("PUT", f"/users/{user_id}", update))
