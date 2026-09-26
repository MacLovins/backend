import asyncio
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

import leadradar_ai as ai
import leadradar_parser as parser
from fastapi import APIRouter, Depends, HTTPException, Query, status
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
from leadradar_core.modules.intelligence.service import load_bundle
from leadradar_core.settings import settings
from leadradar_core.utils.domain import normalize_domain
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/discovery", tags=["discovery"])

COUNTRY_TO_ISO: dict[str, str] = {
    "GERMANY": "DE",
    "DEUTSCHLAND": "DE",
    "DE": "DE",
    "SWITZERLAND": "CH",
    "SCHWEIZ": "CH",
    "CH": "CH",
    "DENMARK": "DK",
    "DANMARK": "DK",
    "DK": "DK",
    "NETHERLANDS": "NL",
    "HOLLAND": "NL",
    "NEDERLAND": "NL",
    "NL": "NL",
    "FRANCE": "FR",
    "FRANKREICH": "FR",
    "FR": "FR",
    "UNITED STATES": "US",
    "USA": "US",
    "US": "US",
}


# Fallback pool when Wikidata is slow or unreachable. Firmographics only: Fit is computed from the ICP.
CURATED: list[dict] = [
    {
        "name": "Deutsche Bahn",
        "domain": "bahn.de",
        "country_code": "DE",
        "industry_ids": ["rail", "logistics"],
        "employees": 320000,
    },
    {
        "name": "E.ON",
        "domain": "eon.com",
        "country_code": "DE",
        "industry_ids": ["energy_utilities"],
        "employees": 72000,
    },
    {
        "name": "Siemens AG",
        "domain": "siemens.com",
        "country_code": "DE",
        "industry_ids": ["manufacturing"],
        "employees": 320000,
    },
    {
        "name": "Bayer AG",
        "domain": "bayer.com",
        "country_code": "DE",
        "industry_ids": ["pharma"],
        "employees": 94000,
    },
    {
        "name": "BMW Group",
        "domain": "bmwgroup.com",
        "country_code": "DE",
        "industry_ids": ["automotive"],
        "employees": 155000,
    },
    {
        "name": "SAP SE",
        "domain": "sap.com",
        "country_code": "DE",
        "industry_ids": ["software"],
        "employees": 108000,
    },
    {
        "name": "Kuehne + Nagel",
        "domain": "kuehne-nagel.com",
        "country_code": "CH",
        "industry_ids": ["logistics"],
        "employees": 79000,
    },
    {
        "name": "Nestlé",
        "domain": "nestle.com",
        "country_code": "CH",
        "industry_ids": ["food_beverage"],
        "employees": 270000,
    },
    {
        "name": "A.P. Moller - Maersk",
        "domain": "maersk.com",
        "country_code": "DK",
        "industry_ids": ["logistics"],
        "employees": 100000,
    },
    {
        "name": "DSV",
        "domain": "dsv.com",
        "country_code": "DK",
        "industry_ids": ["logistics"],
        "employees": 75000,
    },
    {
        "name": "ASML Holding",
        "domain": "asml.com",
        "country_code": "NL",
        "industry_ids": ["manufacturing"],
        "employees": 42000,
    },
    {
        "name": "Schneider Electric",
        "domain": "se.com",
        "country_code": "FR",
        "industry_ids": ["manufacturing", "energy_utilities"],
        "employees": 150000,
    },
    {
        "name": "TotalEnergies",
        "domain": "totalenergies.com",
        "country_code": "FR",
        "industry_ids": ["oil_gas", "energy_utilities"],
        "employees": 100000,
    },
    {
        "name": "Amazon",
        "domain": "amazon.com",
        "country_code": "US",
        "industry_ids": ["retail"],
        "employees": 1500000,
    },
]
COUNTRY_NAMES = {
    "DE": "Germany",
    "CH": "Switzerland",
    "DK": "Denmark",
    "NL": "Netherlands",
    "FR": "France",
    "US": "United States",
}
LIVE_DISCOVERY_TIMEOUT_S = 10.0


def _iso(country: str | None) -> str:
    norm = (country or "").strip().upper()
    return COUNTRY_TO_ISO.get(norm, norm)


def _fit(candidate: dict, icp: ai.ICPConfig) -> ai.FitResult:
    profile = ai.CompanyProfile(
        id=uuid5(NAMESPACE_URL, f"leadradar:discovery:{candidate['domain']}"),
        name=candidate["name"],
        domain=candidate["domain"],
        country_code=candidate.get("country_code"),
        industry_ids=candidate.get("industry_ids") or [],
        employees=candidate.get("employees"),
        revenue_eur=candidate.get("revenue_eur"),
    )
    return ai.fit_score(profile, icp)


def _reason(fit: ai.FitResult, source: str) -> str:
    matched = [d["label"] for d in fit.details if d["status"] in ("pass", "match")]
    text = "; ".join(matched[:3]) or "Passes the ICP filters"
    return f"{text} ({source})"


async def _service_icp(session: AsyncSession, org_id, service_id) -> ai.ICPConfig:
    stmt = select(Service).where(Service.org_id == org_id)
    stmt = stmt.where(Service.id == service_id) if service_id else stmt.where(Service.is_active.is_(True))
    service = (await session.execute(stmt.order_by(Service.slug).limit(1))).scalar_one_or_none()
    if service is None:
        if service_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
        return ai.ICPConfig()
    return (await load_bundle(session, service)).icp


async def _live_candidates(
    icp: ai.ICPConfig, countries: list[str], industry: str | None, limit: int
) -> list[dict]:
    """Wikidata candidates for the ICP; [] when the registry is slow or unreachable (and in tests: no network)."""
    if settings.ENV == "test":
        return []
    industries = [industry] if industry else list(icp.industries_any)
    if not industries:
        industries = [v for c in icp.nice_to_have if c.kind == "industry_in" for v in c.values]
    query = parser.DiscoveryQuery(
        countries=countries or list(icp.countries) or ["DE", "FR", "NL", "CH", "DK"],
        industries=[str(i) for i in industries],
        employees_min=icp.employees_min,
        employees_max=icp.employees_max,
        limit=max(limit * 3, 30),
    )
    try:
        async with parser.create_http_client() as http:
            found = await asyncio.wait_for(
                parser.discover(query, http=http), timeout=LIVE_DISCOVERY_TIMEOUT_S
            )
    except Exception as e:
        log.warning("discovery_live_failed", error=str(e) or type(e).__name__)
        return []
    return [c.model_dump() for c in found]


async def _ranked(
    session: AsyncSession, org_id, service_id, country: str | None, industry: str | None, limit: int
) -> list[DiscoveredCompany]:
    """Live and curated candidates, deduplicated by domain, scored with ai.fit_score against the service ICP.
    Candidates that fail a must-have (Fit 0) are dropped; the rest are sorted by Fit."""
    icp = await _service_icp(session, org_id, service_id)
    iso = _iso(country)
    tracked = set((await session.execute(select(Company.domain).where(Company.org_id == org_id))).scalars())
    pool = [(c, "Wikidata") for c in await _live_candidates(icp, [iso] if iso else [], industry, limit)]
    pool += [(c, "curated list") for c in CURATED]

    seen: set[str] = set()
    items: list[DiscoveredCompany] = []
    for candidate, source in pool:
        domain = normalize_domain(candidate["domain"]) or candidate["domain"]
        if domain in seen:
            continue
        seen.add(domain)
        if iso and candidate.get("country_code") != iso:
            continue
        if industry and industry not in (candidate.get("industry_ids") or []):
            continue
        fit = _fit(candidate, icp)
        if not fit.must_have_passed:
            continue
        items.append(
            DiscoveredCompany(
                name=candidate["name"],
                domain=domain,
                country_code=candidate.get("country_code"),
                industry_ids=candidate.get("industry_ids") or [],
                employees=candidate.get("employees"),
                fit_score=round(fit.fit, 1),
                already_tracked=domain in tracked,
                reason=_reason(fit, source),
            )
        )
    items.sort(key=lambda i: (-i.fit_score, i.name.casefold()))
    return items[:limit]


@router.post("/search", response_model=DiscoverySearchOut)
async def search_discovery(
    search_in: DiscoverySearchIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DiscoverySearchOut:
    items = await _ranked(
        session,
        principal.org_id,
        search_in.service_id,
        search_in.country,
        search_in.industry,
        search_in.limit,
    )
    return DiscoverySearchOut(items=items, total=len(items))


@router.get("", response_model=list[dict])
async def list_discovery_candidates(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    country: Annotated[str, Query()] = "",
    service_id: Annotated[UUID | None, Query()] = None,
) -> list[dict]:
    """Lightweight discovery list for the UI: Fit against the service ICP (default: first active service)."""
    items = await _ranked(session, principal.org_id, service_id, country, None, 50)
    return [
        {
            "id": i.domain,
            "name": i.name,
            "domain": i.domain,
            "country": COUNTRY_NAMES.get(i.country_code or "", i.country_code),
            "fit": round(i.fit_score),
            "reason": i.reason,
            "already_tracked": i.already_tracked,
        }
        for i in items
    ]


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
