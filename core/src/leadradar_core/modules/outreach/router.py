"""Outreach drafts (CO-A1), asynchronous: POST enqueues a worker job (202), GET polls its result."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.outreach import service
from leadradar_core.modules.outreach.schemas import OutreachGenerateIn, OutreachJobOut
from leadradar_core.settings import settings
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/leads", tags=["leads"])


@router.post(
    "/{company_id}/outreach",
    response_model=OutreachJobOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Outreach is disabled (APP_FEATURE_OUTREACH)"}
    },
)
async def generate_lead_outreach(
    company_id: UUID,
    generate_in: OutreachGenerateIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> OutreachJobOut:
    if not settings.FEATURE_OUTREACH:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Outreach is disabled")
    job = await service.create_job(session, principal.org_id, principal.user_id, company_id, generate_in)
    return service.job_out(job)


@router.get("/{company_id}/outreach/{job_id}", response_model=OutreachJobOut)
async def get_lead_outreach(
    company_id: UUID,
    job_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> OutreachJobOut:
    return service.job_out(await service.get_job(session, principal.org_id, company_id, job_id))
