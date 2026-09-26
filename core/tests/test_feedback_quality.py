"""Feedback on signals and leads, rescoring after votes, evidence memory across runs, GET /quality."""

import hashlib
import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from _lead_fixtures import create_run, headers, question, seed_lead, signal, verified_signal
from leadradar_core.adapters.store import SqlAnalysisStore
from leadradar_core.db.session import async_session_factory, engine
from leadradar_core.main import create_app
from leadradar_core.modules.feedback.models import Feedback
from leadradar_core.modules.intelligence.evidence import evidence_key
from leadradar_core.modules.intelligence.models import Document, Signal
from leadradar_core.modules.leads.models import LeadScore
from leadradar_core.settings import settings
from sqlalchemy import select, text, update


@pytest.fixture(autouse=True)
async def fresh_engine() -> AsyncIterator[None]:
    """Each async test runs in its own event loop; pooled asyncpg connections must not cross loops."""
    await engine.dispose()
    yield


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """In-process ASGI client on the test's own event loop (no cross-loop asyncpg connections)."""
    settings.ENV = "test"
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def org_id() -> UUID:
    return uuid4()


async def current_score(company_id: UUID, service_id: UUID) -> LeadScore:
    async with async_session_factory() as session:
        return (
            await session.execute(
                select(LeadScore).where(
                    LeadScore.company_id == company_id,
                    LeadScore.service_id == service_id,
                    LeadScore.is_current.is_(True),
                )
            )
        ).scalar_one()


# --- verdicts ----------------------------------------------------------------------------------


async def test_verdicts_are_validated_per_target_type(org_id, client):
    seeded = await seed_lead(org_id)
    h = headers(org_id, "sales", uuid4())
    sig_url = f"/api/v1/signals/{seeded.signal_ids[0]}/feedback"
    lead_url = f"/api/v1/leads/{seeded.company_id}/feedback"

    assert (await client.post(sig_url, headers=h, json={"verdict": "wrong"})).status_code == 422
    assert (await client.post(sig_url, headers=h, json={"verdict": "good_fit"})).status_code == 422
    wrong_lead = {"verdict": "correct", "service_id": str(seeded.service_id)}
    assert (await client.post(lead_url, headers=h, json=wrong_lead)).status_code == 422
    for verdict in ("correct", "irrelevant"):
        assert (await client.post(sig_url, headers=h, json={"verdict": verdict})).status_code == 201
    ok = await client.post(
        lead_url, headers=h, json={"verdict": "good_fit", "service_id": str(seeded.service_id)}
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["target_type"] == "lead" and ok.json()["verdict"] == "good_fit"
    # a signal of another org is not found; a wrong service id is rejected
    assert (
        await client.post(sig_url, headers=headers(uuid4()), json={"verdict": "correct"})
    ).status_code == 404
    bad_service = await client.post(
        sig_url, headers=h, json={"verdict": "correct", "service_id": str(uuid4())}
    )
    assert bad_service.status_code == 422


async def test_incorrect_rejects_the_signal_and_rescores_in_the_same_request(org_id, client):
    seeded = await seed_lead(org_id)
    before = await current_score(seeded.company_id, seeded.service_id)
    assert before.intent > 0

    res = await client.post(
        f"/api/v1/signals/{seeded.signal_ids[0]}/feedback",
        headers=headers(org_id, "sales", uuid4()),
        json={"verdict": "incorrect", "service_id": str(seeded.service_id), "reason": "other company"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["verdict"] == "incorrect"
    assert body["score"]["intent"] == 0 and isinstance(body["score"]["priority"], (int, float))

    assert (await signal(seeded.signal_ids[0])).status == "rejected_by_user"
    after = await current_score(seeded.company_id, seeded.service_id)
    assert float(after.intent) == 0 and after.id != before.id


async def test_repeated_vote_is_an_upsert_and_can_be_withdrawn(org_id, client):
    seeded = await seed_lead(org_id)
    user = uuid4()
    h = headers(org_id, "sales", user)
    url = f"/api/v1/signals/{seeded.signal_ids[0]}/feedback"

    first = await client.post(url, headers=h, json={"verdict": "correct"})
    second = await client.post(url, headers=h, json={"verdict": "incorrect"})
    assert first.status_code == 201 and second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["score"]["intent"] == 0

    async with async_session_factory() as session:
        verdicts = (
            (
                await session.execute(
                    select(Feedback.verdict).where(Feedback.target_id == seeded.signal_ids[0])
                )
            )
            .scalars()
            .all()
        )
    assert verdicts == ["incorrect"]

    withdrawn = await client.delete(url, headers=h)
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["withdrawn"] is True and withdrawn.json()["score"]["intent"] > 0
    assert (await signal(seeded.signal_ids[0])).status == "active"
    assert (await client.delete(url, headers=h)).json()["withdrawn"] is False


async def test_rejection_survives_reanalysis_by_evidence_key(org_id, client):
    seeded = await seed_lead(org_id)
    user = uuid4()
    old_id = seeded.signal_ids[0]
    res = await client.post(
        f"/api/v1/signals/{old_id}/feedback",
        headers=headers(org_id, "sales", user),
        json={"verdict": "incorrect"},
    )
    assert res.status_code == 201, res.text

    # re-analysis extracts the same evidence (whitespace/case differ) plus a new one
    q_ai = await question(seeded.service_id, "ia_ai_projects")
    q_cost = await question(seeded.service_id, "ia_cost")
    async with async_session_factory() as session:
        document = await session.get(Document, seeded.document_id)
    same = verified_signal(q_ai, document, "  ACME uses agentic   AI for RFQs ")
    fresh = verified_signal(q_cost, document, "Acme cuts costs by 20%")
    store = SqlAnalysisStore(async_session_factory, org_id)
    await store.save_extraction(
        await create_run(org_id), seeded.company_id, seeded.service_id, "fp-2", [same, fresh], []
    )

    async with async_session_factory() as session:
        rows = (
            (await session.execute(select(Signal).where(Signal.company_id == seeded.company_id)))
            .scalars()
            .all()
        )
    by_status = {(r.question_key, r.status) for r in rows}
    assert ("ia_ai_projects", "superseded") in by_status  # the old row: history
    assert ("ia_ai_projects", "rejected_by_user") in by_status  # the new copy stays rejected
    assert ("ia_cost", "active") in by_status
    assert not any(r.question_key == "ia_ai_projects" and r.status == "active" for r in rows)

    # the card shows the user's vote on the evidence; withdrawing it brings the current copy back
    card = (
        await client.get(
            f"/api/v1/leads/{seeded.company_id}",
            params={"service_id": str(seeded.service_id)},
            headers=headers(org_id, "sales", user),
        )
    ).json()
    shown = {s["quote"] for g in card["signals_by_question"] for s in g["signals"]}
    assert shown == {"Acme cuts costs by 20%"}

    withdrawn = await client.delete(
        f"/api/v1/signals/{old_id}/feedback", headers=headers(org_id, "sales", user)
    )
    assert withdrawn.json()["withdrawn"] is True
    async with async_session_factory() as session:
        statuses = {
            r.status
            for r in (
                await session.execute(
                    select(Signal).where(
                        Signal.company_id == seeded.company_id, Signal.question_key == "ia_ai_projects"
                    )
                )
            ).scalars()
        }
    assert statuses == {"superseded", "active"}


def test_evidence_key_normalisation():
    base = evidence_key("q", "Uses agentic AI", "https://x.com/a/")
    assert base == evidence_key("q", "  uses   AGENTIC\nai ", "https://X.com/a#frag")
    assert base != evidence_key("q2", "Uses agentic AI", "https://x.com/a/")
    assert base != evidence_key("q", "Uses agentic AI", "https://x.com/b")
    raw = "q\nuses agentic ai\nhttps://x.com/a"
    assert base == hashlib.sha256(raw.encode()).hexdigest()


async def test_migration_backfill_matches_python_evidence_key(org_id):
    path = Path(__file__).parents[1] / "migrations" / "versions" / "2026-09-26_signal_evidence_key.py"
    spec = importlib.util.spec_from_file_location("evidence_key_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    seeded = await seed_lead(org_id, signals=(("ia_ai_projects", "  Mixed\tCase  Quote "),))
    sig_id = seeded.signal_ids[0]
    expected = (await signal(sig_id)).evidence_key
    async with async_session_factory() as session, session.begin():
        await session.execute(update(Signal).where(Signal.id == sig_id).values(evidence_key=None))
        await session.execute(text(migration.BACKFILL_SQL))
    assert (await signal(sig_id)).evidence_key == expected


# --- quality -----------------------------------------------------------------------------------


async def test_quality_by_category_and_source_from_signal_votes_only(org_id, client):
    seeded = await seed_lead(
        org_id,
        signals=(("ia_ai_projects", "Acme uses agentic AI"), ("ia_hiring", "Acme hires RPA developers")),
    )
    h = headers(org_id, "sales", uuid4())
    ai_sig, hiring_sig = seeded.signal_ids
    assert (
        await client.post(f"/api/v1/signals/{ai_sig}/feedback", headers=h, json={"verdict": "correct"})
    ).is_success
    res = await client.post(
        f"/api/v1/signals/{hiring_sig}/feedback", headers=h, json={"verdict": "incorrect"}
    )
    assert res.is_success
    # a lead vote must not count in signal precision
    res = await client.post(
        f"/api/v1/leads/{seeded.company_id}/feedback",
        headers=h,
        json={"verdict": "bad_fit", "service_id": str(seeded.service_id)},
    )
    assert res.is_success

    q = await client.get("/api/v1/quality", params={"service_id": str(seeded.service_id)}, headers=h)
    assert q.status_code == 200, q.text
    body = q.json()
    assert body["labeled"] == 2 and body["precision"] == 0.5
    assert {(c["category"], c["labeled"], c["precision"]) for c in body["by_category"]} == {
        ("ai_automation", 1, 1.0),
        ("hiring", 1, 0.0),
    }
    assert body["by_source"] == [{"source_type": "news", "labeled": 2, "precision": 0.5}]
    assert body["leads"] == {"labeled": 1, "good_fit": 0, "bad_fit": 1}
