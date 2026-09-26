"""HubSpot push: the client against a mocked CRM API (respx) and the endpoint against the real database."""

import json
from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from leadradar_auth.schemas import Principal
from leadradar_auth.security import create_access_token
from leadradar_core.db.session import async_session_factory, engine
from leadradar_core.main import create_app
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.activity.models import DomainEvent
from leadradar_core.modules.config.models import ScoringProfile, SignalQuestion
from leadradar_core.modules.config.presets import create_service_from_preset
from leadradar_core.modules.integrations.hubspot import (
    BASE_URL,
    PROPERTIES,
    Evidence,
    HubSpotClient,
    LeadSnapshot,
    note_body,
)
from leadradar_core.modules.intelligence.models import Signal
from leadradar_core.modules.leads.models import LeadScore
from leadradar_core.settings import settings
from sqlalchemy import select

TOKEN = "pat-test"


def snapshot(**overrides) -> LeadSnapshot:
    data = {
        "name": "Acme Logistics",
        "domain": "acme.example",
        "employees": 20000,
        "city": "Bonn",
        "service": "Intelligent Automation",
        "tier": "hot",
        "priority": 71.4,
        "fit": 90.0,
        "intent": 80.0,
        "risk": 0.0,
        "why_now": ["Runs agentic AI in customer service"],
        "evidence": [
            Evidence(
                quote="Acme <expands> agentic AI",
                source_name="reuters",
                url="https://news.example.com/a?x=1&y=2",
                event_date="2026-09-01",
                question="Is the company running AI initiatives?",
            )
        ],
        "lead_url": "http://localhost:5173/companies/1?service=2",
    }
    return LeadSnapshot(**(data | overrides))


@pytest.fixture
def hubspot() -> Iterator[respx.MockRouter]:
    HubSpotClient._ready.clear()
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.post("/crm/v3/properties/companies/groups").respond(409, json={"message": "exists"})
        mock.post("/crm/v3/properties/companies", name="properties").respond(201, json={})
        mock.get("/account-info/v3/details").respond(
            200, json={"portalId": 4242, "uiDomain": "app-eu1.hubspot.com"}
        )
        mock.post("/crm/v3/objects/notes", name="notes").respond(201, json={"id": "note-1"})
        yield mock


def test_note_escapes_html_and_links_the_evidence() -> None:
    body = note_body(snapshot())
    assert "“Acme &lt;expands&gt; agentic AI”" in body
    assert 'href="https://news.example.com/a?x=1&amp;y=2"' in body
    assert "HOT (71/100)" in body
    assert "Open in LeadRadar" in body


async def test_new_domain_creates_the_company_with_score_and_note(hubspot) -> None:
    hubspot.post("/crm/v3/objects/companies/search").respond(200, json={"total": 0, "results": []})
    create = hubspot.post("/crm/v3/objects/companies").respond(201, json={"id": "501"})

    async with HubSpotClient(TOKEN) as client:
        result = await client.push_lead(snapshot())

    assert (result.company_id, result.created, result.note_id) == ("501", True, "note-1")
    assert result.record_url == "https://app-eu1.hubspot.com/contacts/4242/record/0-2/501"
    props = json.loads(create.calls.last.request.content)["properties"]
    assert props["name"] == "Acme Logistics" and props["domain"] == "acme.example"
    assert props["leadradar_tier"] == "hot" and props["leadradar_priority"] == 71.4
    note = json.loads(hubspot["notes"].calls.last.request.content)
    assert note["associations"][0]["to"]["id"] == "501"
    assert create.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_existing_domain_is_updated_without_touching_name(hubspot) -> None:
    hubspot.post("/crm/v3/objects/companies/search").respond(
        200, json={"total": 1, "results": [{"id": "77"}]}
    )
    patch = hubspot.patch("/crm/v3/objects/companies/77").respond(200, json={"id": "77"})
    create = hubspot.post("/crm/v3/objects/companies")

    async with HubSpotClient(TOKEN) as client:
        result = await client.push_lead(snapshot())

    assert (result.company_id, result.created) == ("77", False)
    assert not create.called
    props = json.loads(patch.calls.last.request.content)["properties"]
    assert "name" not in props and "domain" not in props
    assert props["leadradar_url"].endswith("?service=2")


async def test_known_record_deleted_in_hubspot_falls_back_to_search(hubspot) -> None:
    hubspot.patch("/crm/v3/objects/companies/9").respond(404, json={"message": "gone"})
    search = hubspot.post("/crm/v3/objects/companies/search").respond(200, json={"results": []})
    hubspot.post("/crm/v3/objects/companies").respond(201, json={"id": "10"})

    async with HubSpotClient(TOKEN) as client:
        result = await client.push_lead(snapshot(), known_id="9")

    assert search.called and result.company_id == "10" and result.created


async def test_properties_are_created_once_per_portal(hubspot) -> None:
    hubspot.patch("/crm/v3/objects/companies/5").respond(200, json={"id": "5"})
    async with HubSpotClient(TOKEN) as client:
        await client.push_lead(snapshot(), known_id="5")
        await client.push_lead(snapshot(), known_id="5")
    assert hubspot["properties"].call_count == len(PROPERTIES)


# --- endpoint --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def fresh_engine() -> AsyncIterator[None]:
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
def client(monkeypatch) -> Iterator[TestClient]:
    settings.ENV = "test"
    monkeypatch.setattr(settings, "FEATURE_HUBSPOT", True)
    monkeypatch.setattr(settings, "HUBSPOT_PRIVATE_APP_TOKEN", TOKEN)
    with TestClient(create_app()) as test_client:
        yield test_client


def headers(org_id: UUID) -> dict[str, str]:
    token = create_access_token(Principal(user_id=org_id, org_id=org_id, email="s@x.io", role="sales"))
    return {"Authorization": f"Bearer {token}"}


async def scored_lead(org_id: UUID) -> tuple[UUID, UUID]:
    async with async_session_factory() as session, session.begin():
        service, _ = await create_service_from_preset(session, org_id, "intelligent_automation")
        company = Company(org_id=org_id, name="Acme", domain=f"acme-{uuid4().hex[:6]}.example")
        session.add(company)
        await session.flush()
        profile_id = (
            await session.execute(select(ScoringProfile.id).where(ScoringProfile.service_id == service.id))
        ).scalar_one()
        question = (
            await session.execute(
                select(SignalQuestion).where(SignalQuestion.service_id == service.id).limit(1)
            )
        ).scalar_one()
        session.add(
            LeadScore(
                org_id=org_id,
                company_id=company.id,
                service_id=service.id,
                scoring_profile_id=profile_id,
                fit=90,
                intent=60,
                risk=0,
                priority=66,
                tier="hot",
                why_now=[{"text": "Runs agentic AI", "polarity": "positive"}],
                is_current=True,
            )
        )
        for quote, status in (("Acme runs agentic AI", "active"), ("rejected quote", "rejected")):
            session.add(
                Signal(
                    org_id=org_id,
                    company_id=company.id,
                    service_id=service.id,
                    question_id=question.id,
                    question_key=question.key,
                    question_version=question.version,
                    category=question.category,
                    polarity=question.polarity,
                    url="https://news.example.com/x",
                    source_type="news",
                    source_name="reuters",
                    quote=quote,
                    summary="s",
                    strength="strong",
                    confidence=0.9,
                    status=status,
                )
            )
    await engine.dispose()  # the TestClient runs the app in its own event loop
    return company.id, service.id


def test_push_is_unavailable_when_not_configured(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "HUBSPOT_PRIVATE_APP_TOKEN", "")
    org_id = uuid4()
    assert client.get("/api/v1/integrations/hubspot", headers=headers(org_id)).json() == {"enabled": False}
    res = client.post(f"/api/v1/leads/{uuid4()}/push/hubspot", headers=headers(org_id))
    assert res.status_code == 503


async def test_push_sends_active_evidence_and_remembers_the_record(client, hubspot) -> None:
    org_id = uuid4()
    company_id, service_id = await scored_lead(org_id)
    hubspot.post("/crm/v3/objects/companies/search").respond(200, json={"results": []})
    hubspot.post("/crm/v3/objects/companies").respond(201, json={"id": "901"})

    res = client.post(f"/api/v1/leads/{company_id}/push/hubspot", headers=headers(org_id))
    await engine.dispose()

    assert res.status_code == 200, res.text
    assert res.json()["hubspot_company_id"] == "901" and res.json()["created"] is True
    note = json.loads(hubspot["notes"].calls.last.request.content)["properties"]["hs_note_body"]
    assert "Acme runs agentic AI" in note and "rejected quote" not in note
    assert f"/companies/{company_id}?service={service_id}" in note

    async with async_session_factory() as session:
        company = await session.get(Company, company_id)
        assert company.hubspot_company_id == "901" and company.hubspot_synced_at is not None
        event = (await session.execute(select(DomainEvent).where(DomainEvent.org_id == org_id))).scalar_one()
        assert event.type == "crm.pushed" and event.payload["crm_id"] == "901"


async def test_unscored_lead_and_bad_token_are_reported(client, hubspot) -> None:
    org_id = uuid4()
    async with async_session_factory() as session, session.begin():
        company = Company(org_id=org_id, name="New", domain=f"new-{uuid4().hex[:6]}.example")
        session.add(company)
    await engine.dispose()
    res = client.post(f"/api/v1/leads/{company.id}/push/hubspot", headers=headers(org_id))
    assert res.status_code == 409
    await engine.dispose()

    company_id, _ = await scored_lead(org_id)
    hubspot.post("/crm/v3/objects/companies/search").respond(401, json={"message": "Authentication failed"})
    res = client.post(f"/api/v1/leads/{company_id}/push/hubspot", headers=headers(org_id))
    assert res.status_code == 502 and "token" in res.json()["detail"]
