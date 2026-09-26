from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.activity.events import emit_event  # noqa: F401  (re-export)
from leadradar_core.modules.activity.models import DomainEvent
from leadradar_core.modules.activity.schemas import DomainEventOut
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/activity", tags=["activity"])


def _multi(values: list[str] | None) -> list[str]:
    """`?types=a&types=b` and `?types=a,b` are both accepted."""
    return [part.strip() for value in values or [] for part in value.split(",") if part.strip()]


@router.get("", response_model=list[DomainEventOut])
async def list_activity(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    company_id: Annotated[UUID | None, Query(description="Events about this company")] = None,
    types: Annotated[
        list[str] | None, Query(description="Event types, e.g. signal.detected (repeat or comma-separate)")
    ] = None,
    since: Annotated[datetime | None, Query(description="Only events created at or after this time")] = None,
) -> list[DomainEventOut]:
    stmt = select(DomainEvent).where(DomainEvent.org_id == principal.org_id)
    if company_id is not None:
        stmt = stmt.where(DomainEvent.payload["company_id"].astext == str(company_id))
    if event_types := _multi(types):
        stmt = stmt.where(DomainEvent.type.in_(event_types))
    if since is not None:
        stmt = stmt.where(DomainEvent.created_at >= since)
    res = await session.execute(stmt.order_by(DomainEvent.created_at.desc(), DomainEvent.id).limit(limit))
    return [DomainEventOut.model_validate(e) for e in res.scalars().all()]
