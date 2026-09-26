"""ProgressSink port: every graph event → a run_event row (SSE replay) → Redis channel run:{run_id} (live SSE).

The runs router already replays run_event rows and relays messages from run:{id} (modules/runs/router.py).
"""

import json
from typing import Any
from uuid import UUID

import leadradar_ai as ai
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog import get_logger

from leadradar_core.modules.runs.models import RunEvent

log = get_logger(__name__)


def event_payload(event: ai.ProgressEvent) -> dict[str, Any]:
    return {
        "company_id": str(event.company_id),
        "service_id": str(event.service_id) if event.service_id else None,
        "stage": event.stage,
        "status": event.status,
        "message": event.message,
        **event.data,
    }


async def publish(redis: aioredis.Redis | None, run_id: UUID, event: str, event_id: int, data: dict) -> None:
    if redis is None:
        return
    try:
        await redis.publish(
            f"run:{run_id}", json.dumps({"event": event, "id": event_id, "data": data}, default=str)
        )
    except Exception as e:
        log.warning("progress_publish_failed", run_id=str(run_id), error=str(e))


class RunProgressSink:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], redis: aioredis.Redis | None, org_id: UUID
    ) -> None:
        self._sessions = session_factory
        self._redis = redis
        self._org_id = org_id

    async def emit(self, event: ai.ProgressEvent) -> None:
        payload = event_payload(event)
        async with self._sessions() as session, session.begin():
            row = RunEvent(
                org_id=self._org_id,
                run_id=event.run_id,
                company_id=event.company_id,
                service_id=event.service_id,
                stage=event.stage,
                status=event.status,
                message=event.message,
                payload=json.loads(json.dumps(payload, default=str)),
            )
            session.add(row)
            await session.flush()
            event_id = row.id
        await publish(self._redis, event.run_id, "company.stage", event_id, payload)
