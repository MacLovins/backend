"""Unified error format {"error": {"code", "message", "details"}} for every error source (SPEC §1.4.1)."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from leadradar_auth.schemas import Principal
from leadradar_auth.security import create_access_token
from leadradar_core.errors import DomainException, NotFoundException, http_error_body
from leadradar_core.main import create_app
from leadradar_core.settings import settings


@pytest.fixture
def app():
    settings.ENV = "test"
    application = create_app()

    @application.get("/_test/domain")
    async def raise_domain() -> None:
        raise DomainException("quota_exhausted", "No quota left", {"model": "m"}, status_code=429)

    @application.get("/_test/not-found")
    async def raise_not_found() -> None:
        raise NotFoundException("Thing not found", {"id": 1})

    @application.get("/_test/http-dict")
    async def raise_http_dict() -> None:
        raise HTTPException(status_code=409, detail={"code": "already_running", "message": "Run in progress"})

    @application.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError("boom")

    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    principal = Principal(
        user_id=uuid4(), org_id=settings.DEFAULT_ORG_ID, email="sales@leadradar.ai", role="sales"
    )
    return {"Authorization": f"Bearer {create_access_token(principal)}"}


def _assert_shape(body: dict) -> dict:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert isinstance(body["error"]["details"], dict)
    return body["error"]


def test_errors_http_exception_401_and_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    res = client.get("/api/v1/companies")
    assert res.status_code == 401
    assert res.headers.get("www-authenticate") == "Bearer"
    assert _assert_shape(res.json())["code"] == "unauthorized"

    res = client.get(f"/api/v1/companies/{uuid4()}", headers=auth_headers)
    assert res.status_code == 404
    err = _assert_shape(res.json())
    assert (err["code"], err["message"]) == ("not_found", "Company not found")

    res = client.get("/api/v1/no-such-route")
    assert res.status_code == 404
    assert _assert_shape(res.json())["code"] == "not_found"


def test_errors_forbidden(client: TestClient, auth_headers: dict[str, str]) -> None:
    res = client.post("/api/v1/services", json={"name": "x", "slug": "x"}, headers=auth_headers)
    assert res.status_code == 403
    assert _assert_shape(res.json())["code"] == "forbidden"


def test_errors_validation_422(client: TestClient, auth_headers: dict[str, str]) -> None:
    res = client.get("/api/v1/companies", params={"page": 0}, headers=auth_headers)
    assert res.status_code == 422
    err = _assert_shape(res.json())
    assert err["code"] == "validation_error"
    assert err["details"]["errors"][0]["loc"] == ["query", "page"]

    res = client.post("/api/v1/companies", json={"name": ""}, headers=auth_headers)
    assert res.status_code == 422
    locs = [e["loc"] for e in _assert_shape(res.json())["details"]["errors"]]
    assert ["body", "domain"] in locs


def test_errors_domain_and_custom(client: TestClient) -> None:
    res = client.get("/_test/domain")
    assert res.status_code == 429
    assert _assert_shape(res.json()) == {
        "code": "quota_exhausted",
        "message": "No quota left",
        "details": {"model": "m"},
    }

    res = client.get("/_test/not-found")
    assert res.status_code == 404
    assert _assert_shape(res.json())["details"] == {"id": 1}

    res = client.get("/_test/http-dict")
    assert res.status_code == 409
    err = _assert_shape(res.json())
    assert (err["code"], err["message"]) == ("already_running", "Run in progress")


def test_errors_unhandled_500(client: TestClient) -> None:
    res = client.get("/_test/crash")
    assert res.status_code == 500
    assert _assert_shape(res.json())["code"] == "internal_error"


def test_errors_code_like_detail() -> None:
    body = http_error_body(401, "invalid_credentials")
    assert body["error"]["code"] == "invalid_credentials"
    assert http_error_body(400, "Invalid domain")["error"] == {
        "code": "bad_request",
        "message": "Invalid domain",
        "details": {},
    }


def test_errors_openapi_documents_error_shape(app) -> None:
    schema = app.openapi()
    op = schema["paths"]["/api/v1/companies"]["get"]
    assert op["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
