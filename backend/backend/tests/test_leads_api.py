from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from leadradar_auth.schemas import Principal
from leadradar_auth.security import create_access_token
from leadradar_core.main import create_app
from leadradar_core.settings import settings


@pytest.fixture
def client() -> AsyncIterator[TestClient]:
    settings.ENV = "test"
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    principal = Principal(
        user_id=settings.DEFAULT_ORG_ID,
        org_id=settings.DEFAULT_ORG_ID,
        email="sales@leadradar.ai",
        role="sales",
    )
    token = create_access_token(principal)
    return {"Authorization": f"Bearer {token}"}


def test_list_leads_unauthorized(client: TestClient) -> None:
    res = client.get("/api/v1/leads")
    assert res.status_code == 401


def test_list_leads_authorized(client: TestClient, auth_headers: dict[str, str]) -> None:
    res = client.get("/api/v1/leads", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data
    assert isinstance(data["items"], list)


def test_export_leads_csv(client: TestClient, auth_headers: dict[str, str]) -> None:
    res = client.get("/api/v1/leads/export.csv", headers=auth_headers)
    assert res.status_code == 200
    assert "text/csv" in res.headers.get("content-type", "")
    assert "Company Name,Domain" in res.text
