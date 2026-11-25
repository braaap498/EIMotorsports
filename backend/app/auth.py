"""
Back-office authentication.

Staff sign in through Supabase Auth and send the access token as
`Authorization: Bearer <jwt>`. This module verifies it locally (no network
call per request) and reads the staff role from `app_metadata.role`.

Why app_metadata: it can only be written with the service role / SQL, never
by the user themselves (unlike user_metadata), so it is safe to trust.

Two signing modes are supported, chosen by the token header:
  * RS256 / ES256 - asymmetric; verified against the project's JWKS.
  * HS256         - legacy shared secret; only if SUPABASE_JWT_SECRET is set.
Anything else (including alg=none) is rejected.

Employees ("staff" role) can be given individual permissions (see
app/permissions.py), stored the same way in `app_metadata.permissions`.

Trade-off: a role or permission change, or a deactivation, takes effect when
the user's current token expires (Supabase default: 1 hour), not instantly.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)

STAFF_ROLES = {"owner", "admin", "staff"}
ADMIN_ROLES = {"owner", "admin"}


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    email: str | None
    role: str
    permissions: frozenset[str] = frozenset()
    aal: str = ""  # Supabase assurance level: aal1 = password only, aal2 = password + second factor
    password_changed_at: str = ""  # app_metadata.password_changed_at (set by the database trigger in the security notes)


@lru_cache
def _jwks_client(url: str) -> PyJWKClient:
    # PyJWKClient caches keys, so this is not a network call per request.
    return PyJWKClient(url, cache_keys=True, lifespan=3600)


def _unauthorized(detail: str = "Invalid or missing credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def decode_token(token: str, settings: Settings) -> dict:
    try:
        alg = jwt.get_unverified_header(token).get("alg")
        common = dict(
            audience=settings.supabase_jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub"]},
        )
        if alg == "HS256" and settings.supabase_jwt_secret:
            return jwt.decode(token, settings.supabase_jwt_secret, algorithms=["HS256"], **common)
        if alg in ("RS256", "ES256"):
            key = _jwks_client(settings.jwks_url).get_signing_key_from_jwt(token).key
            return jwt.decode(token, key, algorithms=["RS256", "ES256"], **common)
    except jwt.PyJWTError:
        raise _unauthorized()
    raise _unauthorized("Unsupported token type")


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> AuthUser:
    if creds is None:
        raise _unauthorized()
    claims = decode_token(creds.credentials, settings)
    meta = claims.get("app_metadata") or {}
    perms = meta.get("permissions")
    if not isinstance(perms, list):
        perms = []
    return AuthUser(
        user_id=claims["sub"],
        email=claims.get("email"),
        role=meta.get("role") or "",
        permissions=frozenset(p for p in perms if isinstance(p, str)),
        aal=str(claims.get("aal") or ""),
        password_changed_at=str(meta.get("password_changed_at") or ""),
    )


MFA_REQUIRED = "MFA required: set up or enter your authenticator code."
PASSWORD_EXPIRED = "Your password has expired. Ask an admin to set a new one, or change it from the sign-in page."


def _password_age_days(stamp: str) -> int | None:
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).days


def require_staff(user: AuthUser = Depends(current_user), settings: Settings = Depends(get_settings)) -> AuthUser:
    if user.role not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Staff access required")
    # Two optional switches (off unless the environment turns them on): a second factor, and a password age limit.
    if settings.require_mfa and user.aal != "aal2":
        raise HTTPException(status_code=403, detail=MFA_REQUIRED)
    if settings.password_max_age_days > 0 and user.password_changed_at:
        age = _password_age_days(user.password_changed_at)
        if age is not None and age > settings.password_max_age_days:
            raise HTTPException(status_code=403, detail=PASSWORD_EXPIRED)
    return user


def require_admin(user: AuthUser = Depends(current_user)) -> AuthUser:
    if user.role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def has_perm(user: AuthUser, key: str) -> bool:
    """Owners and admins can do everything; employees only what they were granted."""
    return user.role in ADMIN_ROLES or key in user.permissions


def needs(*keys: str):
    """
    Mark a route as open to employees who hold ANY of these permissions
    (owners and admins always pass). Use it under the route decorator:

        @router.get("/products")
        @needs("products.view")
        def list_products(...): ...

    A route in a router that uses `require_route_access` WITHOUT this marker is
    admin-only. Forgetting it therefore locks employees out; it never lets
    them in.
    """

    def mark(fn):
        fn._needs_any = tuple(keys)
        return fn

    return mark


def require_route_access(request: Request, user: AuthUser = Depends(require_staff)) -> AuthUser:
    """Router-level guard that enforces the `needs(...)` marker on each route."""
    if user.role in ADMIN_ROLES:
        return user
    keys = getattr(request.scope.get("endpoint"), "_needs_any", ())
    if keys and any(k in user.permissions for k in keys):
        return user
    raise HTTPException(status_code=403, detail="You don't have permission to do that")
