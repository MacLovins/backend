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
        email="admin@leadradar.ai",
        role="admin",
    )
    token = create_access_token(principal)
    return {"Authorization": f"Bearer {token}"}


def test_runs_lifecycle_and_sse(client: TestClient, auth_headers: dict[str, str]) -> None:
    # 1. Create run
    create_res = client.post(
        "/api/v1/runs",
        json={"kind": "analyze", "company_ids": [], "service_ids": []},
        headers=auth_headers,
    )
    assert create_res.status_code == 201
    run_data = create_res.json()
    run_id = run_data["id"]
    assert run_data["status"] == "pending"

    # 2. Get run details
    get_res = client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)
    assert get_res.status_code == 200
    assert get_res.json()["id"] == run_id

    # 3. Connect to SSE stream (GET)
    sse_res = client.get(f"/api/v1/runs/{run_id}/events", headers=auth_headers)
    assert sse_res.status_code == 200
    assert "text/event-stream" in sse_res.headers.get("content-type", "")

    # 4. Connect to SSE stream (POST)
    post_sse_res = client.post(f"/api/v1/runs/{run_id}/events", headers=auth_headers)
    assert post_sse_res.status_code == 200
    assert "text/event-stream" in post_sse_res.headers.get("content-type", "")

    # 5. Cancel run
    cancel_res = client.post(f"/api/v1/runs/{run_id}/cancel", headers=auth_headers)
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "cancelled"
