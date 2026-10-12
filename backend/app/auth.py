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

Trade-off: a role change or deactivation takes effect when the user's
current token expires (Supabase default: 1 hour), not instantly.
"""
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, status
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
    role = (claims.get("app_metadata") or {}).get("role")
    return AuthUser(user_id=claims["sub"], email=claims.get("email"), role=role or "")


def require_staff(user: AuthUser = Depends(current_user)) -> AuthUser:
    if user.role not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Staff access required")
    return user


def require_admin(user: AuthUser = Depends(current_user)) -> AuthUser:
    if user.role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
