"""Domain events written to the outbox (`core.domain_event`) in the caller's transaction (SPEC core CO-17).

Each helper is a one-line call at the place where the fact happens; it only adds rows to the session, the
caller's commit makes the event durable together with the fact. The dispatcher delivers them to consumers.

Event types and payloads (the /activity feed and the add-ons read them):
- signal.detected     a verified signal not seen before for the company, service and question
- lead.tier_changed   a new current lead score whose tier differs from the previous current one (or first score)
- run.finished        an analysis run reached a terminal status
- feedback.created    a user voted on a signal or a lead
- crm.pushed          a lead was pushed to a CRM by hand (POST /leads/{id}/push/hubspot)
"""

from typing import Any
from uuid import UUID, uuid4

import leadradar_ai as ai
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.activity.models import DomainEvent
from leadradar_core.modules.config.models import Service, SignalQuestion
from leadradar_core.modules.feedback.models import Feedback
from leadradar_core.modules.intelligence.models import Signal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

SIGNAL_DETECTED = "signal.detected"
LEAD_TIER_CHANGED = "lead.tier_changed"
RUN_FINISHED = "run.finished"
FEEDBACK_CREATED = "feedback.created"
CRM_PUSHED = "crm.pushed"

WHY_NOW_LIMIT = 3


def emit_event(session: AsyncSession, org_id: UUID, event_type: str, payload: dict[str, Any]) -> DomainEvent:
    """Adds an outbox row to the session; it is committed (or rolled back) with the caller's transaction."""
    ev = DomainEvent(id=uuid4(), org_id=org_id, type=event_type, payload=payload)
    session.add(ev)
    return ev


def _num(value: Any) -> float | None:
    return None if value is None else round(float(value), 2)


def _quote_key(question_id: UUID, quote: str) -> tuple[UUID, str]:
    return question_id, " ".join(quote.lower().split())


async def signals_detected(session: AsyncSession, signals: list[Signal]) -> list[DomainEvent]:
    """signal.detected for each new signal among freshly added rows (same company and service).

    A signal is new when no earlier row of the company and service (active, superseded or rejected by a user)
    has the same question and the same quote: re-extraction of known evidence stays silent.
    """
    if not signals:
        return []
    first = signals[0]
    new_ids = {s.id for s in signals if s.id is not None}
    stmt = select(Signal.id, Signal.question_id, Signal.quote).where(
        Signal.company_id == first.company_id, Signal.service_id == first.service_id
    )
    seen = {_quote_key(q, text) for sid, q, text in (await session.execute(stmt)).all() if sid not in new_ids}
    fresh = []
    for s in signals:
        key = _quote_key(s.question_id, s.quote)
        if key not in seen:
            seen.add(key)
            fresh.append(s)
    if not fresh:
        return []
    company = await session.get(Company, first.company_id)
    service = await session.get(Service, first.service_id)
    weights = dict(
        (
            await session.execute(
                select(SignalQuestion.id, SignalQuestion.weight).where(
                    SignalQuestion.id.in_({s.question_id for s in fresh})
                )
            )
        ).all()
    )
    events = []
    for s in fresh:
        if s.id is None:
            s.id = uuid4()
        payload = {
            "signal_id": str(s.id),
            "company_id": str(s.company_id),
            "company_name": company.name if company else None,
            "domain": company.domain if company else None,
            "service_id": str(s.service_id),
            "service_name": service.name if service else None,
            "run_id": str(s.run_id) if s.run_id else None,
            "question_id": str(s.question_id),
            "question_key": s.question_key,
            "weight": weights.get(s.question_id, "medium"),
            "category": s.category,
            "polarity": s.polarity,
            "strength": s.strength,
            "confidence": _num(s.confidence),
            "summary": s.summary,
            "quote": s.quote,
            "url": s.url,
            "source_name": s.source_name,
        }
        events.append(emit_event(session, s.org_id, SIGNAL_DETECTED, payload))
    return events


async def lead_tier_changed(
    session: AsyncSession,
    org_id: UUID,
    score: ai.LeadScore,
    tier_before: str | None,
    *,
    company: Company | None = None,
    service: Service | None = None,
) -> DomainEvent | None:
    """lead.tier_changed when the new current score's tier differs from the previous current one."""
    if score.tier == tier_before:
        return None
    company = company or await session.get(Company, score.company_id)
    service = service or await session.get(Service, score.service_id)
    payload = {
        "company_id": str(score.company_id),
        "company_name": company.name if company else None,
        "domain": company.domain if company else None,
        "service_id": str(score.service_id),
        "service_name": service.name if service else None,
        "tier_before": tier_before,
        "tier_after": score.tier,
        "priority": _num(score.priority),
        "fit": _num(score.fit),
        "intent": _num(score.intent),
        "risk": _num(score.risk),
        "disqualified": score.disqualified,
        "why_now": [r.model_dump(mode="json") for r in score.why_now[:WHY_NOW_LIMIT]],
    }
    return emit_event(session, org_id, LEAD_TIER_CHANGED, payload)


def run_finished(
    session: AsyncSession, org_id: UUID, run_id: UUID, status: str, progress: dict
) -> DomainEvent:
    payload = {"run_id": str(run_id), "status": status, "progress": progress}
    return emit_event(session, org_id, RUN_FINISHED, payload)


def feedback_created(session: AsyncSession, fb: Feedback) -> DomainEvent:
    if fb.id is None:
        fb.id = uuid4()
    payload = {
        "feedback_id": str(fb.id),
        "user_id": str(fb.user_id),
        "target_type": fb.target_type,
        "target_id": str(fb.target_id),
        "service_id": str(fb.service_id),
        "verdict": fb.verdict,
        "reason": fb.reason,
    }
    return emit_event(session, fb.org_id, FEEDBACK_CREATED, payload)
