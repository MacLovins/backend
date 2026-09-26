"""Use cases around the AI engine: service bundles for the graph and instant rescoring (no LLM)."""

import time
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

import leadradar_ai as ai
from leadradar_core.adapters import mapping
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.config.models import (
    DisqualificationRule,
    ICPProfile,
    ScoringProfile,
    Service,
    SignalQuestion,
)
from leadradar_core.modules.config.presets import default_scoring_params
from leadradar_core.modules.intelligence.models import Signal
from leadradar_core.modules.leads.models import LeadScore
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def current_scoring_profile(session: AsyncSession, service: Service) -> ScoringProfile:
    """lead_score.scoring_profile_id needs a row: a service without a profile gets the defaults (v1)."""
    profile = (
        await session.execute(
            select(ScoringProfile)
            .where(ScoringProfile.service_id == service.id, ScoringProfile.is_current.is_(True))
            .order_by(ScoringProfile.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if profile is None:
        profile = ScoringProfile(
            org_id=service.org_id,
            service_id=service.id,
            version=1,
            params=default_scoring_params(),
            is_current=True,
        )
        session.add(profile)
        await session.flush()
    return profile


async def load_bundle(session: AsyncSession, service: Service) -> ai.ServiceBundle:
    questions = (
        (
            await session.execute(
                select(SignalQuestion)
                .where(SignalQuestion.service_id == service.id)
                # stable order = stable prompt = LLM cache hits on repeat analyses (creation order = preset order)
                .order_by(SignalQuestion.created_at, SignalQuestion.key)
            )
        )
        .scalars()
        .all()
    )
    icp = (
        await session.execute(select(ICPProfile).where(ICPProfile.service_id == service.id))
    ).scalar_one_or_none()
    rules = (
        (
            await session.execute(
                select(DisqualificationRule).where(DisqualificationRule.service_id == service.id)
            )
        )
        .scalars()
        .all()
    )
    profile = await current_scoring_profile(session, service)
    return mapping.service_bundle(service, list(questions), icp, list(rules), profile)


async def load_bundles(
    session: AsyncSession, org_id: UUID, service_ids: list[UUID]
) -> list[ai.ServiceBundle]:
    """Bundles for the requested services, or for every active service of the org when none are given."""
    stmt = select(Service).where(Service.org_id == org_id)
    stmt = stmt.where(Service.id.in_(service_ids)) if service_ids else stmt.where(Service.is_active.is_(True))
    services = (await session.execute(stmt.order_by(Service.slug))).scalars().all()
    return [await load_bundle(session, s) for s in services]


async def rescore_service(session: AsyncSession, org_id: UUID, service_id: UUID) -> dict:
    """Recompute every already-scored company of the service from its stored signals (SPEC CO-05).

    One query for companies, one for signals; score_company is pure. Commits nothing.
    """
    started = time.perf_counter()
    await session.flush()  # the config change being rescored is part of the same transaction
    service = await session.get(Service, service_id)
    if service is None or service.org_id != org_id:
        return {"rescored": 0, "tier_changes": 0, "duration_ms": 0}
    bundle = await load_bundle(session, service)
    current = (
        (
            await session.execute(
                select(LeadScore).where(LeadScore.service_id == service_id, LeadScore.is_current.is_(True))
            )
        )
        .scalars()
        .all()
    )
    if not current:
        return {"rescored": 0, "tier_changes": 0, "duration_ms": int((time.perf_counter() - started) * 1000)}

    company_ids = [s.company_id for s in current]
    companies = {
        c.id: c for c in (await session.execute(select(Company).where(Company.id.in_(company_ids)))).scalars()
    }
    signals: dict[UUID, list[ai.StoredSignal]] = defaultdict(list)
    rows = await session.execute(
        select(Signal).where(
            Signal.service_id == service_id, Signal.company_id.in_(company_ids), Signal.status == "active"
        )
    )
    for row in rows.scalars():
        signals[row.company_id].append(mapping.stored_signal(row))

    now = datetime.now(UTC)
    previous_tier = {s.company_id: s.tier for s in current}
    await session.execute(
        update(LeadScore)
        .where(LeadScore.service_id == service_id, LeadScore.is_current.is_(True))
        .values(is_current=False)
    )
    tier_changes = 0
    for company_id, company in companies.items():
        score = ai.score_company(mapping.company_profile(company), bundle, signals[company_id], now)
        session.add(mapping.lead_score_row(score, org_id))
        tier_changes += score.tier != previous_tier.get(company_id)
    await session.flush()
    return {
        "rescored": len(companies),
        "tier_changes": tier_changes,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


async def rescore_company(
    session: AsyncSession, org_id: UUID, company_id: UUID, service_id: UUID
) -> LeadScore | None:
    """Recompute one company for one service from its active signals (after user feedback). Commits nothing.

    Pending changes of the session (e.g. a signal just rejected by the user) are flushed first, so the new
    score sees them. Returns the new current lead_score row, or None when company or service is unknown.
    """
    await session.flush()
    service = await session.get(Service, service_id)
    company = await session.get(Company, company_id)
    if service is None or company is None or service.org_id != org_id or company.org_id != org_id:
        return None
    bundle = await load_bundle(session, service)
    rows = await session.execute(
        select(Signal).where(
            Signal.company_id == company_id, Signal.service_id == service_id, Signal.status == "active"
        )
    )
    signals = [mapping.stored_signal(r) for r in rows.scalars()]
    score = ai.score_company(mapping.company_profile(company), bundle, signals, datetime.now(UTC))
    await session.execute(
        update(LeadScore)
        .where(
            LeadScore.company_id == company_id,
            LeadScore.service_id == service_id,
            LeadScore.is_current.is_(True),
        )
        .values(is_current=False)
    )
    row = mapping.lead_score_row(score, org_id)
    session.add(row)
    await session.flush()
    return row
