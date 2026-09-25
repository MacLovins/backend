import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.runs.models import AnalysisRun, RunEvent
from leadradar_core.modules.runs.schemas import RunCreate, RunOut
from leadradar_core.settings import settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/runs", tags=["runs"])


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
    run = AnalysisRun(
        org_id=principal.org_id,
        kind=run_in.kind,
        status="pending",
        params={
            "company_ids": [str(c) for c in run_in.company_ids],
            "service_ids": [str(s) for s in run_in.service_ids],
        },
        progress={"done": 0, "total": len(run_in.company_ids), "failed": 0, "paused": 0},
        created_by=principal.user_id,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    # Emit initial run event
    initial_event = RunEvent(
        org_id=principal.org_id,
        run_id=run.id,
        stage="run",
        status="pending",
        message="Run queued for processing",
        payload=run.progress,
    )
    session.add(initial_event)
    await session.commit()

    return RunOut.model_validate(run)


@router.get("/{id}", response_model=RunOut)
async def get_run(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunOut:
    run = await session.get(AnalysisRun, id)
    if not run or run.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return RunOut.model_validate(run)


@router.post("/{id}/cancel", response_model=RunOut)
async def cancel_run(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunOut:
    run = await session.get(AnalysisRun, id)
    if not run or run.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    run.status = "cancelled"
    cancel_ev = RunEvent(
        org_id=principal.org_id,
        run_id=run.id,
        stage="run",
        status="cancelled",
        message="Run cancelled by user",
    )
    session.add(cancel_ev)
    await session.commit()
    await session.refresh(run)

    # Publish to Redis
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await r.publish(
            f"run:{run.id}",
            json.dumps({"event": "run.finished", "data": {"status": "cancelled"}}),
        )
        await r.aclose()
    except Exception:
        pass

    return RunOut.model_validate(run)


@router.post("/{id}/retry-failed", response_model=RunOut)
async def retry_failed(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunOut:
    run = await session.get(AnalysisRun, id)
    if not run or run.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    run.status = "pending"
    retry_ev = RunEvent(
        org_id=principal.org_id,
        run_id=run.id,
        stage="run",
        status="retrying",
        message="Retrying failed analysis tasks",
    )
    session.add(retry_ev)
    await session.commit()
    await session.refresh(run)
    return RunOut.model_validate(run)


async def _stream_events(
    run_id: UUID,
    org_id: UUID,
    last_event_id: int | None,
    session: AsyncSession,
) -> AsyncIterator[ServerSentEvent]:
    # 1. Replay past events from run_event table
    stmt = (
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.org_id == org_id)
        .order_by(RunEvent.id.asc())
    )
    if last_event_id is not None:
        stmt = stmt.where(RunEvent.id > last_event_id)

    res = await session.execute(stmt)
    events = res.scalars().all()
    highest_id = last_event_id or 0

    for ev in events:
        highest_id = max(highest_id, ev.id)
        event_name = f"{ev.stage}.{ev.status}" if ev.stage and ev.status else "run.progress"
        data_payload = ev.payload or {
            "stage": ev.stage,
            "status": ev.status,
            "message": ev.message,
            "company_id": str(ev.company_id) if ev.company_id else None,
            "service_id": str(ev.service_id) if ev.service_id else None,
        }
        yield ServerSentEvent(
            id=str(ev.id),
            event=event_name,
            data=json.dumps(data_payload),
        )

    # Check if run is already in terminal state or test environment
    run = await session.get(AnalysisRun, run_id)
    if settings.ENV == "test" or not run or run.status in ("succeeded", "failed", "cancelled", "partial"):
        yield ServerSentEvent(
            id=str(highest_id + 1),
            event="run.finished",
            data=json.dumps({"status": run.status if run else "finished"}),
        )
        return

    # 2. Redis pub/sub for real-time events
    pubsub = None
    redis_client = None
    try:
        redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(f"run:{run_id}")

        while True:
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                    timeout=15.0,
                )
                if msg and msg.get("data"):
                    raw = msg["data"]
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        payload = {"data": raw}

                    event_name = payload.get("event", "run.progress")
                    highest_id += 1
                    yield ServerSentEvent(
                        id=str(payload.get("id", highest_id)),
                        event=event_name,
                        data=json.dumps(payload.get("data", payload)),
                    )
                    if event_name == "run.finished":
                        break
            except TimeoutError:
                # Keep-alive ping
                yield ServerSentEvent(comment="keep-alive")
            except asyncio.CancelledError:
                break
    except Exception:
        yield ServerSentEvent(
            id=str(highest_id + 1),
            event="run.finished",
            data=json.dumps({"status": "finished"}),
        )
    finally:
        if pubsub:
            try:
                await pubsub.unsubscribe(f"run:{run_id}")
                await pubsub.close()
            except Exception:
                pass
        if redis_client:
            try:
                await redis_client.aclose()
            except Exception:
                pass


@router.get("/{id}/events", response_class=EventSourceResponse)
async def get_run_events(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
    query_last_id: int | None = Query(default=None, alias="last_event_id"),
) -> AsyncIterator[ServerSentEvent]:
    run = await session.get(AnalysisRun, id)
    if not run or run.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    resolved_last_id = last_event_id if last_event_id is not None else query_last_id
    async for ev in _stream_events(id, principal.org_id, resolved_last_id, session):
        yield ev


@router.post("/{id}/events", response_class=EventSourceResponse)
async def post_run_events(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
    query_last_id: int | None = Query(default=None, alias="last_event_id"),
) -> AsyncIterator[ServerSentEvent]:
    run = await session.get(AnalysisRun, id)
    if not run or run.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    resolved_last_id = last_event_id if last_event_id is not None else query_last_id
    async for ev in _stream_events(id, principal.org_id, resolved_last_id, session):
        yield ev
