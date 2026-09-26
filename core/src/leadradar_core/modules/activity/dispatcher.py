"""Outbox dispatcher (SPEC core CO-17, ARCHITECTURE §4.13).

Reads unprocessed `domain_event` rows, fans each one out to the registered consumers and marks it processed.

- At-least-once: an event is marked processed only after every interested consumer handled it; a consumer
  that raises is retried on the next pass (the consumers that already succeeded are remembered in
  `delivered_to` and are not called again). After `max_attempts` failed passes the event is given up:
  processed_at is set and `last_error` keeps the reason.
- Concurrency: rows are claimed with `SELECT … FOR UPDATE SKIP LOCKED` inside one transaction, so parallel
  dispatchers (several workers, the scheduler tick overlapping a slow pass) never deliver the same event twice
  at the same time.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from leadradar_core.modules.activity.models import DomainEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog import get_logger

log = get_logger(__name__)

ERROR_MAX_LEN = 2000


@dataclass(frozen=True)
class Event:
    """What a consumer sees: a detached, read-only copy of the outbox row."""

    id: UUID
    org_id: UUID
    type: str
    payload: dict[str, Any]
    created_at: datetime


class Consumer(Protocol):
    name: str
    event_types: frozenset[str]

    async def handle(self, event: Event) -> None: ...


@dataclass
class DispatchResult:
    claimed: int = 0
    processed: int = 0
    failed: int = 0
    given_up: int = 0


async def dispatch_pending(
    session_factory: async_sessionmaker[AsyncSession],
    consumers: Sequence[Consumer],
    *,
    batch_size: int = 100,
    max_attempts: int = 5,
) -> DispatchResult:
    """One dispatcher pass over at most `batch_size` of the oldest unprocessed events."""
    result = DispatchResult()
    async with session_factory() as session, session.begin():
        rows = (
            (
                await session.execute(
                    select(DomainEvent)
                    .where(DomainEvent.processed_at.is_(None))
                    .order_by(DomainEvent.created_at, DomainEvent.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        result.claimed = len(rows)
        for row in rows:
            event = Event(
                id=row.id,
                org_id=row.org_id,
                type=row.type,
                payload=dict(row.payload),
                created_at=row.created_at,
            )
            delivered = set(row.delivered_to or [])
            errors = []
            for consumer in consumers:
                if consumer.name in delivered or event.type not in consumer.event_types:
                    continue
                try:
                    await consumer.handle(event)
                    delivered.add(consumer.name)
                except Exception as e:
                    log.warning(
                        "event_consumer_failed", consumer=consumer.name, event_id=str(row.id), error=str(e)
                    )
                    errors.append(f"{consumer.name}: {type(e).__name__}: {e}")
            row.delivered_to = sorted(delivered)
            now = datetime.now(UTC)
            if not errors:
                row.processed_at = now
                result.processed += 1
                continue
            row.attempts += 1
            row.last_error = "; ".join(errors)[:ERROR_MAX_LEN]
            result.failed += 1
            if row.attempts >= max_attempts:
                row.processed_at = now
                result.given_up += 1
                log.error("event_given_up", event_id=str(row.id), type=row.type, error=row.last_error)
    return result
