import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth
from app.config import get_settings
from app.database import get_db
from app.main import app

S = get_settings()
client = TestClient(app)


def make_token(role="staff", *, secret=None, aud=None, iss=None, exp_in=3600, alg="HS256", key=None, kid=None):
    payload = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "email": "ian@example.com",
        "aud": aud or S.supabase_jwt_audience,
        "iss": iss or S.jwt_issuer,
        "exp": int(time.time()) + exp_in,
        "app_metadata": {"role": role} if role else {},
    }
    headers = {"kid": kid} if kid else None
    return jwt.encode(payload, key or secret or S.supabase_jwt_secret, algorithm=alg, headers=headers)


def get_me(token=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get("/admin/me", headers=h)


def test_health_is_public():
    assert client.get("/health").json() == {"status": "ok"}


def test_admin_requires_token():
    assert get_me().status_code == 401


@pytest.mark.parametrize("role", ["owner", "admin", "staff"])
def test_staff_roles_allowed(role):
    r = get_me(make_token(role))
    assert r.status_code == 200 and r.json()["role"] == role


def test_signed_in_but_no_role_is_forbidden():
    assert get_me(make_token(role=None)).status_code == 403


def test_unknown_role_is_forbidden():
    assert get_me(make_token(role="customer")).status_code == 403


def test_expired_token_rejected():
    assert get_me(make_token(exp_in=-10)).status_code == 401


def test_wrong_secret_rejected():
    assert get_me(make_token(secret="x" * 40)).status_code == 401


def test_wrong_audience_rejected():
    assert get_me(make_token(aud="anon")).status_code == 401


def test_wrong_issuer_rejected():
    assert get_me(make_token(iss="https://evil.example/auth/v1")).status_code == 401


def test_alg_none_rejected():
    payload = {"sub": "x", "aud": S.supabase_jwt_audience, "iss": S.jwt_issuer,
               "exp": int(time.time()) + 3600, "app_metadata": {"role": "owner"}}
    token = jwt.encode(payload, key=None, algorithm="none")
    assert get_me(token).status_code == 401


def test_garbage_token_rejected():
    assert get_me("not.a.jwt").status_code == 401


def test_hs256_ignored_when_no_secret_configured(monkeypatch):
    """Alg-confusion guard: with no shared secret, an HS256 token is never accepted."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "")
    get_settings.cache_clear()
    try:
        assert get_me(make_token("owner")).status_code == 401
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_es256_via_jwks(monkeypatch):
    """Asymmetric projects: token verified against the (stubbed) JWKS public key."""
    private = ec.generate_private_key(ec.SECP256R1())

    class StubSigningKey:
        key = private.public_key()

    class StubClient:
        def get_signing_key_from_jwt(self, token):
            return StubSigningKey()

    monkeypatch.setattr(auth, "_jwks_client", lambda url: StubClient())
    token = make_token("admin", alg="ES256", key=private, kid="k1")
    r = get_me(token)
    assert r.status_code == 200 and r.json()["role"] == "admin"

    # a token signed by a different key must fail
    other = ec.generate_private_key(ec.SECP256R1())
    assert get_me(make_token("admin", alg="ES256", key=other, kid="k1")).status_code == 401


def test_ping_db_requires_auth_then_works():
    engine = create_engine("sqlite://")
    Session = sessionmaker(bind=engine)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    try:
        assert client.get("/admin/ping-db").status_code == 401
        r = client.get("/admin/ping-db", headers={"Authorization": f"Bearer {make_token('staff')}"})
        assert r.json() == {"database": "ok"}
    finally:
        app.dependency_overrides.clear()
