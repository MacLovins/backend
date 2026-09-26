"""Startup secret checks, Secure cookies outside dev, Origin check, docs outside dev, login rate limit."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from leadradar_auth import AuthSettings, auth_settings, reset_login_rate_limit
from leadradar_auth.schemas import Principal
from leadradar_auth.security import create_access_token
from leadradar_auth.settings import DEV_JWT_SECRET
from leadradar_core.main import create_app
from leadradar_core.security import InsecureConfigurationError, configure_security, origin_allowed
from leadradar_core.settings import AppSettings, settings

REAL_SECRET = "0123456789abcdef0123456789abcdef-real-secret"


@pytest.fixture(autouse=True)
def _restore_auth_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # create_app() resolves COOKIE_SECURE on the shared auth settings; restore it after every test
    monkeypatch.setattr(auth_settings, "COOKIE_SECURE", auth_settings.COOKIE_SECURE)
    monkeypatch.setattr(auth_settings, "JWT_SECRET", auth_settings.JWT_SECRET)
    monkeypatch.setattr(settings, "ENV", "test")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def _token() -> str:
    return create_access_token(
        Principal(user_id=uuid4(), org_id=settings.DEFAULT_ORG_ID, email="sales@leadradar.ai", role="sales")
    )


# --- secrets & cookies ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret", [DEV_JWT_SECRET, "leadradar-super-secret-development-jwt-key-32chars!", "short", ""]
)
def test_security_refuses_insecure_jwt_secret_outside_dev(secret: str) -> None:
    with pytest.raises(InsecureConfigurationError):
        configure_security(AppSettings(ENV="demo"), AuthSettings(JWT_SECRET=secret))


def test_security_cookie_secure_defaults() -> None:
    prod_auth = AuthSettings(JWT_SECRET=REAL_SECRET)
    configure_security(AppSettings(ENV="demo"), prod_auth)
    assert prod_auth.COOKIE_SECURE is True

    dev_auth = AuthSettings()
    configure_security(AppSettings(ENV="dev"), dev_auth)  # the dev secret is fine in dev
    assert dev_auth.COOKIE_SECURE is False

    explicit = AuthSettings(JWT_SECRET=REAL_SECRET, COOKIE_SECURE=False)
    configure_security(AppSettings(ENV="demo"), explicit)
    assert explicit.COOKIE_SECURE is False


def test_security_create_app_fails_without_secret_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENV", "demo")
    monkeypatch.setattr(auth_settings, "JWT_SECRET", DEV_JWT_SECRET)
    with pytest.raises(InsecureConfigurationError):
        create_app()


def test_security_docs_disabled_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENV", "demo")
    monkeypatch.setattr(auth_settings, "JWT_SECRET", REAL_SECRET)
    monkeypatch.setattr(auth_settings, "COOKIE_SECURE", None)
    app = create_app()
    assert auth_settings.COOKIE_SECURE is True
    with TestClient(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
        assert client.get("/health").status_code == 200
    assert "/api/v1/companies" in app.openapi()["paths"]  # the snapshot export still works


def test_security_docs_enabled_in_dev(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


# --- Origin check -----------------------------------------------------------------------------------


def test_origin_allowed_rules() -> None:
    public = "https://leadradar.example.com"
    assert origin_allowed("https://leadradar.example.com", public, "demo")
    assert origin_allowed("https://LeadRadar.example.com/", public, "demo")
    assert not origin_allowed("http://localhost:5173", public, "demo")
    assert not origin_allowed("https://evil.example", public, "demo")
    assert not origin_allowed("null", public, "demo")
    assert origin_allowed("http://localhost:5173", public, "dev")
    assert origin_allowed("http://127.0.0.1:3000", public, "dev")
    assert not origin_allowed("https://evil.example", public, "dev")


def test_origin_check_blocks_cross_origin_cookie_writes(client: TestClient) -> None:
    token = _token()
    domain = f"origin-{uuid4().hex[:8]}.com"
    client.cookies.set(auth_settings.COOKIE_NAME, token)

    res = client.post(
        "/api/v1/companies",
        json={"name": "Evil", "domain": domain},
        headers={"Origin": "https://evil.example"},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "origin_not_allowed"

    res = client.post(
        "/api/v1/companies",
        json={"name": "Evil", "domain": domain},
        headers={"Referer": "https://evil.example/x"},
    )
    assert res.status_code == 403

    # safe methods are not checked
    assert client.get("/api/v1/companies", headers={"Origin": "https://evil.example"}).status_code == 200

    res = client.post(
        "/api/v1/companies", json={"name": "Ok", "domain": domain}, headers={"Origin": settings.PUBLIC_ORIGIN}
    )
    assert res.status_code == 201, res.text
    company_id = res.json()["id"]
    client.cookies.clear()

    # bearer-authenticated requests are not CSRF-able: no Origin check
    admin = create_access_token(
        Principal(user_id=uuid4(), org_id=settings.DEFAULT_ORG_ID, email="a@leadradar.ai", role="admin")
    )
    res = client.delete(
        f"/api/v1/companies/{company_id}",
        headers={"Authorization": f"Bearer {admin}", "Origin": "https://evil.example"},
    )
    assert res.status_code == 204


# --- login rate limit ---------------------------------------------------------------------------------


def test_login_rate_limit_returns_429(client: TestClient) -> None:
    reset_login_rate_limit()
    email = f"nobody-{uuid4().hex[:8]}@leadradar.ai"
    try:
        for _ in range(auth_settings.LOGIN_RATE_LIMIT):
            res = client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
            assert res.status_code == 401
            assert res.json()["error"]["code"] == "invalid_credentials"
        res = client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
        assert res.status_code == 429
        assert res.json()["error"]["code"] == "rate_limited"
        assert int(res.headers["retry-after"]) >= 1
        # another e-mail from the same IP is not affected
        other = client.post("/api/v1/auth/login", json={"email": f"x-{email}", "password": "wrong-password"})
        assert other.status_code == 401
    finally:
        reset_login_rate_limit()
