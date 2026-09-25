from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from leadradar_core.main import create_app
from leadradar_core.settings import settings


@pytest.fixture
def client() -> AsyncIterator[TestClient]:
    settings.ENV = "test"
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def test_meta_industries(client: TestClient) -> None:
    res = client.get("/api/v1/meta/industries")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 8
    assert any(i["id"] == "logistics" for i in data)


def test_meta_countries(client: TestClient) -> None:
    res = client.get("/api/v1/meta/countries")
    assert res.status_code == 200
    data = res.json()
    assert any(c["code"] == "DE" for c in data)


def test_meta_presets(client: TestClient) -> None:
    res = client.get("/api/v1/meta/presets")
    assert res.status_code == 200
    data = res.json()
    assert any(p["key"] == "intelligent_automation" for p in data)
    assert any(p["key"] == "cybersecurity" for p in data)


def test_meta_labels(client: TestClient) -> None:
    res = client.get("/api/v1/meta/labels")
    assert res.status_code == 200
    data = res.json()
    assert "categories" in data
    assert "weights" in data
