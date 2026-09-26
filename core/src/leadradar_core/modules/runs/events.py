"""Run events: every event is one run_event row (SSE replay, audit) and the same event live on Redis run:{run_id}.

SSE event names (SPEC core §1.4.1) are derived from the row's (stage, status), so the live stream and the replay
from the database always use the same name, and both send run_event.payload as the data:

| event           | run_event row                                   | data                                            |
|-----------------|-------------------------------------------------|-------------------------------------------------|
| `run.progress`  | stage "run", non-terminal status (queued, …)    | {done, total, failed, paused}                   |
| `run.finished`  | stage "run", terminal status                    | {status, done, total, failed, paused}           |
| `company.done`  | stage "company" (done / failed / paused / …)    | {company_id, status, message, scores[]}         |
| `company.stage` | a graph stage (resolving … scoring)             | {company_id, service_id, stage, status, message}|

The SSE id of every event is run_event.id.
"""

import json
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from leadradar_core.modules.runs.models import RUN_TERMINAL_STATUSES, RunEvent
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

log = get_logger(__name__)

RUN_PROGRESS = "run.progress"
RUN_FINISHED = "run.finished"
COMPANY_STAGE = "company.stage"
COMPANY_DONE = "company.done"


def channel(run_id: UUID) -> str:
    return f"run:{run_id}"


def sse_event_name(stage: str, status: str) -> str:
    if stage == "run":
        return RUN_FINISHED if status in RUN_TERMINAL_STATUSES else RUN_PROGRESS
    if stage == "company":
        return COMPANY_DONE
    return COMPANY_STAGE


def event_data(row: RunEvent) -> dict[str, Any]:
    """The SSE data of a stored event; rows without a payload get the company.stage shape."""
    if row.payload is not None:
        return row.payload
    data: dict[str, Any] = {
        "company_id": str(row.company_id) if row.company_id else None,
        "service_id": str(row.service_id) if row.service_id else None,
        "stage": row.stage,
        "status": row.status,
        "message": row.message or "",
    }
    if row.stage == "run":
        data = {"status": row.status}
    return data


def json_safe(data: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(data, default=str))


def new_event(
    *,
    org_id: UUID,
    run_id: UUID,
    stage: str,
    status: str,
    payload: dict[str, Any],
    message: str = "",
    company_id: UUID | None = None,
    service_id: UUID | None = None,
) -> RunEvent:
    return RunEvent(
        org_id=org_id,
        run_id=run_id,
        company_id=company_id,
        service_id=service_id,
        stage=stage,
        status=status,
        message=message,
        payload=json_safe(payload),
    )


async def add_event(session: AsyncSession, **fields: Any) -> RunEvent:
    """Add a run_event row (flushed, so its id is known); the caller commits and then publishes it."""
    row = new_event(**fields)
    session.add(row)
    await session.flush()
    return row


async def publish(redis: aioredis.Redis | None, row: RunEvent) -> None:
    """Relay a committed run_event row to live SSE subscribers. Never raises: replay covers lost messages."""
    if redis is None:
        return
    message = {"event": sse_event_name(row.stage, row.status), "id": row.id, "data": event_data(row)}
    try:
        await redis.publish(channel(row.run_id), json.dumps(message, default=str))
    except Exception as e:
        log.warning("run_event_publish_failed", run_id=str(row.run_id), error=str(e))
