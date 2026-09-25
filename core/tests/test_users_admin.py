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
def admin_headers() -> dict[str, str]:
    principal = Principal(
        user_id=settings.DEFAULT_ORG_ID,
        org_id=settings.DEFAULT_ORG_ID,
        email="admin@leadradar.ai",
        role="admin",
    )
    token = create_access_token(principal)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sales_headers() -> dict[str, str]:
    principal = Principal(
        user_id=settings.DEFAULT_ORG_ID,
        org_id=settings.DEFAULT_ORG_ID,
        email="sales@leadradar.ai",
        role="sales",
    )
    token = create_access_token(principal)
    return {"Authorization": f"Bearer {token}"}


def test_users_admin_access_control(
    client: TestClient, admin_headers: dict[str, str], sales_headers: dict[str, str]
) -> None:
    # 1. Anonymous forbidden
    anon_res = client.get("/api/v1/auth/users")
    assert anon_res.status_code == 401

    # 2. Sales forbidden
    sales_res = client.get("/api/v1/auth/users", headers=sales_headers)
    assert sales_res.status_code == 403

    # 3. Admin authorized
    admin_res = client.get("/api/v1/auth/users", headers=admin_headers)
    assert admin_res.status_code == 200
    assert isinstance(admin_res.json(), list)
