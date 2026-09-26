"""Core ↔ leadradar-ai integration against the real database: ports, the analyze_company task, rescoring.

LLM and embeddings are fakes; the parser's network calls are replaced, so ParserCollector itself is exercised.
Every test works in its own org id, so it does not interfere with other tests on the shared dev database.
"""

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import leadradar_ai as ai
import leadradar_parser as parser
import pytest
from fastapi.testclient import TestClient
from leadradar_ai.testing import FakeEmbedder, FakeLLM
from leadradar_auth.schemas import Principal
from leadradar_auth.security import create_access_token
from leadradar_core.adapters.store import SqlAnalysisStore
from leadradar_core.db.session import async_session_factory, engine
from leadradar_core.main import create_app
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.config.models import SignalQuestion
from leadradar_core.modules.config.presets import create_service_from_preset
from leadradar_core.modules.intelligence.models import Document, Signal
from leadradar_core.modules.intelligence.service import load_bundle
from leadradar_core.modules.leads.models import LeadScore
from leadradar_core.modules.runs.models import AnalysisRun, RunEvent
from leadradar_core.settings import settings
from leadradar_core.worker import tasks
from leadradar_core.worker.deps import worker_context
from sqlalchemy import select

NOW = datetime.now(UTC)
SNIPPET = re.compile(r'<snippet id="(S\d+)"[^>]*>(.*?)</snippet>', re.S)
QUESTION = re.compile(r'<question id="(Q\d+)"[^>]*>(.*?)</question>', re.S)


@pytest.fixture(autouse=True)
async def fresh_engine() -> AsyncIterator[None]:
    """Each async test runs in its own event loop; pooled asyncpg connections must not cross loops."""
    await engine.dispose()
    yield
    await engine.dispose()


def smart_llm(request):
    """Answers "yes" with a verbatim quote when a snippet mentions agentic AI; everything else unclear."""
    user = request.user.split("</examples>")[-1]
    snippets = SNIPPET.findall(user)
    answers = []
    for qid, text in QUESTION.findall(user):
        hit = next(((sid, body) for sid, body in snippets if "agentic AI" in body), None)
        if hit and "AI, RPA" in text:
            start = hit[1].index("agentic AI")
            quote = hit[1][max(0, start - 20) : start + 40]
            evidence = [
                {
                    "snippet_id": hit[0],
                    "quote": quote,
                    "subject": "target_company",
                    "event_date": None,
                    "strength": "strong",
                    "summary": "Runs agentic AI in operations.",
                }
            ]
            answers.append(
                {
                    "question_id": qid,
                    "answer": "yes",
                    "confidence": 0.9,
                    "rationale": "r",
                    "evidence": evidence,
                }
            )
        else:
            answers.append(
                {"question_id": qid, "answer": "unclear", "confidence": 0.3, "rationale": "r", "evidence": []}
            )
    return {"answers": answers}


def parser_document(title: str, text: str) -> parser.Document:
    url = f"https://news.example.com/{uuid4().hex[:8]}"
    return parser.Document(
        source_type="news",
        source_name="gdelt",
        url=url,
        canonical_url=url,
        title=title,
        text=text,
        published_at=NOW - timedelta(days=5),
        fetched_at=NOW,
        language="en",
        content_hash=uuid4().hex,
        meta={},
    )


@pytest.fixture
def org_id() -> UUID:
    return uuid4()


@pytest.fixture
def fake_worker(monkeypatch) -> FakeLLM:
    llm = FakeLLM(smart_llm)
    monkeypatch.setattr(worker_context, "started", True)
    monkeypatch.setattr(worker_context, "llm", llm)
    monkeypatch.setattr(worker_context, "embedder", FakeEmbedder(dim=384))
    monkeypatch.setattr(worker_context, "redis", None)
    monkeypatch.setattr(worker_context, "checkpointer", None)
    monkeypatch.setattr(settings, "ANALYSIS_RESOLVE_TIMEOUT_S", 5)
    return llm


@pytest.fixture
def fake_parser(monkeypatch) -> list[parser.Document]:
    documents = [
        parser_document(
            "Acme Logistics scales agentic AI",
            "Acme Logistics now uses agentic AI to process customer RFQs across its European hubs.",
        ),
        parser_document("Acme Logistics dividend", "Acme Logistics raises its dividend after a strong year."),
    ]

    async def resolve_company(ref, *, http=None):
        return parser.ResolvedCompany(
            **ref.model_dump(),
            homepage_url=f"https://{ref.domain}",
            own_domains=[ref.domain],
            resolved_at=NOW,
        )

    async def collect(company, plan, *, http=None):
        return parser.CollectResult(
            documents=documents, errors=[], stats={"gdelt": len(documents)}, duration_ms=1
        )

    monkeypatch.setattr(parser, "resolve_company", resolve_company)
    monkeypatch.setattr(parser, "collect", collect)
    return documents


async def seed(org_id: UUID) -> tuple[UUID, UUID]:
    async with async_session_factory() as session, session.begin():
        service, _ = await create_service_from_preset(session, org_id, "intelligent_automation")
        company = Company(
            org_id=org_id,
            name="Acme Logistics",
            domain=f"acme-{uuid4().hex[:6]}.example",
            country_code="DE",
            industry_ids=["logistics"],
            employees=20000,
        )
        session.add(company)
        await session.flush()
        return service.id, company.id


async def create_run(org_id: UUID, company_ids: list[UUID], service_ids: list[UUID]) -> UUID:
    async with async_session_factory() as session, session.begin():
        run = AnalysisRun(
            org_id=org_id,
            kind="analyze",
            status="queued",
            params={
                "company_ids": [str(c) for c in company_ids],
                "service_ids": [str(s) for s in service_ids],
            },
            progress={"done": 0, "total": len(company_ids), "failed": 0, "paused": 0},
        )
        session.add(run)
        await session.flush()
        return run.id


# --- bundle and presets ------------------------------------------------------------------------


async def test_service_from_preset_maps_to_the_ai_bundle(org_id):
    service_id, _ = await seed(org_id)
    async with async_session_factory() as session:
        from leadradar_core.modules.config.models import Service

        bundle = await load_bundle(session, await session.get(Service, service_id))
    preset = ai.load_preset("intelligent_automation")
    assert [q.key for q in bundle.questions] == [q.key for q in preset.questions]
    assert bundle.icp.nice_to_have == preset.icp.nice_to_have
    assert [r.name for r in bundle.rules] == [r.name for r in preset.rules]
    assert bundle.scoring.tiers == {"hot": 65, "warm": 40}
    assert bundle.questions[0].keywords == preset.questions[0].keywords_seed


# --- the analyze_company task ------------------------------------------------------------------


async def test_analyze_company_end_to_end(org_id, fake_worker, fake_parser):
    service_id, company_id = await seed(org_id)
    run_id = await create_run(org_id, [company_id], [service_id])

    assert await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)]) == "done"
    assert len(fake_worker.calls) == 1

    async with async_session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert (run.status, run.progress["done"], run.finished_at is not None) == ("succeeded", 1, True)
        docs = (
            (await session.execute(select(Document).where(Document.company_id == company_id))).scalars().all()
        )
        assert len(docs) == 2
        signals = (
            (await session.execute(select(Signal).where(Signal.company_id == company_id))).scalars().all()
        )
        assert [s.question_key for s in signals] == ["ia_ai_projects"]
        assert "agentic AI" in signals[0].quote and signals[0].quote_start is not None
        score = (
            await session.execute(
                select(LeadScore).where(LeadScore.company_id == company_id, LeadScore.is_current.is_(True))
            )
        ).scalar_one()
        assert score.intent > 0 and score.why_now[0]["text"] == "Runs agentic AI in operations."
        events = (
            await session.execute(
                select(RunEvent.stage, RunEvent.status).where(RunEvent.run_id == run_id).order_by(RunEvent.id)
            )
        ).all()
        stages = [(e.stage, e.status) for e in events]
        assert ("collecting", "done") in stages and ("scoring", "done") in stages
        assert stages[-1] == ("run", "succeeded") and ("company", "done") in stages
        company = await session.get(Company, company_id)
        assert company.last_analyzed_at is not None and company.resolved_at is not None

    # a second analysis with nothing new skips the LLM (fingerprint) and keeps the signals
    run2 = await create_run(org_id, [company_id], [service_id])
    assert await tasks.analyze_company(str(run2), str(company_id), [str(service_id)]) == "done"
    assert len(fake_worker.calls) == 1


async def test_quota_pauses_the_company_and_the_run_ends_partial(
    org_id, fake_worker, fake_parser, monkeypatch
):
    service_id, company_id = await seed(org_id)
    run_id = await create_run(org_id, [company_id], [service_id])
    monkeypatch.setattr(worker_context, "llm", FakeLLM([ai.QuotaExhausted("main")]))
    assert await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)]) == "paused"
    async with async_session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert (run.status, run.progress["paused"]) == ("partial", 1)


async def test_cancelled_run_is_skipped(org_id, fake_worker, fake_parser):
    service_id, company_id = await seed(org_id)
    run_id = await create_run(org_id, [company_id], [service_id])
    async with async_session_factory() as session, session.begin():
        (await session.get(AnalysisRun, run_id)).status = "cancelled"
    assert await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)]) == "skipped"
    assert fake_worker.calls == []


# --- store adapter -----------------------------------------------------------------------------


async def test_store_supersedes_signals_and_keeps_score_history(org_id, fake_worker, fake_parser):
    service_id, company_id = await seed(org_id)
    run_id = await create_run(org_id, [company_id], [service_id])
    await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)])
    store = SqlAnalysisStore(async_session_factory, org_id)

    first = await store.load_signals(company_id, service_id)
    await store.save_extraction(run_id, company_id, service_id, "fp-new", [], [])
    assert await store.load_signals(company_id, service_id) == []
    assert await store.get_fingerprint(company_id, service_id) == "fp-new"
    async with async_session_factory() as session:
        superseded = (
            (await session.execute(select(Signal.status).where(Signal.company_id == company_id)))
            .scalars()
            .all()
        )
    assert set(superseded) == {"superseded"} and len(superseded) == len(first)

    async with async_session_factory() as session:
        from leadradar_core.modules.config.models import Service

        bundle = await load_bundle(session, await session.get(Service, service_id))
        company = await session.get(Company, company_id)
    from leadradar_core.adapters import mapping

    score = ai.score_company(mapping.company_profile(company), bundle, [], NOW)
    change = await store.save_score(run_id, score)
    assert change.tier_before is not None and change.tier_after == score.tier
    async with async_session_factory() as session:
        rows = (
            await session.execute(select(LeadScore.is_current).where(LeadScore.company_id == company_id))
        ).all()
    assert sorted(r.is_current for r in rows) == [False, True]


# --- API: rescore, rules, presets, retry, expand -----------------------------------------------


@pytest.fixture
def client() -> AsyncIterator[TestClient]:
    settings.ENV = "test"
    with TestClient(create_app()) as test_client:
        yield test_client


def headers(org_id: UUID) -> dict[str, str]:
    token = create_access_token(Principal(user_id=org_id, org_id=org_id, email="a@x.io", role="admin"))
    return {"Authorization": f"Bearer {token}"}


async def test_scoring_profile_change_rescores_without_llm(org_id, fake_worker, fake_parser, client):
    service_id, company_id = await seed(org_id)
    run_id = await create_run(org_id, [company_id], [service_id])
    await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)])
    await engine.dispose()

    res = client.put(
        f"/api/v1/services/{service_id}/scoring-profile",
        headers=headers(org_id),
        json={"params": {"tiers": {"hot": 99, "warm": 98}}},
    )  # hot → cold
    assert res.status_code == 200, res.text
    assert res.json()["rescored"] == 1 and res.json()["tier_changes"] == 1
    assert len(fake_worker.calls) == 1  # no new LLM calls

    bad = client.put(
        f"/api/v1/services/{service_id}/scoring-profile",
        headers=headers(org_id),
        json={"params": {"weights": {"fit": 0.35}, "thresholds": {}}},
    )
    assert bad.status_code == 422


async def test_invalid_rule_is_rejected_and_valid_rule_rescores(org_id, client):
    service_id, _ = await seed(org_id)
    await engine.dispose()
    bad = client.post(
        f"/api/v1/services/{service_id}/rules",
        headers=headers(org_id),
        json={
            "name": "x",
            "kind": "firmographic",
            "condition": {"field": "employees", "op": "lt"},
            "action": "exclude",
        },
    )
    assert bad.status_code == 422
    ok = client.post(
        f"/api/v1/services/{service_id}/rules",
        headers=headers(org_id),
        json={
            "name": "small",
            "kind": "firmographic",
            "condition": {"field": "employees", "op": "lt", "value": 100},
            "action": "exclude",
        },
    )
    assert ok.status_code == 201, ok.text


def test_apply_preset_creates_the_full_service(client):
    org = uuid4()
    res = client.post("/api/v1/presets/cybersecurity/apply", headers=headers(org))
    assert res.status_code == 200, res.text
    questions = client.get(f"/api/v1/services/{res.json()['id']}/questions", headers=headers(org)).json()
    assert len(questions) == 9 and {q["key"] for q in questions} >= {"cy_incident", "cy_mssp"}
    assert client.post("/api/v1/presets/astrology/apply", headers=headers(org)).status_code == 404


async def test_retry_failed_reopens_failed_companies(org_id, fake_worker, fake_parser, client, monkeypatch):
    service_id, company_id = await seed(org_id)
    run_id = await create_run(org_id, [company_id], [service_id])
    monkeypatch.setattr(worker_context, "llm", FakeLLM([ai.QuotaExhausted("main")]))
    await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)])
    await engine.dispose()
    res = client.post(f"/api/v1/runs/{run_id}/retry-failed", headers=headers(org_id))
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "queued" and res.json()["progress"]["paused"] == 0


async def test_expand_question_task_stores_keywords(org_id, fake_worker):
    service_id, _ = await seed(org_id)
    async with async_session_factory() as session:
        q = (
            await session.execute(
                select(SignalQuestion).where(
                    SignalQuestion.service_id == service_id, SignalQuestion.key == "ia_hiring"
                )
            )
        ).scalar_one()
    expansion = {
        "keywords": [{"language": "en", "terms": ["robotic process automation"]}],
        "job_titles": ["RPA Engineer"],
        "negative_terms": [],
    }
    worker_context.llm = FakeLLM([expansion])
    assert await tasks.expand_question(str(q.id)) == "ready"
    async with async_session_factory() as session:
        row = await session.get(SignalQuestion, q.id)
    assert row.keywords_status == "ready"
    assert "robotic process automation" in row.keywords["en"] and "RPA Engineer" in row.job_titles


async def test_sse_replay_sends_json_objects(org_id, fake_worker, fake_parser, client):
    service_id, company_id = await seed(org_id)
    run_id = await create_run(org_id, [company_id], [service_id])
    await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)])
    await engine.dispose()
    body = client.get(f"/api/v1/runs/{run_id}/events", headers=headers(org_id)).text
    import json

    data = [json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")]
    assert data and all(isinstance(d, dict) for d in data)  # not a JSON string inside JSON
    assert any(d.get("stage") == "scoring" for d in data)
    assert data[-1]["status"] == "succeeded"
