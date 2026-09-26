"""Worker tasks (SPEC core §1.4.2): analyze_company runs the leadradar-ai graph, expand_question generates
multilingual keywords. Run progress is counted atomically in analysis_run.progress; the last company of a
run sets its final status and emits run.finished.

Every analyze_company that finds its run reports exactly one company outcome (done / failed / paused), so the
run always finishes — also when the company is missing or the service configuration cannot be loaded.
Cancellation: the run status is checked at every stage event of the graph and every CANCEL_POLL_S seconds;
a cancelled run stops the graph and the company is reported as "cancelled" (not counted: the run is closed).
"""

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime
from uuid import UUID

import leadradar_ai as ai
from sqlalchemy import select, text
from structlog import get_logger

from leadradar_core.adapters import mapping
from leadradar_core.db.session import async_session_factory
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.activity import events as domain_events
from leadradar_core.modules.config.models import Service, SignalQuestion
from leadradar_core.modules.intelligence.service import load_bundle, load_bundles
from leadradar_core.modules.runs import events
from leadradar_core.modules.runs.models import RUN_TERMINAL_STATUSES, AnalysisRun
from leadradar_core.worker.broker import broker
from leadradar_core.worker.deps import worker_context

log = get_logger(__name__)

TERMINAL = RUN_TERMINAL_STATUSES
CANCEL_POLL_S = 5.0


class RunCancelled(Exception):
    pass


async def _event(
    run: AnalysisRun,
    company_id: UUID | None,
    stage: str,
    status: str,
    message: str,
    data: dict,
) -> None:
    """run_event row + live publish; the SSE event name follows from (stage, status) (runs/events.py)."""
    async with async_session_factory() as session, session.begin():
        row = await events.add_event(
            session,
            org_id=run.org_id,
            run_id=run.id,
            company_id=company_id,
            stage=stage,
            status=status,
            message=message,
            payload=data,
        )
    await events.publish(worker_context.redis, row)


async def _run_status(run_id: UUID) -> str | None:
    async with async_session_factory() as session:
        return (
            await session.execute(select(AnalysisRun.status).where(AnalysisRun.id == run_id))
        ).scalar_one_or_none()


class CancellableProgress:
    """ProgressSink wrapper: between stages (every graph event) checks whether the run was cancelled.
    A cancelled run's events are dropped and the watcher in `_until_cancelled` stops the graph."""

    def __init__(self, inner: ai.ProgressSink, run_id: UUID, cancelled: asyncio.Event) -> None:
        self._inner = inner
        self._run_id = run_id
        self._cancelled = cancelled

    async def emit(self, event: ai.ProgressEvent) -> None:
        if self._cancelled.is_set() or await _run_status(self._run_id) == "cancelled":
            self._cancelled.set()
            return
        await self._inner.emit(event)


async def _until_cancelled[T](run_id: UUID, work: Awaitable[T], cancelled: asyncio.Event) -> T:
    """Await `work`; cancel it and raise RunCancelled as soon as the run is cancelled."""
    task = asyncio.ensure_future(work)
    signal = asyncio.ensure_future(cancelled.wait())
    try:
        while True:
            await asyncio.wait({task, signal}, timeout=CANCEL_POLL_S, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                return task.result()
            if cancelled.is_set() or await _run_status(run_id) == "cancelled":
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise RunCancelled
    finally:
        signal.cancel()
        if not task.done():  # the worker task itself was cancelled (shutdown)
            task.cancel()


async def _count(run_id: UUID, outcome: str, error: str | None = None) -> dict:
    """Atomically increment progress[outcome] (and keep the first error); returns progress and status."""
    async with async_session_factory() as session, session.begin():
        row = (
            await session.execute(
                text(
                    "UPDATE core.analysis_run SET progress = jsonb_set(progress, ARRAY[:key], "
                    "to_jsonb(COALESCE((progress->>:key)::int, 0) + 1)), "
                    "error = COALESCE(error, :error), updated_at = now() "
                    "WHERE id = :id RETURNING progress, status"
                ),
                {"key": outcome, "id": run_id, "error": error},
            )
        ).one()
    return {"progress": row.progress, "status": row.status}


async def _finish_if_complete(run: AnalysisRun, progress: dict) -> None:
    finished = progress.get("done", 0) + progress.get("failed", 0) + progress.get("paused", 0)
    if finished < progress.get("total", 0):
        return
    if progress.get("done", 0) == progress.get("total", 0):
        status = "succeeded"
    elif progress.get("done", 0) == 0 and progress.get("paused", 0) == 0:
        status = "failed"
    else:
        status = "partial"
    async with async_session_factory() as session, session.begin():
        updated = (
            await session.execute(
                text(
                    "UPDATE core.analysis_run SET status = :status, finished_at = now(), updated_at = now() "
                    "WHERE id = :id AND status NOT IN ('succeeded', 'partial', 'failed', 'cancelled') RETURNING id"
                ),
                {"status": status, "id": run.id},
            )
        ).first()
        if updated:
            domain_events.run_finished(session, run.org_id, run.id, status, progress)
    if updated:  # only the task that closes the run announces it
        await _event(run, None, "run", status, f"Run {status}", {"status": status, **progress})


async def _analyze(
    run: AnalysisRun, company_id: UUID, service_ids: list[str], mode: str
) -> ai.AnalysisOutput:
    """Everything that can fail for a company, including loading its configuration."""
    async with async_session_factory() as session, session.begin():
        company = await session.get(Company, company_id)
        if company is None or company.org_id != run.org_id:
            raise LookupError(f"Company {company_id} not found")
        bundles = await load_bundles(session, run.org_id, [UUID(s) for s in service_ids])
        profile = mapping.company_profile(company)
    if not bundles:
        raise LookupError("No active services to analyze")
    inp = ai.AnalysisInput(run_id=run.id, company=profile, services=bundles, mode=mode, now=datetime.now(UTC))
    deps = worker_context.analysis_deps(run.org_id)
    cancelled = asyncio.Event()
    deps.progress = CancellableProgress(deps.progress, run.id, cancelled)
    graph = ai.build_analysis_graph(deps)
    return await _until_cancelled(run.id, ai.run_analysis(graph, inp), cancelled)


@broker.task(task_name="analyze_company", retry_on_error=False)
async def analyze_company(
    run_id: str, company_id: str, service_ids: list[str], mode: str = "incremental"
) -> str:
    await worker_context.start()
    async with async_session_factory() as session, session.begin():
        run = await session.get(AnalysisRun, UUID(run_id))
        if run is None or run.status == "cancelled":
            return "skipped"
        if run.status == "queued":
            run.status = "running"
            run.started_at = datetime.now(UTC)

    outcome, scores, message = "done", [], ""
    try:
        output = await _analyze(run, UUID(company_id), service_ids, mode)
        scores = output.get("scores", [])
        failed = [e for e in output.get("errors", []) if e.service_id is not None]
        if failed and not scores:
            outcome, message = "failed", "; ".join(f"{e.stage}: {e.message}" for e in failed)
    except RunCancelled:
        outcome, message = "cancelled", "Run cancelled"
    except ai.AnalysisPaused as e:
        outcome, scores, message = "paused", e.output.get("scores", []), str(e)
    except ai.QuotaExhausted as e:
        outcome, message = "paused", str(e)
    except Exception as e:
        log.exception("analyze_company_failed", run_id=run_id, company_id=company_id)
        outcome, message = "failed", f"{type(e).__name__}: {e}"

    if outcome in ("done", "paused"):
        async with async_session_factory() as session, session.begin():
            row = await session.get(Company, UUID(company_id))
            if row is not None:
                row.last_analyzed_at = datetime.now(UTC)
    data = {
        "company_id": company_id,
        "status": outcome,
        "message": message,
        "scores": [{"service_id": str(s.service_id), "priority": s.priority, "tier": s.tier} for s in scores],
    }
    company_ref = UUID(company_id) if await _company_exists(UUID(company_id)) else None
    await _event(run, company_ref, "company", outcome, message or f"Company {outcome}", data)
    if outcome == "cancelled":  # the run is already closed by the cancel request
        return outcome
    state = await _count(run.id, outcome, message if outcome == "failed" else None)
    await _event(run, None, "run", "progress", "", state["progress"])
    if state["status"] not in TERMINAL:
        await _finish_if_complete(run, state["progress"])
    return outcome


async def _company_exists(company_id: UUID) -> bool:
    """run_event.company_id is a foreign key: a missing company is reported with the id in the payload only."""
    async with async_session_factory() as session:
        return await session.get(Company, company_id) is not None


@broker.task(task_name="expand_question", retry_on_error=False)
async def expand_question(question_id: str) -> str:
    await worker_context.start()
    async with async_session_factory() as session:
        question = await session.get(SignalQuestion, UUID(question_id))
        if question is None:
            return "missing"
        service = await session.get(Service, question.service_id)
        bundle = await load_bundle(session, service)
        await session.commit()
    config = next((q for q in bundle.questions if q.id == question.id), None)
    if config is None:  # inactive question
        return "skipped"
    try:
        expansion = await ai.expand_question(worker_context.llm, bundle, config)
        status = "ready"
    except ai.QuotaExhausted:
        return "pending"  # keywords_status stays pending; retried later
    except Exception:
        log.exception("expand_question_failed", question_id=question_id)
        expansion, status = None, "failed"
    async with async_session_factory() as session, session.begin():
        row = await session.get(SignalQuestion, UUID(question_id))
        if row is None:
            return "missing"
        if expansion is not None:
            row.keywords = expansion.keywords
            row.job_titles = expansion.job_titles
            row.negative_terms = expansion.negative_terms
        row.keywords_status = status
    return status
