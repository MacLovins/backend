"""ProgressSink port: every graph event → a run_event row (SSE replay) → Redis channel run:{run_id} (live SSE).

Event names and data are the same for live and replay (modules/runs/events.py): graph stages go out as
`company.stage` with the payload below.
"""

from typing import Any
from uuid import UUID

import leadradar_ai as ai
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from leadradar_core.modules.runs import events


def event_payload(event: ai.ProgressEvent) -> dict[str, Any]:
    return {
        "company_id": str(event.company_id),
        "service_id": str(event.service_id) if event.service_id else None,
        "stage": event.stage,
        "status": event.status,
        "message": event.message,
        **event.data,
    }


class RunProgressSink:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], redis: aioredis.Redis | None, org_id: UUID
    ) -> None:
        self._sessions = session_factory
        self._redis = redis
        self._org_id = org_id

    async def emit(self, event: ai.ProgressEvent) -> None:
        async with self._sessions() as session, session.begin():
            row = await events.add_event(
                session,
                org_id=self._org_id,
                run_id=event.run_id,
                company_id=event.company_id,
                service_id=event.service_id,
                stage=event.stage,
                status=event.status,
                message=event.message,
                payload=event_payload(event),
            )
        await events.publish(self._redis, row)
