from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.accounts.schemas import CompanyOut
from leadradar_core.modules.discovery import service as discovery_service
from leadradar_core.modules.discovery.schemas import (
    DiscoveryAcceptIn,
    DiscoverySearchIn,
    DiscoverySearchOut,
)
from leadradar_core.settings import settings
from leadradar_core.utils.domain import normalize_domain
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/discovery", tags=["discovery"])


@router.post("/search", response_model=DiscoverySearchOut)
async def search_discovery(
    search_in: DiscoverySearchIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DiscoverySearchOut:
    """Find companies matching the service ICP (Wikidata via parser), ranked by Fit (ai.fit_score)."""
    return await discovery_service.search(
        session, principal.org_id, search_in, timeout_s=settings.DISCOVERY_TIMEOUT_S
    )


@router.post("/accept", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
async def accept_discovery(
    accept_in: DiscoveryAcceptIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CompanyOut:
    normalized = normalize_domain(accept_in.domain)
    if not normalized:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid domain")

    stmt = select(Company).where(
        Company.org_id == principal.org_id,
        Company.domain == normalized,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        existing.is_tracked = True
        await session.commit()
        await session.refresh(existing)
        return CompanyOut.model_validate(existing)

    company = Company(
        org_id=principal.org_id,
        name=accept_in.name,
        domain=normalized,
        country_code=accept_in.country_code,
        industry_ids=accept_in.industry_ids,
        employees=accept_in.employees,
        revenue_eur=accept_in.revenue_eur,
        wikidata_qid=accept_in.wikidata_qid,
        lei=accept_in.lei,
        crunchbase_id=accept_in.crunchbase_id,
        homepage_url=f"https://{normalized}",
        notes=accept_in.notes,
        tags=accept_in.tags,
        origin="discovery",
        is_tracked=True,
    )
    session.add(company)
    await session.commit()
    await session.refresh(company)
    return CompanyOut.model_validate(company)
