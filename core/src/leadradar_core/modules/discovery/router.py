from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.accounts.schemas import CompanyOut
from leadradar_core.modules.config.models import Service
from leadradar_core.modules.discovery.schemas import (
    DiscoveredCompany,
    DiscoveryAcceptIn,
    DiscoverySearchIn,
    DiscoverySearchOut,
)
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
    service = await session.get(Service, search_in.service_id)
    if not service or service.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    # Query currently tracked companies in org to mark already_tracked
    existing_stmt = select(Company.domain).where(Company.org_id == principal.org_id)
    existing_res = await session.execute(existing_stmt)
    existing_domains = set(existing_res.scalars().all())

    # Curated candidate pool for discovery simulation / parser integration
    candidates = [
        {
            "name": "Kuehne + Nagel",
            "domain": "kuehne-nagel.com",
            "country_code": "CH",
            "industry_ids": ["logistics"],
            "employees": 79000,
            "fit_score": 88.5,
            "reason": "Large European logistics firm actively deploying enterprise automation.",
        },
        {
            "name": "DSV Panalpina",
            "domain": "dsv.com",
            "country_code": "DK",
            "industry_ids": ["logistics"],
            "employees": 75000,
            "fit_score": 85.0,
            "reason": "Major global transport provider undergoing supply chain digitization.",
        },
        {
            "name": "Schneider Electric",
            "domain": "se.com",
            "country_code": "FR",
            "industry_ids": ["manufacturing", "energy"],
            "employees": 135000,
            "fit_score": 91.0,
            "reason": "Energy management & industrial automation giant.",
        },
        {
            "name": "Bayer AG",
            "domain": "bayer.com",
            "country_code": "DE",
            "industry_ids": ["healthcare"],
            "employees": 100000,
            "fit_score": 79.5,
            "reason": "Pharma and life sciences enterprise with automated labs.",
        },
        {
            "name": "ASML",
            "domain": "asml.com",
            "country_code": "NL",
            "industry_ids": ["manufacturing"],
            "employees": 42000,
            "fit_score": 93.0,
            "reason": "Semiconductor equipment leader investing heavily in AI and process intelligence.",
        },
        {
            "name": "TotalEnergies",
            "domain": "totalenergies.com",
            "country_code": "FR",
            "industry_ids": ["energy"],
            "employees": 101000,
            "fit_score": 76.0,
            "reason": "Energy transition and automated monitoring initiatives.",
        },
    ]

    import asyncio

    import leadradar_parser as parser

    items: list[DiscoveredCompany] = []

    # Attempt live discovery via leadradar_parser (Wikidata SPARQL)
    try:
        query = parser.DiscoveryQuery(
            countries=[search_in.country.upper()] if search_in.country else ["DE", "FR", "NL", "CH", "DK"],
            industries=[search_in.industry] if search_in.industry else [],
            employees_min=500,
            limit=search_in.limit,
        )
        async with parser.create_http_client() as http:
            live_candidates = await asyncio.wait_for(parser.discover(query, http=http), timeout=3.0)
            for cand in live_candidates:
                domain = cand.domain
                already = domain in existing_domains
                items.append(
                    DiscoveredCompany(
                        name=cand.name,
                        domain=domain,
                        country_code=cand.country_code
                        or (search_in.country.upper() if search_in.country else "EU"),
                        industry_ids=cand.industry_ids
                        or ([search_in.industry] if search_in.industry else ["enterprise"]),
                        employees=cand.employees or 1500,
                        fit_score=85.0,
                        already_tracked=already,
                        reason="Discovered via public entity registry matching target profile.",
                    )
                )
    except Exception:
        pass

    # If live discovery didn't find enough or timed out, supplement with curated pool
    if len(items) < search_in.limit:
        for c in candidates:
            domain = c["domain"]
            if any(item.domain == domain for item in items):
                continue
            already = domain in existing_domains

            # Filters
            if search_in.country and c["country_code"] != search_in.country.upper():
                continue
            if search_in.industry and search_in.industry not in c["industry_ids"]:
                continue

            items.append(
                DiscoveredCompany(
                    name=c["name"],
                    domain=domain,
                    country_code=c["country_code"],
                    industry_ids=c["industry_ids"],
                    employees=c["employees"],
                    fit_score=c["fit_score"],
                    already_tracked=already,
                    reason=c["reason"],
                )
            )
            if len(items) >= search_in.limit:
                break

    return DiscoverySearchOut(items=items, total=len(items))


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
        notes=accept_in.notes,
        tags=accept_in.tags,
        origin="discovery",
        is_tracked=True,
    )
    session.add(company)
    await session.commit()
    await session.refresh(company)
    return CompanyOut.model_validate(company)
