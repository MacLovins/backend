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


def test_discovery_search_and_accept(client: TestClient, auth_headers: dict[str, str]) -> None:
    # 1. Fetch available services first
    svc_res = client.get("/api/v1/services", headers=auth_headers)
    assert svc_res.status_code == 200
    services = svc_res.json()
    if not services:
        return

    service_id = services[0]["id"]

    # 2. Search discovery
    search_res = client.post(
        "/api/v1/discovery/search",
        json={"service_id": service_id, "limit": 5},
        headers=auth_headers,
    )
    assert search_res.status_code == 200
    search_data = search_res.json()
    assert "items" in search_data
    assert len(search_data["items"]) > 0
    candidate = search_data["items"][0]
    assert "domain" in candidate
    assert "fit_score" in candidate

    # 3. Accept discovery
    accept_res = client.post(
        "/api/v1/discovery/accept",
        json={
            "name": candidate["name"],
            "domain": candidate["domain"],
            "country_code": candidate.get("country_code"),
            "industry_ids": candidate.get("industry_ids", []),
            "employees": candidate.get("employees"),
            "service_id": service_id,
        },
        headers=auth_headers,
    )
    assert accept_res.status_code in (200, 201)
    acc_data = accept_res.json()
    assert acc_data["domain"] == candidate["domain"]
    assert acc_data["is_tracked"] is True
