"""Config API: validated questions/ICP/rules, save + rescore atomically, soft delete of questions."""

from collections.abc import AsyncIterator
from typing import get_args
from uuid import UUID, uuid4

import httpx
import leadradar_ai as ai
import pytest
from _lead_fixtures import ensure_service, headers, question, seed_lead, signal
from leadradar_core.adapters import mapping
from leadradar_core.db.session import async_session_factory, engine
from leadradar_core.main import create_app
from leadradar_core.modules.config import router as config_router
from leadradar_core.modules.config import schemas
from leadradar_core.modules.leads.models import LeadScore
from leadradar_core.settings import settings
from sqlalchemy import select


@pytest.fixture(autouse=True)
async def fresh_engine() -> AsyncIterator[None]:
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    settings.ENV = "test"
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def org_id() -> UUID:
    return uuid4()


async def current_intent(company_id: UUID, service_id: UUID) -> float:
    async with async_session_factory() as session:
        return float(
            (
                await session.execute(
                    select(LeadScore.intent).where(
                        LeadScore.company_id == company_id,
                        LeadScore.service_id == service_id,
                        LeadScore.is_current.is_(True),
                    )
                )
            ).scalar_one()
        )


def test_core_enums_match_the_ai_contracts():
    fields = ai.QuestionConfig.model_fields
    assert set(get_args(schemas.Weight)) == set(get_args(fields["weight"].annotation))
    assert set(get_args(schemas.Polarity)) == set(get_args(fields["polarity"].annotation))
    (source_type,) = get_args(fields["source_types"].annotation)  # set[SourceType]
    assert set(get_args(schemas.SourceType)) == set(get_args(source_type))
    rule_fields = ai.RuleConfig.model_fields
    assert set(get_args(schemas.RuleKind)) == set(get_args(rule_fields["kind"].annotation))
    assert set(get_args(schemas.RuleAction)) == set(get_args(rule_fields["action"].annotation))


@pytest.mark.parametrize(
    "patch",
    [
        {"weight": "huge"},
        {"polarity": "neutral"},
        {"category": "astrology"},
        {"source_types": ["tv"]},
        {"source_types": []},
        {"recency_days": 0},
    ],
)
async def test_invalid_question_fields_are_rejected(org_id, client, patch):
    service_id = await ensure_service(org_id)
    q = await question(service_id, "ia_cost")
    h = headers(org_id)
    assert (await client.patch(f"/api/v1/questions/{q.id}", headers=h, json=patch)).status_code == 422
    body = {"key": f"k_{uuid4().hex[:6]}", "text": "Is it?", **patch}
    assert (
        await client.post(f"/api/v1/services/{service_id}/questions", headers=h, json=body)
    ).status_code == 422
    # nothing was stored, rescoring still works
    assert (await question(service_id, "ia_cost")).weight == q.weight
    ok = await client.put(f"/api/v1/services/{service_id}/scoring-profile", headers=h, json={"params": {}})
    assert ok.status_code == 200, ok.text


async def test_valid_question_create_and_duplicate_key(org_id, client):
    service_id = await ensure_service(org_id)
    h = headers(org_id)
    body = {
        "key": "ia_new",
        "text": "Does the company run a robotics program?",
        "category": "ai_automation",
        "polarity": "positive",
        "weight": "low",
        "source_types": ["news", "news", "jobs"],
    }
    res = await client.post(f"/api/v1/services/{service_id}/questions", headers=h, json=body)
    assert res.status_code == 201, res.text
    assert res.json()["source_types"] == ["news", "jobs"]
    dup = await client.post(f"/api/v1/services/{service_id}/questions", headers=h, json=body)
    assert dup.status_code == 409


GUIDE = {"weak": "One RPA job ad.", "moderate": "A few RPA roles.", "strong": "An RPA team being built."}


async def test_question_temperature_create_update_and_reset(org_id, client):
    service_id = await ensure_service(org_id)
    h = headers(org_id)
    body = {"key": "ia_rpa_team", "text": "Is the company building an RPA team?", "category": "hiring"}
    created = await client.post(
        f"/api/v1/services/{service_id}/questions", headers=h, json={**body, "temperature": GUIDE}
    )
    assert created.status_code == 201, created.text
    assert created.json()["temperature"] == GUIDE and created.json()["version"] == 1
    plain = await client.post(
        f"/api/v1/services/{service_id}/questions", headers=h, json={**body, "key": "ia_plain"}
    )
    assert plain.status_code == 201 and plain.json()["temperature"] is None  # the category default
    listed = (await client.get(f"/api/v1/services/{service_id}/questions", headers=h)).json()
    assert {q["key"]: q["temperature"] for q in listed}["ia_rpa_team"] == GUIDE

    url = f"/api/v1/questions/{created.json()['id']}"
    same = await client.patch(url, headers=h, json={"temperature": {**GUIDE, "weak": " One RPA job ad. "}})
    assert same.status_code == 200, same.text
    assert same.json()["version"] == 1  # the same guide (stripped): the question means the same

    hotter = {**GUIDE, "strong": "A hiring drive for automation."}
    changed = (await client.patch(url, headers=h, json={"temperature": hotter})).json()
    assert changed["temperature"] == hotter and changed["version"] == 2  # new extraction (fingerprint)
    assert changed["keywords_status"] == "pending"
    stored = await question(service_id, "ia_rpa_team")
    assert ai.temperature_guide(mapping.question_config(stored)) == hotter  # what the prompt shows

    reset = (await client.patch(url, headers=h, json={"temperature": None})).json()
    assert reset["temperature"] is None and reset["version"] == 3
    stored = await question(service_id, "ia_rpa_team")
    assert mapping.question_config(stored).temperature == {}
    assert ai.temperature_guide(mapping.question_config(stored)) == ai.DEFAULT_TEMPERATURE["hiring"]


@pytest.mark.parametrize(
    "temperature",
    [
        {"weak": "w", "moderate": "m"},
        {**GUIDE, "hot": "h"},
        {**GUIDE, "weak": " "},
        {**GUIDE, "strong": "s" * 301},
        "hot",
    ],
)
async def test_invalid_question_temperature_is_rejected(org_id, client, temperature):
    service_id = await ensure_service(org_id)
    q = await question(service_id, "ia_hiring")
    h = headers(org_id)
    patched = await client.patch(f"/api/v1/questions/{q.id}", headers=h, json={"temperature": temperature})
    assert patched.status_code == 422, patched.text
    body = {"key": f"k_{uuid4().hex[:6]}", "text": "Is it?", "temperature": temperature}
    assert (
        await client.post(f"/api/v1/services/{service_id}/questions", headers=h, json=body)
    ).status_code == 422
    assert (await question(service_id, "ia_hiring")).temperature is None


async def test_preset_question_temperature_is_copied_to_the_question(org_id, client, monkeypatch):
    preset = ai.load_preset("intelligent_automation")
    questions = [
        q.model_copy(update={"temperature": GUIDE} if q.key == "ia_hiring" else {}) for q in preset.questions
    ]
    monkeypatch.setattr(ai, "load_preset", lambda key: preset.model_copy(update={"questions": questions}))
    service_id = await ensure_service(org_id)
    listed = (await client.get(f"/api/v1/services/{service_id}/questions", headers=headers(org_id))).json()
    by_key = {q["key"]: q["temperature"] for q in listed}
    assert by_key.pop("ia_hiring") == GUIDE
    assert by_key and set(by_key.values()) == {None}  # no guide in the preset: the category default


async def test_icp_nice_to_have_is_validated(org_id, client):
    service_id = await ensure_service(org_id)
    h = headers(org_id)
    url = f"/api/v1/services/{service_id}/icp"
    bad = [
        {"nice_to_have": {"criteria": [{"kind": "moon_phase", "values": ["full"]}]}},
        {"nice_to_have": {"criteria": [{"kind": "industry_in", "values": ["x"], "weight": -1}]}},
        {"nice_to_have": {"rules": []}},
        {"employees_min": 5000, "employees_max": 100},
    ]
    for body in bad:
        assert (await client.put(url, headers=h, json=body)).status_code == 422, body
    good = {
        "countries": ["DE"],
        "employees_min": 1000,
        "nice_to_have": {"criteria": [{"kind": "industry_in", "values": ["logistics"], "weight": 2}]},
    }
    res = await client.put(url, headers=h, json=good)
    assert res.status_code == 200, res.text
    assert res.json()["nice_to_have"]["criteria"][0]["kind"] == "industry_in"


async def test_rule_action_is_required(org_id, client):
    service_id = await ensure_service(org_id)
    h = headers(org_id)
    url = f"/api/v1/services/{service_id}/rules"
    rule = {
        "name": "tiny",
        "kind": "firmographic",
        "condition": {"field": "employees", "op": "lt", "value": 10},
    }
    assert (await client.post(url, headers=h, json=rule)).status_code == 422
    assert (await client.post(url, headers=h, json={**rule, "action": "disqualify"})).status_code == 422
    res = await client.post(url, headers=h, json={**rule, "action": "flag"})
    assert res.status_code == 201, res.text
    assert res.json()["cap_value"] is None


async def test_weight_change_and_rescore_share_one_transaction(org_id, client, monkeypatch):
    seeded = await seed_lead(org_id)
    q = await question(seeded.service_id, "ia_ai_projects")
    before = await current_intent(seeded.company_id, seeded.service_id)
    h = headers(org_id)

    async def broken_rescore(*args, **kwargs):
        raise RuntimeError("rescore failed")

    monkeypatch.setattr(config_router, "rescore_service", broken_rescore)
    failed = await client.patch(f"/api/v1/questions/{q.id}", headers=h, json={"weight": "low"})
    assert failed.status_code == 500
    assert (await question(seeded.service_id, "ia_ai_projects")).weight == "high"  # rolled back
    monkeypatch.undo()

    ok = await client.patch(f"/api/v1/questions/{q.id}", headers=h, json={"weight": "low"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["weight"] == "low" and ok.json()["version"] == q.version  # weight: no new version
    assert 0 < await current_intent(seeded.company_id, seeded.service_id) < before


async def test_delete_question_is_soft_and_rescores(org_id, client):
    seeded = await seed_lead(org_id)
    q = await question(seeded.service_id, "ia_ai_projects")
    assert await current_intent(seeded.company_id, seeded.service_id) > 0
    h = headers(org_id)

    res = await client.delete(f"/api/v1/questions/{q.id}", headers=h)
    assert res.status_code == 204
    assert (await question(seeded.service_id, "ia_ai_projects")).is_active is False
    assert (await signal(seeded.signal_ids[0])) is not None  # evidence kept
    assert await current_intent(seeded.company_id, seeded.service_id) == 0

    card = (await client.get(f"/api/v1/leads/{seeded.company_id}", headers=h)).json()
    assert card["signals_by_question"] == []
    assert "ia_ai_projects" not in {x["key"] for x in card["questions_without_evidence"]}
    leads = (
        await client.get("/api/v1/leads", params={"service_id": str(seeded.service_id)}, headers=h)
    ).json()
    assert leads["items"][0]["signals_count"] == 0
