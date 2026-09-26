"""Periodic tasks (SPEC core CO-22, CO-17), fired by `taskiq scheduler leadradar_core.worker.broker:scheduler`
from their `schedule` labels and executed by the worker:

- refresh_tracked  (APP_REFRESH_CRON, every 6 h): one `refresh` run per org over tracked companies that were
  analyzed before; the analysis is incremental, so the LLM is called only for new fragments.
- resume_paused    (APP_RESUME_PAUSED_CRON, every 15 min): companies paused by the LLM quota continue from
  their checkpoint (same run_id and company → same LangGraph thread).
- dispatch_events  (APP_DISPATCH_EVENTS_CRON, every minute): the outbox dispatcher.
- evaluate_jobs_thresholds (APP_JOBS_ALERTS_CRON, hourly): alert rules of kind jobs_threshold ("100+ job
  postings in a day"); a (rule, company) is notified once per window.
- send_email_digests (APP_ALERTS_DIGEST_CRON, 07:00 and 15:00 UTC): queued e-mails of alert rules with
  email_frequency twice_daily (every run) or daily (the run at APP_ALERTS_DAILY_DIGEST_HOUR), one e-mail per
  user.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text
from structlog import get_logger

from leadradar_core import integrations
from leadradar_core.db.session import async_session_factory
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.activity.dispatcher import Consumer, dispatch_pending
from leadradar_core.modules.alerts import service as alerts_service
from leadradar_core.modules.config.models import Service
from leadradar_core.modules.runs.models import AnalysisRun, RunEvent
from leadradar_core.settings import settings
from leadradar_core.worker.broker import broker

log = get_logger(__name__)

ACTIVE_RUN = ("pending", "running")
RESUMABLE_RUN = ("running", "partial")
ACTIVE_RUN_WINDOW = timedelta(hours=24)  # an older unfinished refresh run no longer blocks a new one
RESUME_WINDOW = timedelta(days=7)
DISPATCH_MAX_PASSES = 10


async def enqueue(run_id: UUID, company_ids: list[UUID], service_ids: list[UUID]) -> None:
    """Scheduled work always goes through the queue (the scheduler implies a running worker)."""
    from leadradar_core.worker.tasks import analyze_company

    for company_id in company_ids:
        await analyze_company.kiq(str(run_id), str(company_id), [str(s) for s in service_ids], "incremental")


# --- refresh_tracked ---------------------------------------------------------------------------


async def refresh_tracked_companies(now: datetime | None = None, only_org: UUID | None = None) -> list[UUID]:
    """Creates a refresh run per org (or only `only_org`) with due tracked companies; returns the run ids."""
    now = now or datetime.now(UTC)
    due_before = now - timedelta(hours=settings.REFRESH_MIN_AGE_H)
    runs: list[tuple[UUID, list[UUID], list[UUID]]] = []
    async with async_session_factory() as session, session.begin():
        orgs = select(Company.org_id).where(Company.is_tracked.is_(True)).group_by(Company.org_id)
        if only_org is not None:
            orgs = orgs.where(Company.org_id == only_org)
        for org_id in (await session.execute(orgs)).scalars().all():
            busy = (
                await session.execute(
                    select(func.count(AnalysisRun.id)).where(
                        AnalysisRun.org_id == org_id,
                        AnalysisRun.kind == "refresh",
                        AnalysisRun.status.in_(ACTIVE_RUN),
                        AnalysisRun.created_at > now - ACTIVE_RUN_WINDOW,
                    )
                )
            ).scalar_one()
            if busy:
                log.info("refresh_skipped_run_active", org_id=str(org_id))
                continue
            company_ids = list(
                (
                    await session.execute(
                        select(Company.id)
                        .where(
                            Company.org_id == org_id,
                            Company.is_tracked.is_(True),
                            Company.last_analyzed_at.is_not(None),
                            Company.last_analyzed_at < due_before,
                        )
                        .order_by(Company.last_analyzed_at)
                    )
                ).scalars()
            )
            service_ids = list(
                (
                    await session.execute(
                        select(Service.id).where(Service.org_id == org_id, Service.is_active.is_(True))
                    )
                ).scalars()
            )
            if not company_ids or not service_ids:
                continue
            run = AnalysisRun(
                org_id=org_id,
                kind="refresh",
                status="pending",
                params={
                    "company_ids": [str(c) for c in company_ids],
                    "service_ids": [str(s) for s in service_ids],
                    "trigger": "scheduler",
                },
                progress={"done": 0, "total": len(company_ids), "failed": 0, "paused": 0},
            )
            session.add(run)
            await session.flush()
            session.add(
                RunEvent(
                    org_id=org_id,
                    run_id=run.id,
                    stage="run",
                    status="pending",
                    message="Scheduled refresh of tracked companies",
                    payload=run.progress,
                )
            )
            runs.append((run.id, company_ids, service_ids))
    for run_id, company_ids, service_ids in runs:
        await enqueue(run_id, company_ids, service_ids)
        log.info("refresh_enqueued", run_id=str(run_id), companies=len(company_ids))
    return [r[0] for r in runs]


# --- resume_paused -----------------------------------------------------------------------------

LAST_COMPANY_OUTCOME = text(
    """
    SELECT DISTINCT ON (company_id) company_id, status
    FROM core.run_event
    WHERE run_id = :run_id AND stage = 'company' AND company_id IS NOT NULL
    ORDER BY company_id, id DESC
    """
)


async def resume_paused_companies(
    now: datetime | None = None, only_org: UUID | None = None
) -> dict[UUID, list[UUID]]:
    """Re-enqueues companies whose last outcome in a recent run is `paused`; returns {run_id: company_ids}."""
    now = now or datetime.now(UTC)
    stmt = select(AnalysisRun.id).where(
        AnalysisRun.status.in_(RESUMABLE_RUN),
        AnalysisRun.created_at > now - RESUME_WINDOW,
        AnalysisRun.progress["paused"].as_integer() > 0,
    )
    if only_org is not None:
        stmt = stmt.where(AnalysisRun.org_id == only_org)
    async with async_session_factory() as session:
        candidates = (await session.execute(stmt)).scalars().all()
    resumed: dict[UUID, list[UUID]] = {}
    for run_id in candidates:
        async with async_session_factory() as session, session.begin():
            # the lock serializes concurrent resume ticks; outcomes are re-read under it
            run = (
                await session.execute(
                    select(AnalysisRun)
                    .where(AnalysisRun.id == run_id, AnalysisRun.status.in_(RESUMABLE_RUN))
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if run is None:
                continue
            outcomes = (await session.execute(LAST_COMPANY_OUTCOME, {"run_id": run_id})).all()
            paused = [row.company_id for row in outcomes if row.status == "paused"]
            if not paused:
                continue
            await session.execute(
                text(
                    "UPDATE core.analysis_run SET status = 'running', finished_at = NULL, updated_at = now(), "
                    "progress = jsonb_set(progress, '{paused}', "
                    "to_jsonb(GREATEST(0, COALESCE((progress->>'paused')::int, 0) - :n))) WHERE id = :id"
                ),
                {"n": len(paused), "id": run_id},
            )
            session.add_all(
                RunEvent(
                    org_id=run.org_id,
                    run_id=run_id,
                    company_id=company_id,
                    stage="company",
                    status="resuming",
                    message="Resuming after LLM quota pause",
                )
                for company_id in paused
            )
            service_ids = [UUID(s) for s in (run.params or {}).get("service_ids", [])]
        await enqueue(run_id, paused, service_ids)
        resumed[run_id] = paused
        log.info("paused_companies_resumed", run_id=str(run_id), companies=len(paused))
    return resumed


# --- dispatch_events ---------------------------------------------------------------------------

_consumers: list[Consumer] | None = None


def consumers() -> list[Consumer]:
    """Built once per worker process: add-ons log once why they are disabled and keep their state."""
    global _consumers
    if _consumers is None:
        _consumers = integrations.enabled_consumers()
        log.info("event_consumers", names=[c.name for c in _consumers])
    return _consumers


async def dispatch_outbox() -> int:
    """Dispatcher passes until the backlog is drained (bounded); returns the number of claimed events."""
    total = 0
    for _ in range(DISPATCH_MAX_PASSES):
        result = await dispatch_pending(
            async_session_factory,
            consumers(),
            batch_size=settings.EVENTS_DISPATCH_BATCH,
            max_attempts=settings.EVENTS_MAX_ATTEMPTS,
        )
        total += result.claimed
        if result.claimed < settings.EVENTS_DISPATCH_BATCH:
            break
    return total


# --- evaluate_jobs_thresholds ------------------------------------------------------------------


async def evaluate_jobs_thresholds(now: datetime | None = None, only_org: UUID | None = None) -> int:
    """One pass over the jobs_threshold alert rules; returns the number of notifications created."""
    from leadradar_core.integrations.alert_rules import mailer_for

    created = await alerts_service.evaluate_jobs_thresholds(
        async_session_factory, settings.PUBLIC_ORIGIN, mailer_for(settings), now=now, only_org=only_org
    )
    if created:
        log.info("jobs_threshold_notified", notifications=len(created))
    return len(created)


# --- send_email_digests ------------------------------------------------------------------------


async def send_email_digests(now: datetime | None = None, only_org: UUID | None = None) -> int:
    """One pass over the queued alert e-mails; returns the number of digest e-mails sent."""
    from leadradar_core.integrations.alert_rules import mailer_for

    sent = await alerts_service.send_email_digests(
        async_session_factory, settings.PUBLIC_ORIGIN, mailer_for(settings), now=now, only_org=only_org
    )
    if sent:
        log.info("email_digests_sent", emails=sent)
    return sent


# --- task registration -------------------------------------------------------------------------


@broker.task(
    task_name="refresh_tracked",
    retry_on_error=False,
    schedule=[{"cron": settings.REFRESH_CRON, "schedule_id": "refresh_tracked"}],
)
async def refresh_tracked() -> int:
    return len(await refresh_tracked_companies())


@broker.task(
    task_name="resume_paused",
    retry_on_error=False,
    schedule=[{"cron": settings.RESUME_PAUSED_CRON, "schedule_id": "resume_paused"}],
)
async def resume_paused() -> int:
    return sum(len(c) for c in (await resume_paused_companies()).values())


@broker.task(
    task_name="dispatch_events",
    retry_on_error=False,
    schedule=[{"cron": settings.DISPATCH_EVENTS_CRON, "schedule_id": "dispatch_events"}],
)
async def dispatch_events() -> int:
    return await dispatch_outbox()


@broker.task(
    task_name="evaluate_jobs_thresholds",
    retry_on_error=False,
    schedule=[{"cron": settings.JOBS_ALERTS_CRON, "schedule_id": "evaluate_jobs_thresholds"}],
)
async def evaluate_jobs_thresholds_task() -> int:
    return await evaluate_jobs_thresholds()


@broker.task(
    task_name="send_email_digests",
    retry_on_error=False,
    schedule=[{"cron": settings.ALERTS_DIGEST_CRON, "schedule_id": "send_email_digests"}],
)
async def send_email_digests_task() -> int:
    return await send_email_digests()
