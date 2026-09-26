"""Runs use cases: create, cancel, retry. Every state change writes a run_event (replay) and publishes it (live)."""

from uuid import UUID

import redis.asyncio as aioredis
from leadradar_core.errors import ConflictException, DomainException, NotFoundException
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.activity import events as domain_events
from leadradar_core.modules.config.models import Service
from leadradar_core.modules.runs import events
from leadradar_core.modules.runs.models import RUN_ACTIVE_STATUSES, AnalysisRun, RunEvent
from leadradar_core.modules.runs.schemas import RunCreate
from leadradar_core.worker.enqueue import enqueue_analysis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


class InvalidRunRequest(DomainException):
    """The request is well-formed but references ids that do not exist in the org (HTTP 422)."""

    def __init__(self, message: str, details: dict) -> None:
        super().__init__(code="validation_error", message=message, details=details)


async def get_run(session: AsyncSession, org_id: UUID, run_id: UUID) -> AnalysisRun:
    run = await session.get(AnalysisRun, run_id)
    if run is None or run.org_id != org_id:
        raise NotFoundException("Run not found")
    return run


async def _missing(session: AsyncSession, model, org_id: UUID, ids: list[UUID]) -> list[str]:
    found = set(
        (await session.execute(select(model.id).where(model.org_id == org_id, model.id.in_(ids)))).scalars()
    )
    return [str(i) for i in ids if i not in found]


async def create_run(
    session: AsyncSession, org_id: UUID, user_id: UUID | None, data: RunCreate
) -> AnalysisRun:
    """A new run is `queued`; one analyze_company task per company goes to the worker."""
    company_ids = list(dict.fromkeys(data.company_ids))
    service_ids = list(dict.fromkeys(data.service_ids))
    # a company that does not exist would never report back and the run would never finish
    if missing := await _missing(session, Company, org_id, company_ids):
        raise InvalidRunRequest("Unknown company ids", {"company_ids": missing})
    if service_ids and (missing := await _missing(session, Service, org_id, service_ids)):
        raise InvalidRunRequest("Unknown service ids", {"service_ids": missing})

    run = AnalysisRun(
        org_id=org_id,
        kind=data.kind,
        status="queued",
        params={
            "company_ids": [str(c) for c in company_ids],
            "service_ids": [str(s) for s in service_ids],
            "mode": data.mode,
        },
        progress={"done": 0, "total": len(company_ids), "failed": 0, "paused": 0},
        created_by=user_id,
    )
    session.add(run)
    await session.flush()
    await events.add_event(
        session,
        org_id=org_id,
        run_id=run.id,
        stage="run",
        status="queued",
        message="Run queued for processing",
        payload=run.progress,
    )
    await session.commit()
    await enqueue_analysis(run.id, company_ids, service_ids, data.mode)
    return run


async def cancel_run(
    session: AsyncSession, redis: aioredis.Redis | None, org_id: UUID, run_id: UUID
) -> AnalysisRun:
    """Only a queued or running run can be cancelled (409 otherwise). Workers see the status between stages
    and stop; tasks not started yet are skipped."""
    run = await get_run(session, org_id, run_id)
    cancelled = (
        await session.execute(
            update(AnalysisRun)
            .where(AnalysisRun.id == run_id, AnalysisRun.status.in_(RUN_ACTIVE_STATUSES))
            .values(status="cancelled", finished_at=func.now(), updated_at=func.now())
            .returning(AnalysisRun.id)
        )
    ).first()
    if cancelled is None:
        await session.rollback()
        await session.refresh(run)
        raise ConflictException(f"Run is already {run.status}", {"status": run.status})
    await session.refresh(run)
    row = await events.add_event(
        session,
        org_id=org_id,
        run_id=run.id,
        stage="run",
        status="cancelled",
        message="Run cancelled by user",
        payload={"status": "cancelled", **(run.progress or {})},
    )
    domain_events.run_finished(session, org_id, run.id, "cancelled", run.progress or {})
    await session.commit()
    await events.publish(redis, row)
    return run


async def retry_failed(
    session: AsyncSession, redis: aioredis.Redis | None, org_id: UUID, run_id: UUID
) -> AnalysisRun:
    """Re-enqueue the companies whose last outcome in this run is failed or paused.

    Same run_id + company: the graph resumes from its checkpoint. Always incremental, so services that already
    finished in this run are not extracted again.
    """
    run = await get_run(session, org_id, run_id)
    if run.status == "cancelled":
        raise ConflictException("A cancelled run cannot be retried", {"status": run.status})
    last_outcome: dict[UUID, str] = {}
    rows = await session.execute(
        select(RunEvent.company_id, RunEvent.status)
        .where(RunEvent.run_id == run.id, RunEvent.stage == "company")
        .order_by(RunEvent.id.asc())
    )
    for company_id, outcome in rows.all():
        last_outcome[company_id] = outcome
    retry = [c for c, outcome in last_outcome.items() if c is not None and outcome in ("failed", "paused")]
    if not retry:
        return run

    progress = dict(run.progress or {})
    for outcome in ("failed", "paused"):
        progress[outcome] = max(
            0, progress.get(outcome, 0) - sum(1 for c in retry if last_outcome[c] == outcome)
        )
    run.progress = progress
    run.status = "running" if run.status == "running" else "queued"
    run.finished_at = None
    row = await events.add_event(
        session,
        org_id=org_id,
        run_id=run.id,
        stage="run",
        status="retrying",
        message=f"Retrying {len(retry)} failed or paused companies",
        payload=progress,
    )
    await session.commit()
    await session.refresh(run)
    await events.publish(redis, row)
    service_ids = [UUID(s) for s in (run.params or {}).get("service_ids", [])]
    await enqueue_analysis(run.id, retry, service_ids, "incremental")
    return run
