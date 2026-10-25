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

Customer support (the Customers tab on the Team page): search customer
accounts (by name, email, phone or saved address), block / unblock them, resend the confirmation email, send a
password-reset email, delete the account, or export the whole list (accounts and guest
checkouts) as Excel or PDF. Only accounts WITHOUT a staff
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


def _address_search_ids(needle: str, db: Session) -> tuple[set[str], Optional[str]]:
    """Ids of customers with a saved address matching the search text, plus an error note if the lookup failed.

    Reads the database directly (the same connection checkout uses) rather than through Supabase's REST
    service, so a hiccup in that service cannot blank out this column.
    """
    n = re.sub(r"[^\w \-#./']", " ", needle).strip()
    if len(n) < 2:
        return set(), None
    pat = "%" + n.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        rows = db.execute(
            text(
                "select distinct user_id from customer_addresses where "
                "full_name ilike :p or line1 ilike :p or line2 ilike :p or city ilike :p "
                "or region ilike :p or postal_code ilike :p or phone ilike :p limit 500"
            ),
            {"p": pat},
        ).all()
    except Exception as e:
        db.rollback()
        log.warning("address search failed; falling back to name/email/phone only", exc_info=True)
        return set(), _describe_error(e)
    return {str(r.user_id) for r in rows if r.user_id}, None


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


@router.get("/customers")
def list_customers(q: str = "", db: Session = Depends(get_db)):
    """Newest customers first; `q` filters by name, email, phone or a saved address."""
    needle = q.strip().lower()
    address_ids, search_err = _address_search_ids(needle, db) if needle else (set(), None)
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
    lookup_err = _attach_addresses(rows, db)
    return {
        "customers": rows,
        "matched": len(matches),
        "total": total,
        # None when addresses loaded fine; otherwise why they didn't (shown on the page).
        "addresses_error": lookup_err or search_err,
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
