"""Discovery use case (CO-08, ARCHITECTURE §4.5 d): ICP → parser.discover → ai.fit_score → ranked candidates."""

import asyncio
from uuid import NAMESPACE_URL, UUID, uuid5

import leadradar_ai as ai
import leadradar_parser as parser
import structlog
from fastapi import status
from leadradar_core.adapters.mapping import icp_config
from leadradar_core.errors import DomainException, NotFoundException, UnprocessableException
from leadradar_core.modules.accounts.importer import match_country, match_industry
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.config.models import ICPProfile, Service
from leadradar_core.modules.discovery.schemas import DiscoveredCompany, DiscoverySearchIn, DiscoverySearchOut
from leadradar_core.utils.domain import normalize_domain
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

_CRITERION_NAMES = {
    "countries": "country",
    "country_in": "country",
    "industries_any": "industry",
    "industry_in": "industry",
    "employees_min": "size",
    "employees_max": "size",
    "employees_between": "size",
    "revenue_min_eur": "revenue",
    "revenue_at_least": "revenue",
    "tag_in": "tags",
}


def _normalize_countries(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        code = match_country(value)
        if code and code not in out:
            out.append(code)
    return out


def _normalize_industries(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        industry_id = match_industry(value)
        if industry_id and industry_id not in out:
            out.append(industry_id)
    return out


def build_discovery_query(icp: ai.ICPConfig, search_in: DiscoverySearchIn) -> parser.DiscoveryQuery:
    """Service ICP + request overrides. Industries fall back to the ICP's nice-to-have industry criteria."""
    countries = list(icp.countries)
    if search_in.countries:
        countries = list(search_in.countries)
    elif search_in.country:
        countries = [search_in.country]

    industries = list(icp.industries_any)
    if search_in.industries:
        industries = list(search_in.industries)
    elif search_in.industry:
        industries = [search_in.industry]
    elif not industries:
        industries = [str(v) for c in icp.nice_to_have if c.kind == "industry_in" for v in c.values]

    countries = _normalize_countries(countries)
    industries = _normalize_industries(industries)
    if not countries or not industries:
        raise UnprocessableException(
            "discovery_query_incomplete",
            "Discovery needs at least one country and one industry: set them in the service ICP "
            "or pass countries / industries in the request",
            {"countries": countries, "industries": industries},
        )
    employees_min = search_in.employees_min if search_in.employees_min is not None else icp.employees_min
    employees_max = search_in.employees_max if search_in.employees_max is not None else icp.employees_max
    return parser.DiscoveryQuery(
        countries=countries,
        industries=industries,
        employees_min=employees_min,
        employees_max=employees_max,
        limit=search_in.limit,
    )


def _reason(result: ai.FitResult) -> str:
    matched: list[str] = []
    failed: list[str] = []
    for d in result.details:
        name = _CRITERION_NAMES.get(str(d.get("criterion")), str(d.get("criterion")))
        if d.get("status") in ("pass", "match") and name not in matched:
            matched.append(name)
        elif d.get("status") in ("fail", "no_match") and name not in failed:
            failed.append(name)
    parts = []
    if matched:
        parts.append("Matches ICP on " + ", ".join(matched))
    if failed:
        parts.append(("fails " if result.must_have_passed is False else "misses ") + ", ".join(failed))
    if result.data_gaps:
        parts.append("unknown " + ", ".join(result.data_gaps))
    return "; ".join(parts) or "Matches the service ICP"


def score_candidate(
    candidate: parser.CompanyCandidate, icp: ai.ICPConfig, tracked: set[str]
) -> DiscoveredCompany | None:
    domain = normalize_domain(candidate.domain)
    if not domain or not candidate.name.strip():
        return None
    try:
        profile = ai.CompanyProfile(
            id=uuid5(NAMESPACE_URL, f"leadradar:discovery:{domain}"),
            name=candidate.name,
            domain=domain,
            country_code=candidate.country_code,
            industry_ids=list(candidate.industry_ids),
            employees=candidate.employees,
            revenue_eur=candidate.revenue_eur,
        )
    except ValidationError as e:
        log.info("discovery_candidate_invalid", domain=domain, error=str(e))
        return None
    fit = ai.fit_score(profile, icp)
    return DiscoveredCompany(
        name=candidate.name,
        domain=domain,
        country_code=candidate.country_code,
        industry_ids=list(candidate.industry_ids),
        employees=candidate.employees,
        revenue_eur=candidate.revenue_eur,
        wikidata_qid=candidate.wikidata_qid,
        lei=candidate.lei,
        crunchbase_id=candidate.crunchbase_id,
        fit_score=round(fit.fit, 1),
        must_have_passed=fit.must_have_passed,
        fit_details=fit.details,
        data_gaps=fit.data_gaps,
        already_tracked=domain in tracked,
        reason=_reason(fit),
    )


async def search(
    session: AsyncSession, org_id: UUID, search_in: DiscoverySearchIn, *, timeout_s: float
) -> DiscoverySearchOut:
    service = await session.get(Service, search_in.service_id)
    if not service or service.org_id != org_id:
        raise NotFoundException("Service not found")
    icp_row = (
        await session.execute(select(ICPProfile).where(ICPProfile.service_id == service.id))
    ).scalar_one_or_none()
    icp = icp_config(icp_row)
    query = build_discovery_query(icp, search_in)
    tracked = set(
        (
            await session.execute(
                select(Company.domain).where(Company.org_id == org_id, Company.is_tracked.is_(True))
            )
        ).scalars()
    )

    try:
        candidates = await asyncio.wait_for(parser.discover(query), timeout=timeout_s)
    except TimeoutError as e:
        raise DomainException(
            "discovery_timeout",
            f"Company search did not finish within {timeout_s:.0f} s; narrow the query and retry",
            {"timeout_s": timeout_s},
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        ) from e
    except Exception as e:  # network errors of the public registry
        log.warning("discovery_failed", error=str(e))
        raise DomainException(
            "discovery_unavailable",
            "Company search source is unavailable, retry later",
            status_code=status.HTTP_502_BAD_GATEWAY,
        ) from e

    items: dict[str, DiscoveredCompany] = {}
    for candidate in candidates:
        scored = score_candidate(candidate, icp, tracked)
        if scored and scored.domain not in items:
            items[scored.domain] = scored
    ranked = sorted(items.values(), key=lambda c: (-c.fit_score, -(c.employees or 0), c.name.lower()))
    ranked = ranked[: search_in.limit]
    return DiscoverySearchOut(
        items=ranked,
        total=len(ranked),
        query={
            "countries": query.countries,
            "industries": query.industries,
            "employees_min": query.employees_min,
            "employees_max": query.employees_max,
            "limit": query.limit,
        },
    )
