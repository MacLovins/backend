"""Use cases around the AI engine: service bundles for the graph and instant rescoring (no LLM)."""

import time
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

import leadradar_ai as ai
from leadradar_core.adapters import mapping
from leadradar_core.adapters.store import tier_change_payload, upsert_derived
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.activity.router import emit_event
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
from sqlalchemy import not_, select, update
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


async def _rescore(
    session: AsyncSession, org_id: UUID, service: Service, company_ids: list[UUID] | None
) -> dict:
    """Recompute the current scores of the service (or of some companies) from stored signals, no LLM.

    One query for scores, companies and signals each; score_company is pure. Derived NIS2/DORA signals are
    re-synced from firmographics. Commits nothing."""
    started = time.perf_counter()
    bundle = await load_bundle(session, service)
    stmt = select(LeadScore).where(LeadScore.service_id == service.id, LeadScore.is_current.is_(True))
    if company_ids is not None:
        stmt = stmt.where(LeadScore.company_id.in_(company_ids))
    current = (await session.execute(stmt)).scalars().all()
    if not current:
        return {"rescored": 0, "tier_changes": 0, "duration_ms": int((time.perf_counter() - started) * 1000)}

    ids = [s.company_id for s in current]
    companies = {
        c.id: c for c in (await session.execute(select(Company).where(Company.id.in_(ids)))).scalars()
    }
    signals: dict[UUID, list[ai.StoredSignal]] = defaultdict(list)
    rows = await session.execute(
        select(Signal).where(
            Signal.service_id == service.id,
            Signal.company_id.in_(ids),
            Signal.status == "active",
            not_(Signal.flags.contains(["derived"])),
        )
    )
    for row in rows.scalars():
        signals[row.company_id].append(mapping.stored_signal(row))

    now = datetime.now(UTC)
    previous = {s.company_id: s for s in current}
    await session.execute(
        update(LeadScore).where(LeadScore.id.in_([s.id for s in current])).values(is_current=False)
    )
    tier_changes = 0
    for company_id, company in companies.items():
        profile = mapping.company_profile(company)
        derived = await upsert_derived(
            session, org_id, company_id, service.id, ai.derived_signals(profile, bundle, now)
        )
        score = ai.score_company(profile, bundle, signals[company_id] + derived, now, include_derived=False)
        session.add(mapping.lead_score_row(score, org_id))
        before = previous.get(company_id)
        if before is None or score.tier != before.tier:
            tier_changes += 1
            await emit_event(session, org_id, "lead.tier_changed", tier_change_payload(score, before))
    await session.flush()
    return {
        "rescored": len(companies),
        "tier_changes": tier_changes,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


async def rescore_service(session: AsyncSession, org_id: UUID, service_id: UUID) -> dict:
    """Recompute every already-scored company of the service from its stored signals (SPEC CO-05)."""
    service = await session.get(Service, service_id)
    if service is None or service.org_id != org_id:
        return {"rescored": 0, "tier_changes": 0, "duration_ms": 0}
    return await _rescore(session, org_id, service, None)


async def rescore_company(session: AsyncSession, org_id: UUID, service_id: UUID, company_id: UUID) -> dict:
    """Recompute one lead, e.g. after a user rejected or restored one of its signals."""
    service = await session.get(Service, service_id)
    if service is None or service.org_id != org_id:
        return {"rescored": 0, "tier_changes": 0, "duration_ms": 0}
    return await _rescore(session, org_id, service, [company_id])
