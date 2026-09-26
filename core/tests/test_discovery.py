"""CO-08: discovery = service ICP (+ overrides) → parser.discover (faked, no network) → ai.fit_score."""

import asyncio
from collections.abc import Iterator
from uuid import uuid4

import leadradar_ai as ai
import leadradar_parser as parser
import pytest
from fastapi.testclient import TestClient
from leadradar_auth.schemas import Principal
from leadradar_auth.security import create_access_token
from leadradar_core.main import create_app
from leadradar_core.modules.discovery.schemas import DiscoverySearchIn
from leadradar_core.modules.discovery.service import build_discovery_query
from leadradar_core.settings import settings


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings.ENV = "test"
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    principal = Principal(
        user_id=uuid4(), org_id=settings.DEFAULT_ORG_ID, email="admin@leadradar.ai", role="admin"
    )
    return {"Authorization": f"Bearer {create_access_token(principal)}"}


@pytest.fixture
def tag() -> str:
    return uuid4().hex[:10]


@pytest.fixture
def service_id(client: TestClient, auth_headers: dict[str, str], tag: str) -> Iterator[str]:
    res = client.post(
        "/api/v1/services",
        json={"name": f"Discovery test {tag}", "slug": f"discovery-test-{tag}", "description": "test"},
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    sid = res.json()["id"]
    res = client.put(
        f"/api/v1/services/{sid}/icp",
        json={
            "countries": ["DE", "AT"],
            "industries_any": [],
            "employees_min": 1000,
            "nice_to_have": {"criteria": [{"kind": "industry_in", "values": ["logistics"], "weight": 2}]},
        },
        headers=auth_headers,
    )
    assert res.status_code == 200, res.text
    yield sid
    client.patch(f"/api/v1/services/{sid}", json={"is_active": False}, headers=auth_headers)


def _candidate(
    tag: str, name: str, country: str, industry: str, employees: int | None
) -> parser.CompanyCandidate:
    return parser.CompanyCandidate(
        name=f"{name} {tag}",
        domain=f"www.{name.lower()}-{tag}.com",
        country_code=country,
        industry_ids=[industry],
        employees=employees,
        revenue_eur=None,
        wikidata_qid=f"Q{abs(hash(name)) % 10**6}",
        lei=None,
        crunchbase_id=None,
    )


def test_build_discovery_query_from_icp_and_overrides() -> None:
    icp = ai.ICPConfig(
        countries=["DE", "AT"],
        employees_min=1000,
        nice_to_have=[ai.Criterion(kind="industry_in", values=["logistics", "airlines"])],
    )
    sid = uuid4()
    q = build_discovery_query(icp, DiscoverySearchIn(service_id=sid, limit=7))
    assert (q.countries, q.industries, q.employees_min, q.limit) == (
        ["DE", "AT"],
        ["logistics", "airlines"],
        1000,
        7,
    )

    q = build_discovery_query(
        icp,
        DiscoverySearchIn(
            service_id=sid, countries=["France"], industries=["Pharmaceuticals"], employees_min=50
        ),
    )
    assert (q.countries, q.industries, q.employees_min) == (["FR"], ["pharma"], 50)

    q = build_discovery_query(
        icp, DiscoverySearchIn(service_id=sid, country="Germany", industry="energy_utilities")
    )
    assert (q.countries, q.industries) == (["DE"], ["energy_utilities"])


def test_discovery_search_uses_parser_and_fit(
    client: TestClient,
    auth_headers: dict[str, str],
    service_id: str,
    tag: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[parser.DiscoveryQuery] = []

    async def fake_discover(query: parser.DiscoveryQuery, *, http=None) -> list[parser.CompanyCandidate]:
        seen.append(query)
        return [
            _candidate(tag, "Small", "DE", "logistics", 200),  # fails must-have employees_min → fit 0
            _candidate(tag, "Bank", "DE", "banking", 5000),  # passes must-have, misses nice-to-have
            _candidate(tag, "Freight", "AT", "logistics", 20000),  # full match
            _candidate(tag, "Freight", "AT", "logistics", 20000),  # duplicate domain
        ]

    monkeypatch.setattr(parser, "discover", fake_discover)
    try:
        # an already tracked company is flagged
        res = client.post(
            "/api/v1/companies",
            json={"name": f"Bank {tag}", "domain": f"bank-{tag}.com"},
            headers=auth_headers,
        )
        assert res.status_code == 201

        res = client.post(
            "/api/v1/discovery/search", json={"service_id": service_id, "limit": 10}, headers=auth_headers
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert seen[0].countries == ["DE", "AT"]
        assert seen[0].industries == ["logistics"]  # from nice_to_have: industries_any is empty
        assert seen[0].employees_min == 1000
        assert data["query"]["countries"] == ["DE", "AT"]

        items = data["items"]
        assert [i["domain"] for i in items] == [f"freight-{tag}.com", f"bank-{tag}.com", f"small-{tag}.com"]
        assert items[0]["fit_score"] == 100.0
        assert items[1]["fit_score"] == 0.0 and items[1]["must_have_passed"] is True
        assert items[2]["fit_score"] == 0.0 and items[2]["must_have_passed"] is False
        assert items[1]["already_tracked"] is True
        assert items[0]["already_tracked"] is False
        assert items[0]["fit_details"] and items[0]["reason"]
        assert data["total"] == 3

        accept = client.post(
            "/api/v1/discovery/accept",
            json={
                "name": items[0]["name"],
                "domain": items[0]["domain"],
                "country_code": items[0]["country_code"],
                "industry_ids": items[0]["industry_ids"],
                "employees": items[0]["employees"],
                "wikidata_qid": items[0]["wikidata_qid"],
                "service_id": service_id,
            },
            headers=auth_headers,
        )
        assert accept.status_code == 201, accept.text
        company = accept.json()
        assert company["origin"] == "discovery" and company["is_tracked"] is True
        assert company["wikidata_qid"] == items[0]["wikidata_qid"]
    finally:
        res = client.get("/api/v1/companies", params={"q": tag, "page_size": 100}, headers=auth_headers)
        for c in res.json()["items"]:
            client.delete(f"/api/v1/companies/{c['id']}", headers=auth_headers)


def test_discovery_timeout_returns_504(
    client: TestClient,
    auth_headers: dict[str, str],
    service_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_discover(query, *, http=None):
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(parser, "discover", slow_discover)
    monkeypatch.setattr(settings, "DISCOVERY_TIMEOUT_S", 0.05)
    res = client.post("/api/v1/discovery/search", json={"service_id": service_id}, headers=auth_headers)
    assert res.status_code == 504
    assert res.json()["error"]["code"] == "discovery_timeout"


def test_discovery_requires_countries_and_industries(
    client: TestClient,
    auth_headers: dict[str, str],
    service_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(query, *, http=None):
        raise AssertionError("parser must not be called")

    monkeypatch.setattr(parser, "discover", never)
    res = client.post(
        "/api/v1/discovery/search",
        json={"service_id": service_id, "industries": ["Underwater basket weaving"]},
        headers=auth_headers,
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "discovery_query_incomplete"


def test_discovery_unknown_service_and_auth(client: TestClient, auth_headers: dict[str, str]) -> None:
    res = client.post("/api/v1/discovery/search", json={"service_id": str(uuid4())}, headers=auth_headers)
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"
    assert client.post("/api/v1/discovery/search", json={"service_id": str(uuid4())}).status_code == 401


def test_unauthenticated_discovery_list_removed(client: TestClient) -> None:
    assert client.get("/api/v1/discovery").status_code in (404, 405)
    assert "/api/v1/discovery" not in client.app.openapi()["paths"]
