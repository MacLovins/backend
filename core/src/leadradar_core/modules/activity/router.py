from typing import Annotated

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


@router.get("", response_model=list[DomainEventOut])
async def list_activity(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = Query(default=50, ge=1, le=100),
) -> list[DomainEventOut]:
    stmt = (
        select(DomainEvent)
        .where(DomainEvent.org_id == principal.org_id)
        .order_by(DomainEvent.created_at.desc())
        .limit(limit)
    )
    res = await session.execute(stmt)
    return [DomainEventOut.model_validate(e) for e in res.scalars().all()]
