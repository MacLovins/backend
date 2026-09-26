"""Outbox (CO-17): events emitted where facts happen, the dispatcher, the scheduler tasks (CO-22).

Runs against the real database like test_ai_integration (its seed helpers and fakes are reused); every test
works in its own org id. The dispatcher sees events of other tests too, so assertions look at our org only.
"""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import leadradar_ai as ai
import pytest
from leadradar_ai.testing import FakeLLM
from leadradar_core.adapters.store import SqlAnalysisStore
from leadradar_core.db.session import async_session_factory, engine
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.activity import events
from leadradar_core.modules.activity.dispatcher import Event, dispatch_pending
from leadradar_core.modules.activity.models import DomainEvent
from leadradar_core.modules.intelligence.models import Signal
from leadradar_core.modules.runs.models import AnalysisRun, RunEvent
from leadradar_core.settings import settings
from leadradar_core.worker import broker as broker_module
from leadradar_core.worker import scheduled, tasks
from leadradar_core.worker.deps import worker_context
from sqlalchemy import select, update
from test_ai_integration import (  # shared seed helpers and fakes
    client,
    create_run,
    fake_parser,
    fake_worker,
    fresh_engine,
    headers,
    org_id,
    seed,
)

__all__ = ["client", "fake_parser", "fake_worker", "fresh_engine", "org_id"]  # fixtures used by name


async def org_events(org: UUID, event_type: str | None = None) -> list[DomainEvent]:
    stmt = select(DomainEvent).where(DomainEvent.org_id == org).order_by(DomainEvent.created_at)
    if event_type:
        stmt = stmt.where(DomainEvent.type == event_type)
    async with async_session_factory() as session:
        return list((await session.execute(stmt)).scalars().all())


# --- emission ----------------------------------------------------------------------------------


async def test_outbox_analysis_emits_signal_tier_and_run_events(org_id, fake_worker, fake_parser):
    service_id, company_id = await seed(org_id)
    run_id = await create_run(org_id, [company_id], [service_id])
    assert await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)]) == "done"

    detected = await org_events(org_id, events.SIGNAL_DETECTED)
    assert len(detected) == 1
    p = detected[0].payload
    assert p["company_id"] == str(company_id) and p["question_key"] == "ia_ai_projects"
    assert "agentic AI" in p["quote"] and p["company_name"] == "Acme Logistics" and p["weight"]
    async with async_session_factory() as session:
        signal_ids = (
            await session.execute(select(Signal.id).where(Signal.company_id == company_id))
        ).scalars()
        assert p["signal_id"] in {str(s) for s in signal_ids}

    tier = await org_events(org_id, events.LEAD_TIER_CHANGED)
    assert len(tier) == 1 and tier[0].payload["tier_before"] is None
    assert tier[0].payload["tier_after"] in ("hot", "warm", "cold", "disqualified")
    assert tier[0].payload["why_now"][0]["text"] == "Runs agentic AI in operations."

    finished = await org_events(org_id, events.RUN_FINISHED)
    assert [(e.payload["run_id"], e.payload["status"]) for e in finished] == [(str(run_id), "succeeded")]

    # nothing new: no signal.detected, same tier → no lead.tier_changed; the run still finishes
    run2 = await create_run(org_id, [company_id], [service_id])
    await tasks.analyze_company(str(run2), str(company_id), [str(service_id)])
    assert len(await org_events(org_id, events.SIGNAL_DETECTED)) == 1
    assert len(await org_events(org_id, events.LEAD_TIER_CHANGED)) == 1
    assert len(await org_events(org_id, events.RUN_FINISHED)) == 2


async def test_outbox_reextracted_known_signal_is_not_new(org_id, fake_worker, fake_parser):
    service_id, company_id = await seed(org_id)
    run_id = await create_run(org_id, [company_id], [service_id])
    await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)])
    store = SqlAnalysisStore(async_session_factory, org_id)
    known = await store.load_signals(company_id, service_id)
    async with async_session_factory() as session:
        row = (await session.execute(select(Signal).where(Signal.company_id == company_id))).scalars().first()
    verified = ai.VerifiedSignal(
        question_id=row.question_id,
        question_key=row.question_key,
        question_version=row.question_version,
        category=row.category,
        polarity=row.polarity,
        document_id=row.document_id,
        chunk_id=row.chunk_id,
        url=row.url,
        source_type=row.source_type,
        source_name=row.source_name,
        quote="  " + row.quote.upper() + " ",  # same evidence, different whitespace and case
        quote_start=row.quote_start,
        quote_end=row.quote_end,
        summary=row.summary,
        strength=row.strength,
        confidence=0.9,
        reliability=0.9,
        event_date=None,
        published_at=row.published_at,
        flags=set(),
        model="fake",
        prompt_version="x",
    )
    new = verified.model_copy(update={"quote": "Acme Logistics opens an AI lab in Hamburg."})
    await store.save_extraction(run_id, company_id, service_id, "fp2", [verified, new], [])
    detected = await org_events(org_id, events.SIGNAL_DETECTED)
    assert len(known) == 1 and [e.payload["quote"] for e in detected[1:]] == [new.quote]


async def test_outbox_rescore_tier_change_and_feedback(org_id, fake_worker, fake_parser, client):
    service_id, company_id = await seed(org_id)
    run_id = await create_run(org_id, [company_id], [service_id])
    await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)])
    await engine.dispose()

    res = client.put(
        f"/api/v1/services/{service_id}/scoring-profile",
        headers=headers(org_id),
        json={"params": {"tiers": {"hot": 99, "warm": 98}}},
    )
    assert res.status_code == 200 and res.json()["tier_changes"] == 1
    fb = client.post(
        f"/api/v1/leads/{company_id}/feedback",
        headers=headers(org_id),
        json={"verdict": "correct", "service_id": str(service_id), "reason": "met them"},
    )
    assert fb.status_code == 201, fb.text

    # the API loop owns the pooled connections now: read the outcome through the activity feed
    feed = client.get("/api/v1/activity", headers=headers(org_id)).json()
    assert feed[0]["type"] == events.FEEDBACK_CREATED
    assert (feed[0]["payload"]["feedback_id"], feed[0]["payload"]["target_type"]) == (fb.json()["id"], "lead")
    assert {e["type"] for e in feed} >= {
        events.SIGNAL_DETECTED,
        events.LEAD_TIER_CHANGED,
        events.RUN_FINISHED,
    }
    tier = [e["payload"] for e in feed if e["type"] == events.LEAD_TIER_CHANGED]
    assert tier[0]["tier_after"] == "cold" and tier[0]["tier_before"] not in (None, "cold")  # newest first


# --- dispatcher --------------------------------------------------------------------------------


class Recorder:
    def __init__(self, name: str, org: UUID, fail_times: int = 0, delay: float = 0) -> None:
        self.name = name
        self.event_types = frozenset({"test.ping", "test.other"})
        self.org = org
        self.fail_times = fail_times
        self.delay = delay
        self.seen: list[Event] = []

    async def handle(self, event: Event) -> None:
        if event.org_id != self.org:
            return
        await asyncio.sleep(self.delay)
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("boom")
        self.seen.append(event)


async def emit(org: UUID, n: int, event_type: str = "test.ping") -> list[UUID]:
    async with async_session_factory() as session, session.begin():
        rows = [events.emit_event(session, org, event_type, {"n": i}) for i in range(n)]
    return [r.id for r in rows]


async def test_outbox_dispatcher_fans_out_and_marks_processed(org_id):
    ids = await emit(org_id, 3)
    other = await emit(org_id, 1, "test.unrouted")
    a, b = Recorder("a", org_id), Recorder("b", org_id)
    await dispatch_pending(async_session_factory, [a, b], batch_size=1000)
    assert [e.payload["n"] for e in a.seen] == [0, 1, 2] and len(b.seen) == 3
    rows = {e.id: e for e in await org_events(org_id)}
    assert all(rows[i].processed_at is not None and rows[i].delivered_to == ["a", "b"] for i in ids)
    assert rows[other[0]].processed_at is not None and rows[other[0]].delivered_to == []

    await dispatch_pending(async_session_factory, [a, b], batch_size=1000)  # idempotent: nothing left
    assert len(a.seen) == 3 and len(b.seen) == 3


async def test_outbox_failing_consumer_is_retried_then_given_up(org_id):
    [event_id] = await emit(org_id, 1)
    ok, flaky = Recorder("ok", org_id), Recorder("flaky", org_id, fail_times=1)
    await dispatch_pending(async_session_factory, [ok, flaky], batch_size=1000)
    [row] = await org_events(org_id)
    assert row.processed_at is None and row.attempts == 1 and row.delivered_to == ["ok"]
    assert "flaky: RuntimeError: boom" in row.last_error

    await dispatch_pending(async_session_factory, [ok, flaky], batch_size=1000)
    [row] = await org_events(org_id)
    assert row.processed_at is not None and row.delivered_to == ["flaky", "ok"]
    assert len(ok.seen) == 1 and len(flaky.seen) == 1  # the healthy consumer is not called twice

    await emit(org_id, 1)
    broken = Recorder("broken", org_id, fail_times=10)
    for _ in range(2):
        await dispatch_pending(async_session_factory, [broken], batch_size=1000, max_attempts=2)
    last = (await org_events(org_id))[-1]
    assert last.id != event_id and last.processed_at is not None and last.attempts == 2


async def test_outbox_concurrent_dispatchers_deliver_each_event_once(org_id):
    ids = await emit(org_id, 20)
    recorder = Recorder("slow", org_id, delay=0.01)
    await asyncio.gather(
        *(dispatch_pending(async_session_factory, [recorder], batch_size=5) for _ in range(6))
    )
    counts = Counter(e.id for e in recorder.seen)
    assert set(counts) <= set(ids) and all(c == 1 for c in counts.values())
    while any(r.processed_at is None for r in await org_events(org_id)):
        await dispatch_pending(async_session_factory, [recorder], batch_size=1000)
    assert sorted(Counter(e.id for e in recorder.seen).items()) == sorted((i, 1) for i in ids)


async def test_outbox_dispatch_task_uses_the_registry(org_id, monkeypatch):
    await emit(org_id, 2)
    recorder = Recorder("registry", org_id)
    monkeypatch.setattr(scheduled, "_consumers", [recorder])
    monkeypatch.setattr(settings, "EVENTS_DISPATCH_BATCH", 1)
    assert await scheduled.dispatch_events() >= 2
    assert len(recorder.seen) == 2


# --- scheduler ---------------------------------------------------------------------------------


@pytest.fixture
def enqueued(monkeypatch) -> list[tuple[UUID, list[UUID], list[UUID]]]:
    calls = []

    async def fake_enqueue(run_id, company_ids, service_ids):
        calls.append((run_id, company_ids, service_ids))

    monkeypatch.setattr(scheduled, "enqueue", fake_enqueue)
    return calls


async def test_scheduler_labels_register_the_periodic_tasks():
    source = broker_module.scheduler.sources[0]
    await source.startup()
    crons = {s.task_name: s.cron for s in await source.get_schedules()}
    assert crons == {
        "refresh_tracked": settings.REFRESH_CRON,
        "resume_paused": settings.RESUME_PAUSED_CRON,
        "dispatch_events": settings.DISPATCH_EVENTS_CRON,
    }


async def test_scheduler_refresh_tracked_creates_one_refresh_run(org_id, enqueued):
    service_id, due = await seed(org_id)
    now = datetime.now(UTC)
    async with async_session_factory() as session, session.begin():
        (await session.get(Company, due)).last_analyzed_at = now - timedelta(hours=7)
        fresh = Company(org_id=org_id, name="Fresh", domain=f"f-{uuid4().hex[:6]}.io", last_analyzed_at=now)
        never = Company(org_id=org_id, name="Never", domain=f"n-{uuid4().hex[:6]}.io")
        untracked = Company(
            org_id=org_id,
            name="Off",
            domain=f"o-{uuid4().hex[:6]}.io",
            is_tracked=False,
            last_analyzed_at=now - timedelta(days=2),
        )
        session.add_all([fresh, never, untracked])

    [run_id] = await scheduled.refresh_tracked_companies(only_org=org_id)
    assert enqueued == [(run_id, [due], [service_id])]
    async with async_session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
    assert (run.kind, run.status, run.progress["total"]) == ("refresh", "pending", 1)

    # the refresh run is still active: the next tick does not stack another one
    assert await scheduled.refresh_tracked_companies(only_org=org_id) == []


async def test_scheduler_resume_paused_continues_from_checkpoint(org_id, fake_worker, fake_parser, enqueued):
    service_id, company_id = await seed(org_id)
    run_id = await create_run(org_id, [company_id], [service_id])
    worker_context.llm = FakeLLM([ai.QuotaExhausted("main")])
    assert await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)]) == "paused"

    assert await scheduled.resume_paused_companies(only_org=org_id) == {run_id: [company_id]}
    assert enqueued == [(run_id, [company_id], [service_id])]
    async with async_session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        last = (
            (
                await session.execute(
                    select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.id.desc())
                )
            )
            .scalars()
            .first()
        )
    assert (run.status, run.progress["paused"], run.finished_at) == ("running", 0, None)
    assert (last.stage, last.status) == ("company", "resuming")
    # already resuming: a second tick does nothing
    assert await scheduled.resume_paused_companies(only_org=org_id) == {}

    # the resumed task finishes the run from the same thread
    worker_context.llm = fake_worker
    assert await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)]) == "done"
    async with async_session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
    assert run.status == "succeeded"
    statuses = [e.payload["status"] for e in await org_events(org_id, events.RUN_FINISHED)]
    assert statuses == ["partial", "succeeded"]


async def test_scheduler_resume_skips_cancelled_runs(org_id, fake_worker, fake_parser, enqueued):
    service_id, company_id = await seed(org_id)
    run_id = await create_run(org_id, [company_id], [service_id])
    worker_context.llm = FakeLLM([ai.QuotaExhausted("main")])
    await tasks.analyze_company(str(run_id), str(company_id), [str(service_id)])
    async with async_session_factory() as session, session.begin():
        await session.execute(update(AnalysisRun).where(AnalysisRun.id == run_id).values(status="cancelled"))
    assert await scheduled.resume_paused_companies(only_org=org_id) == {}
    assert enqueued == []
