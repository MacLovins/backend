"""HubSpot add-on endpoints (SPEC core CO-A3): the status for the button and the manual push of a lead."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.activity import events
from leadradar_core.modules.config.models import Service, SignalQuestion
from leadradar_core.modules.integrations.hubspot import (
    Evidence,
    HubSpotClient,
    HubSpotError,
    LeadSnapshot,
    lead_link,
    reasons_from_payload,
)
from leadradar_core.modules.integrations.schemas import HubSpotPushIn, HubSpotPushOut, HubSpotStatus
from leadradar_core.modules.intelligence.models import Signal
from leadradar_core.modules.leads.models import LeadScore
from leadradar_core.settings import settings
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["integrations"])

MAX_EVIDENCE = 8
MAX_REASONS = 5


def hubspot_enabled() -> bool:
    return settings.FEATURE_HUBSPOT and bool(settings.HUBSPOT_PRIVATE_APP_TOKEN)


@router.get("/integrations/hubspot", response_model=HubSpotStatus)
async def hubspot_status(principal: Annotated[Principal, Depends(get_current_principal)]) -> HubSpotStatus:
    """The frontend shows the "Send to HubSpot" button only when this is enabled."""
    return HubSpotStatus(enabled=hubspot_enabled())


async def _snapshot(
    session: AsyncSession, company: Company, service_id: UUID | None
) -> tuple[LeadSnapshot, UUID]:
    stmt = select(LeadScore).where(
        LeadScore.company_id == company.id,
        LeadScore.org_id == company.org_id,
        LeadScore.is_current.is_(True),
    )
    if service_id:
        stmt = stmt.where(LeadScore.service_id == service_id)
    score = (await session.execute(stmt.order_by(LeadScore.priority.desc()).limit(1))).scalar_one_or_none()
    if score is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The lead has no score yet: analyze the company first")
    service = await session.get(Service, score.service_id)

    strength = case({"strong": 0, "moderate": 1}, value=Signal.strength, else_=2)
    rows = (
        await session.execute(
            select(Signal, SignalQuestion.text)
            .join(SignalQuestion, Signal.question_id == SignalQuestion.id)
            .where(
                Signal.company_id == company.id,
                Signal.service_id == score.service_id,
                Signal.status == "active",
            )
            .order_by(strength, Signal.confidence.desc(), Signal.event_date.desc().nulls_last())
            .limit(MAX_EVIDENCE)
        )
    ).all()
    snapshot = LeadSnapshot(
        name=company.name,
        domain=company.domain,
        employees=company.employees,
        city=company.hq_city,
        service=service.name if service else "",
        tier="disqualified" if score.disqualified else score.tier,
        priority=float(score.priority),
        fit=float(score.fit),
        intent=float(score.intent),
        risk=float(score.risk),
        why_now=reasons_from_payload(score.why_now)[:MAX_REASONS],
        evidence=[
            Evidence(
                quote=s.quote,
                source_name=s.source_name,
                url=s.url,
                event_date=str(s.event_date) if s.event_date else None,
                question=text,
            )
            for s, text in rows
        ],
        lead_url=lead_link(settings.PUBLIC_ORIGIN, company.id, score.service_id),
    )
    return snapshot, score.service_id


@router.post("/leads/{company_id}/push/hubspot", response_model=HubSpotPushOut)
async def push_to_hubspot(
    company_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    push_in: HubSpotPushIn | None = None,
) -> HubSpotPushOut:
    """Creates or updates the company in HubSpot with the LeadRadar score and adds a note with the evidence.

    409: the lead has no score yet. 503: the integration is not configured or HubSpot rate-limits us.
    502: HubSpot rejected the token or the request (the message says what).
    """
    if not hubspot_enabled():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "HubSpot integration is not configured")
    company = await session.get(Company, company_id)
    if not company or company.org_id != principal.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")

    snapshot, service_id = await _snapshot(session, company, push_in.service_id if push_in else None)
    known_id = company.hubspot_company_id
    await session.commit()  # release the DB connection during the HubSpot calls

    try:
        async with HubSpotClient(
            settings.HUBSPOT_PRIVATE_APP_TOKEN, base_url=settings.HUBSPOT_BASE_URL
        ) as hubspot:
            result = await hubspot.push_lead(snapshot, known_id)
    except HubSpotError as exc:
        if exc.status_code == 429:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "HubSpot rate limit, retry later"
            ) from exc
        if exc.status_code in (401, 403):
            detail = f"HubSpot rejected the token or its scopes: {exc.message}"
        else:
            detail = f"HubSpot error {exc.status_code}: {exc.message}"
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail) from exc

    synced_at = datetime.now(UTC)
    company = await session.get(Company, company_id)
    company.hubspot_company_id = result.company_id
    company.hubspot_synced_at = synced_at
    events.emit_event(
        session,
        principal.org_id,
        events.CRM_PUSHED,
        {
            "company_id": str(company_id),
            "company_name": company.name,
            "domain": company.domain,
            "crm": "hubspot",
            "created": result.created,
            "tier": snapshot.tier,
            "service_id": str(service_id),
            "hubspot_company_id": result.company_id,
            "record_url": result.record_url,
        },
    )
    await session.commit()
    return HubSpotPushOut(
        hubspot_company_id=result.company_id,
        created=result.created,
        note_id=result.note_id,
        record_url=result.record_url,
        synced_at=synced_at,
    )
