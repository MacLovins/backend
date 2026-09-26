from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.activity import events as domain_events
from leadradar_core.modules.feedback.models import Feedback
from leadradar_core.modules.feedback.schemas import FeedbackIn, FeedbackOut, QualityMetricsOut
from leadradar_core.modules.intelligence.models import RejectedEvidence, Signal
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["feedback"])


@router.post(
    "/signals/{id}/feedback",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
)
async def feedback_signal(
    id: UUID,
    fb_in: FeedbackIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FeedbackOut:
    fb = Feedback(
        org_id=principal.org_id,
        user_id=principal.user_id,
        target_type="signal",
        target_id=id,
        service_id=fb_in.service_id,
        verdict=fb_in.verdict,
        reason=fb_in.reason,
    )
    session.add(fb)
    domain_events.feedback_created(session, fb)

    # When user marks signal as wrong, change status so it drops out of scoring
    if fb_in.verdict == "wrong":
        sig = await session.get(Signal, id)
        if sig and sig.org_id == principal.org_id:
            sig.status = "rejected_by_user"

    await session.commit()
    await session.refresh(fb)
    return FeedbackOut.model_validate(fb)


@router.post(
    "/leads/{company_id}/feedback",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
)
async def feedback_lead(
    company_id: UUID,
    fb_in: FeedbackIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FeedbackOut:
    fb = Feedback(
        org_id=principal.org_id,
        user_id=principal.user_id,
        target_type="lead",
        target_id=company_id,
        service_id=fb_in.service_id,
        verdict=fb_in.verdict,
        reason=fb_in.reason,
    )
    session.add(fb)
    domain_events.feedback_created(session, fb)
    await session.commit()
    await session.refresh(fb)
    return FeedbackOut.model_validate(fb)


@router.get("/quality", response_model=QualityMetricsOut)
async def get_quality(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service_id: Annotated[UUID | None, Query()] = None,
) -> QualityMetricsOut:
    stmt = select(Feedback).where(Feedback.org_id == principal.org_id)
    if service_id:
        stmt = stmt.where(Feedback.service_id == service_id)

    res = await session.execute(stmt)
    feedbacks = res.scalars().all()

    labeled = len(feedbacks)
    correct_count = sum(1 for f in feedbacks if f.verdict == "correct")
    precision = (correct_count / labeled) if labeled > 0 else 0.0

    # Verifier stats
    rej_stmt = select(RejectedEvidence.reason, func.count(RejectedEvidence.id)).where(
        RejectedEvidence.org_id == principal.org_id
    )
    if service_id:
        rej_stmt = rej_stmt.where(RejectedEvidence.service_id == service_id)
    rej_stmt = rej_stmt.group_by(RejectedEvidence.reason)
    rej_res = await session.execute(rej_stmt)
    rejected_reasons = {r: count for r, count in rej_res.all()}
    evidence_rejected_total = sum(rejected_reasons.values())

    sig_count_stmt = select(func.count(Signal.id)).where(Signal.org_id == principal.org_id)
    if service_id:
        sig_count_stmt = sig_count_stmt.where(Signal.service_id == service_id)
    total_signals = (await session.execute(sig_count_stmt)).scalar() or 0
    evidence_total = total_signals + evidence_rejected_total

    hallucination_rate = (
        round(rejected_reasons.get("quote_not_found", 0) / evidence_total, 4) if evidence_total > 0 else 0.0
    )

    return QualityMetricsOut(
        labeled=labeled,
        precision=round(precision, 2),
        by_category=[],
        by_source=[],
        verifier={
            "evidence_total": evidence_total,
            "rejected": rejected_reasons,
        },
        hallucination_rate=hallucination_rate,
    )
