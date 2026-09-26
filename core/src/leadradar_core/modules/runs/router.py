"""Runs API and the run SSE stream (SPEC core CO-09, CO-10).

SSE (`GET|POST /runs/{id}/events`): first a replay of stored run_event rows after `Last-Event-ID`, then live
events from Redis `run:{id}`. Live and replay use the same event names and the same data (the run_event payload):

- `run.progress`  `{done, total, failed, paused}`
- `company.stage` `{company_id, service_id, stage, status, message, ...}`
- `company.done`  `{company_id, status, message, scores: [{service_id, priority, tier}]}`
- `run.finished`  `{status, done, total, failed, paused}` — the stream ends after it

Every event's `id` is `run_event.id`. Live messages already sent by the replay are skipped. A keep-alive comment
goes out every 15 s; at the same time rows missed on the live channel are replayed from the database.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import async_session_factory, get_db_session
from leadradar_core.modules.runs import events, service
from leadradar_core.modules.runs.models import RUN_TERMINAL_STATUSES, AnalysisRun, RunEvent
from leadradar_core.modules.runs.schemas import RunCreate, RunOut
from leadradar_core.settings import settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/runs", tags=["runs"])

KEEP_ALIVE_S = 15.0


def _redis(request: Request) -> aioredis.Redis | None:
    return getattr(request.app.state, "redis", None)


@router.get("", response_model=list[RunOut])
async def list_runs(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = Query(default=20, ge=1, le=100),
) -> list[RunOut]:
    stmt = (
        select(AnalysisRun)
        .where(AnalysisRun.org_id == principal.org_id)
        .order_by(AnalysisRun.created_at.desc())
        .limit(limit)
    )
    res = await session.execute(stmt)
    return [RunOut.model_validate(r) for r in res.scalars().all()]


@router.post("", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def create_run(
    run_in: RunCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunOut:
    try:
        run = await service.create_run(session, principal.org_id, principal.user_id, run_in)
    except service.InvalidRunRequest as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"message": e.message, **e.details}
        ) from e
    return RunOut.model_validate(run)


@router.get("/{id}", response_model=RunOut)
async def get_run(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunOut:
    return RunOut.model_validate(await service.get_run(session, principal.org_id, id))


@router.post(
    "/{id}/cancel",
    response_model=RunOut,
    responses={status.HTTP_409_CONFLICT: {"description": "The run has already finished"}},
)
async def cancel_run(
    id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunOut:
    run = await service.cancel_run(session, _redis(request), principal.org_id, id)
    return RunOut.model_validate(run)


@router.post(
    "/{id}/retry-failed",
    response_model=RunOut,
    responses={status.HTTP_409_CONFLICT: {"description": "The run was cancelled"}},
)
async def retry_failed(
    id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunOut:
    run = await service.retry_failed(session, _redis(request), principal.org_id, id)
    return RunOut.model_validate(run)


async def _stored_events(run_id: UUID, org_id: UUID, after: int) -> tuple[list[RunEvent], str | None]:
    """run_event rows after `after` and the current run status (a short session: the stream may be long)."""
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.org_id == org_id, RunEvent.id > after)
                    .order_by(RunEvent.id.asc())
                )
            )
            .scalars()
            .all()
        )
        run_status = (
            await session.execute(select(AnalysisRun.status).where(AnalysisRun.id == run_id))
        ).scalar_one_or_none()
    return list(rows), run_status


def _sse(row: RunEvent) -> ServerSentEvent:
    # FastAPI JSON-encodes data itself; a pre-encoded string would come out double-encoded
    return ServerSentEvent(
        id=str(row.id), event=events.sse_event_name(row.stage, row.status), data=events.event_data(row)
    )


async def _subscribe(run_id: UUID) -> tuple[aioredis.Redis, Any] | None:
    if settings.ENV == "test":
        return None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(events.channel(run_id))
        return client, pubsub
    except Exception as e:
        log.warning("sse_subscribe_failed", run_id=str(run_id), error=str(e))
        return None


async def _stream_events(
    run_id: UUID, org_id: UUID, last_event_id: int | None
) -> AsyncIterator[ServerSentEvent]:
    # subscribe before the replay, so nothing published in between is lost (duplicates are skipped by id)
    live = await _subscribe(run_id)
    highest = last_event_id or 0
    try:
        rows, run_status = await _stored_events(run_id, org_id, highest)
        finished = False
        for row in rows:
            highest = row.id
            event = _sse(row)
            finished = event.event == events.RUN_FINISHED  # a retried run continues after an earlier finish
            yield event
        if run_status in RUN_TERMINAL_STATUSES:
            if not finished:  # the terminal row was replayed before Last-Event-ID: still tell the client
                yield ServerSentEvent(event=events.RUN_FINISHED, data={"status": run_status})
            return
        if live is None:  # no live channel (tests, Redis down): the client reconnects with Last-Event-ID
            return

        _, pubsub = live
        last_sent = time.monotonic()
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("data"):
                try:
                    payload = json.loads(msg["data"])
                    event_id = int(payload["id"])
                    name = payload["event"]
                except (ValueError, KeyError, TypeError):
                    log.warning("sse_bad_message", run_id=str(run_id))
                    continue
                if event_id <= highest:
                    continue
                highest = event_id
                last_sent = time.monotonic()
                yield ServerSentEvent(id=str(event_id), event=name, data=payload.get("data", {}))
                if name == events.RUN_FINISHED:
                    return
            elif time.monotonic() - last_sent >= KEEP_ALIVE_S:
                last_sent = time.monotonic()
                yield ServerSentEvent(comment="keep-alive")
                rows, run_status = await _stored_events(run_id, org_id, highest)
                for row in rows:  # catch up on messages lost by pub/sub
                    highest = row.id
                    event = _sse(row)
                    yield event
                    if event.event == events.RUN_FINISHED and run_status in RUN_TERMINAL_STATUSES:
                        return
    except asyncio.CancelledError:
        raise
    finally:
        if live is not None:
            client, pubsub = live
            try:
                await pubsub.unsubscribe(events.channel(run_id))
                await pubsub.aclose()
                await client.aclose()
            except Exception:
                pass


async def _sse_run(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AnalysisRun:
    """Checked in a dependency: an exception inside the SSE generator can no longer become a 404."""
    return await service.get_run(session, principal.org_id, id)


@router.get("/{id}/events", response_class=EventSourceResponse)
async def get_run_events(
    run: Annotated[AnalysisRun, Depends(_sse_run)],
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
    query_last_id: int | None = Query(default=None, alias="last_event_id"),
) -> AsyncIterator[ServerSentEvent]:
    after = last_event_id if last_event_id is not None else query_last_id
    async for ev in _stream_events(run.id, run.org_id, after):
        yield ev


@router.post("/{id}/events", response_class=EventSourceResponse)
async def post_run_events(
    run: Annotated[AnalysisRun, Depends(_sse_run)],
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
    query_last_id: int | None = Query(default=None, alias="last_event_id"),
) -> AsyncIterator[ServerSentEvent]:
    after = last_event_id if last_event_id is not None else query_last_id
    async for ev in _stream_events(run.id, run.org_id, after):
        yield ev
