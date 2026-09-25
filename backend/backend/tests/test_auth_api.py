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


def test_auth_me_unauthorized(client: TestClient) -> None:
    res = client.get("/api/v1/auth/me")
    assert res.status_code == 401


def test_auth_me_with_bearer_token(client: TestClient) -> None:
    principal = Principal(
        user_id=settings.DEFAULT_ORG_ID,
        org_id=settings.DEFAULT_ORG_ID,
        email="test_user@leadradar.ai",
        role="sales",
        full_name="Test Sales",
    )
    token = create_access_token(principal)

    res = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    # If user is not physically in session DB under test, get_me looks up by id:
    # 404 is expected when user_id is random test UUID not in DB, but auth check passed!
    assert res.status_code in (200, 404)


def test_admin_role_restriction(client: TestClient) -> None:
    # Sales token attempting to perform admin POST /services
    sales_principal = Principal(
        user_id=settings.DEFAULT_ORG_ID,
        org_id=settings.DEFAULT_ORG_ID,
        email="sales_rep@leadradar.ai",
        role="sales",
    )
    token = create_access_token(sales_principal)

    res = client.post(
        "/api/v1/services",
        json={"name": "Forbidden", "slug": "forbidden"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Sales role must receive 403 Forbidden on config mutation
    assert res.status_code == 403
