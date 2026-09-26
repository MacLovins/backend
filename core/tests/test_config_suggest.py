"""POST /api/v1/services/{id}/questions/suggest — drafts from ai.suggest_questions, admin only, nothing saved."""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import leadradar_ai as ai
import pytest
from fastapi.testclient import TestClient
from leadradar_ai.testing import FakeLLM
from leadradar_auth.schemas import Principal
from leadradar_auth.security import create_access_token
from leadradar_core.db.session import async_session_factory, engine
from leadradar_core.main import create_app
from leadradar_core.modules.config.models import SignalQuestion
from leadradar_core.modules.config.presets import create_service_from_preset
from leadradar_core.modules.config.suggest_router import get_assist_llm
from leadradar_core.settings import settings
from sqlalchemy import func, select


@pytest.fixture(autouse=True)
async def fresh_engine() -> AsyncIterator[None]:
    await engine.dispose()
    yield
    await engine.dispose()


def answer(request):
    assert request.purpose == "suggest_questions" and request.pool == "cheap"
    question = {
        "label": "Automation hiring",
        "text": "Is the company hiring automation engineers?",
        "category": "hiring",
        "polarity": "positive",
        "weight": "high",
        "source_types": ["jobs"],
        "recency_days": 90,
        "keywords": ["automation engineer"],
    }
    return {
        "questions": [
            question | {"key": "sg_hiring"},
            question | {"key": "ia_cost"},  # exists in the preset: dropped
            question
            | {
                "key": "sg_inhouse",
                "polarity": "negative",
                "category": "internal_capability",
                "weight": "medium",
            },
        ],
        "rules": [
            {
                "name": "Too small",
                "kind": "firmographic",
                "field": "employees",
                "op": "lt",
                "value": "500",
                "action": "exclude",
            }
        ],
    }


def headers(org_id: UUID, role: str) -> dict[str, str]:
    token = create_access_token(Principal(user_id=uuid4(), org_id=org_id, email=f"{role}@x.io", role=role))
    return {"Authorization": f"Bearer {token}"}


async def seed(org_id: UUID) -> UUID:
    async with async_session_factory() as session, session.begin():
        service, _ = await create_service_from_preset(session, org_id, "intelligent_automation")
        return service.id


async def question_count(service_id: UUID) -> int:
    async with async_session_factory() as session:
        stmt = select(func.count()).select_from(SignalQuestion).where(SignalQuestion.service_id == service_id)
        return (await session.execute(stmt)).scalar_one()


async def test_suggest_questions_returns_unsaved_drafts():
    org_id = uuid4()
    service_id = await seed(org_id)
    before = await question_count(service_id)
    await engine.dispose()  # the TestClient runs its own event loop: no pooled connections may cross
    settings.ENV = "test"
    app = create_app()
    llm = FakeLLM(answer)
    app.dependency_overrides[get_assist_llm] = lambda: llm
    with TestClient(app) as client:
        url = f"/api/v1/services/{service_id}/questions/suggest"
        assert client.post(url, headers=headers(org_id, "sales")).status_code == 403
        assert client.post(url, headers=headers(uuid4(), "admin")).status_code == 404  # other org

        res = client.post(url, headers=headers(org_id, "admin"))
        assert res.status_code == 200, res.text
        body = res.json()

        app.dependency_overrides[get_assist_llm] = lambda: FakeLLM([ai.QuotaExhausted("cheap")])
        assert client.post(url, headers=headers(org_id, "admin")).status_code == 429

    await engine.dispose()
    assert [q["key"] for q in body["questions"]] == ["sg_hiring", "sg_inhouse"]
    assert body["questions"][0] | {"keywords": None} == {
        "key": "sg_hiring",
        "label": "Automation hiring",
        "text": "Is the company hiring automation engineers?",
        "category": "hiring",
        "polarity": "positive",
        "weight": "high",
        "source_types": ["jobs"],
        "recency_days": 90,
        "keywords": None,
        "job_titles": [],
        "negative_terms": [],
    }
    assert body["rules"] == [
        {
            "name": "Too small",
            "kind": "firmographic",
            "condition": {"field": "employees", "op": "lt", "value": 500},
            "action": "exclude",
            "cap_value": None,
        }
    ]
    assert body["prompt_version"] == "suggest_questions@v1"
    assert "Intelligent Automation" in llm.calls[0].user
    assert await question_count(service_id) == before  # drafts only
