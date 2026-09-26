"""Services from leadradar-ai presets (ai/src/leadradar_ai/presets/*.yaml) — used by `lr seed` and
POST /presets/{key}/apply, so the database and the AI engine share one definition of each service."""

from decimal import Decimal
from uuid import UUID

import leadradar_ai as ai
from leadradar_core.modules.config.models import (
    DisqualificationRule,
    ICPProfile,
    ScoringProfile,
    Service,
    SignalQuestion,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class UnknownPreset(KeyError):
    pass


async def create_service_from_preset(session: AsyncSession, org_id: UUID, key: str) -> tuple[Service, bool]:
    """Idempotent by slug: returns (service, created). Commits nothing — the caller owns the transaction."""
    existing = (
        await session.execute(select(Service).where(Service.org_id == org_id, Service.slug == key))
    ).scalar_one_or_none()
    if existing:
        return existing, False
    if key not in ai.list_presets():
        raise UnknownPreset(key)
    preset = ai.load_preset(key)

    service = Service(
        org_id=org_id,
        name=preset.name,
        slug=preset.key,
        description=preset.description,
        value_proposition=preset.value_proposition,
        decision_makers=preset.decision_makers,
        is_active=True,
    )
    session.add(service)
    await session.flush()

    for q in preset.questions:
        session.add(
            SignalQuestion(
                org_id=org_id,
                service_id=service.id,
                key=q.key,
                text=q.text,
                category=q.category,
                polarity=q.polarity,
                weight=q.weight,
                source_types=sorted(q.source_types),
                recency_days=q.recency_days,
                keywords=q.keywords_seed,
                job_titles=q.job_titles,
                negative_terms=q.negative_terms,
                temperature=dict(q.temperature) or None,  # none: the category default
                keywords_status="pending",  # expand_question adds multilingual terms to the seed
                version=1,
                is_active=True,
            )
        )
    icp = preset.icp
    session.add(
        ICPProfile(
            org_id=org_id,
            service_id=service.id,
            countries=icp.countries,
            industries_any=icp.industries_any,
            employees_min=icp.employees_min,
            employees_max=icp.employees_max,
            revenue_min_eur=Decimal(icp.revenue_min_eur) if icp.revenue_min_eur is not None else None,
            nice_to_have={"criteria": [c.model_dump(mode="json") for c in icp.nice_to_have]},
            version=1,
        )
    )
    for r in preset.rules:
        session.add(
            DisqualificationRule(
                org_id=org_id,
                service_id=service.id,
                name=r.name,
                kind=r.kind,
                condition=r.condition,
                action=r.action,
                cap_value=Decimal(str(r.cap_value)) if r.cap_value is not None else None,
                is_active=True,
            )
        )
    session.add(
        ScoringProfile(
            org_id=org_id,
            service_id=service.id,
            version=1,
            params=default_scoring_params(preset.scoring),
            is_current=True,
        )
    )
    await session.flush()
    return service, True


def default_scoring_params(overrides: dict | None = None) -> dict:
    """The full parameter set, so admins see and edit every value (defaults: ARCHITECTURE §3.4)."""
    profile = ai.ScoringProfile(id=UUID(int=0), version=1, **(overrides or {}))
    return profile.model_dump(mode="json", exclude={"id", "version"})
