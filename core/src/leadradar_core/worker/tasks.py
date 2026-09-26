"""Worker tasks (SPEC core §1.4.2): analyze_company runs the leadradar-ai graph, expand_question generates
multilingual keywords. Run progress is counted atomically in analysis_run.progress; the last company of a
run sets its final status and emits run.finished."""

from datetime import UTC, datetime
from uuid import UUID

import leadradar_ai as ai
from sqlalchemy import text
from structlog import get_logger

from leadradar_core.adapters import mapping
from leadradar_core.adapters.progress import publish
from leadradar_core.db.session import async_session_factory
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.activity import events as domain_events
from leadradar_core.modules.config.models import Service, SignalQuestion
from leadradar_core.modules.intelligence.service import load_bundle, load_bundles
from leadradar_core.modules.runs.models import AnalysisRun, RunEvent
from leadradar_core.worker.broker import broker
from leadradar_core.worker.deps import worker_context

log = get_logger(__name__)

TERMINAL = ("succeeded", "partial", "failed", "cancelled")


async def _event(
    run: AnalysisRun,
    company_id: UUID | None,
    stage: str,
    status: str,
    message: str,
    data: dict,
    channel_event: str,
) -> None:
    async with async_session_factory() as session, session.begin():
        row = RunEvent(
            org_id=run.org_id,
            run_id=run.id,
            company_id=company_id,
            stage=stage,
            status=status,
            message=message,
            payload=data,
        )
        session.add(row)
        await session.flush()
        event_id = row.id
    await publish(worker_context.redis, run.id, channel_event, event_id, data)


async def _count(run_id: UUID, outcome: str) -> dict:
    """Atomically increment progress[outcome]; returns progress and status after the update."""
    async with async_session_factory() as session, session.begin():
        row = (
            await session.execute(
                text(
                    "UPDATE core.analysis_run SET progress = jsonb_set(progress, ARRAY[:key], "
                    "to_jsonb(COALESCE((progress->>:key)::int, 0) + 1)), updated_at = now() "
                    "WHERE id = :id RETURNING progress, status"
                ),
                {"key": outcome, "id": run_id},
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
        await _event(
            run, None, "run", status, f"Run {status}", {"status": status, **progress}, "run.finished"
        )


@broker.task(task_name="analyze_company", retry_on_error=False)
async def analyze_company(
    run_id: str, company_id: str, service_ids: list[str], mode: str = "incremental"
) -> str:
    await worker_context.start()
    async with async_session_factory() as session, session.begin():
        run = await session.get(AnalysisRun, UUID(run_id))
        company = await session.get(Company, UUID(company_id))
        if run is None or company is None or run.status == "cancelled":
            return "skipped"
        if run.status == "pending":
            run.status = "running"
            run.started_at = datetime.now(UTC)
        bundles = await load_bundles(session, run.org_id, [UUID(s) for s in service_ids])
        profile = mapping.company_profile(company)

    inp = ai.AnalysisInput(run_id=run.id, company=profile, services=bundles, mode=mode, now=datetime.now(UTC))
    graph = ai.build_analysis_graph(worker_context.analysis_deps(run.org_id))
    outcome, scores, message = "done", [], ""
    try:
        output = await ai.run_analysis(graph, inp)
        scores = output.get("scores", [])
        failed = [e for e in output.get("errors", []) if e.service_id is not None]
        if failed and not scores:
            outcome, message = "failed", "; ".join(f"{e.stage}: {e.message}" for e in failed)
    except ai.AnalysisPaused as e:
        outcome, scores, message = "paused", e.output.get("scores", []), str(e)
    except ai.QuotaExhausted as e:
        outcome, message = "paused", str(e)
    except Exception as e:
        log.exception("analyze_company_failed", run_id=run_id, company_id=company_id)
        outcome, message = "failed", f"{type(e).__name__}: {e}"

    async with async_session_factory() as session, session.begin():
        row = await session.get(Company, UUID(company_id))
        if row is not None and outcome != "failed":
            row.last_analyzed_at = datetime.now(UTC)
    data = {
        "company_id": company_id,
        "status": outcome,
        "message": message,
        "scores": [{"service_id": str(s.service_id), "priority": s.priority, "tier": s.tier} for s in scores],
    }
    await _event(
        run, UUID(company_id), "company", outcome, message or f"Company {outcome}", data, "company.done"
    )
    state = await _count(run.id, outcome)
    await _event(run, None, "run", "progress", "", state["progress"], "run.progress")
    if state["status"] not in TERMINAL:
        await _finish_if_complete(run, state["progress"])
    return outcome


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
