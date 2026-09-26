from collections import defaultdict
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.activity import events as domain_events
from leadradar_core.modules.config.models import Service
from leadradar_core.modules.feedback.models import Feedback
from leadradar_core.modules.feedback.schemas import (
    CategoryQuality,
    FeedbackOut,
    FeedbackWithdrawOut,
    LeadFeedbackIn,
    LeadFeedbackStats,
    QualityMetricsOut,
    SignalFeedbackIn,
    SourceQuality,
)
from leadradar_core.modules.intelligence.models import Document, RejectedEvidence, Signal
from leadradar_core.modules.intelligence.service import rescore_company
from leadradar_core.modules.leads.models import LeadScore
from leadradar_core.modules.leads.schemas import ScoreSummary
from sqlalchemy import and_, delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["feedback"])

# statuses a user verdict may switch between; superseded signals belong to an older run and stay as they are
_LIVE_STATUSES = ("active", "rejected_by_user")


async def _get_signal(session: AsyncSession, signal_id: UUID, org_id: UUID) -> Signal:
    sig = await session.get(Signal, signal_id)
    if sig is None or sig.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signal not found")
    return sig


async def _upsert_feedback(
    session: AsyncSession,
    principal: Principal,
    target_type: str,
    target_id: UUID,
    service_id: UUID,
    verdict: str,
    reason: str | None,
) -> Feedback:
    """One vote per user and target (uq_feedback_user_target): a repeated vote replaces the previous one."""
    stmt = (
        insert(Feedback)
        .values(
            id=uuid4(),
            org_id=principal.org_id,
            user_id=principal.user_id,
            target_type=target_type,
            target_id=target_id,
            service_id=service_id,
            verdict=verdict,
            reason=reason,
        )
        .on_conflict_do_update(
            constraint="uq_feedback_user_target",
            set_={"verdict": verdict, "reason": reason, "service_id": service_id, "updated_at": func.now()},
        )
        .returning(Feedback)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one()


def _same_evidence(sig: Signal):
    """Rows of the company and service that carry the same evidence (older runs included)."""
    key = Signal.evidence_key == sig.evidence_key if sig.evidence_key else Signal.id == sig.id
    return and_(Signal.company_id == sig.company_id, Signal.service_id == sig.service_id, key)


async def _sync_rejection(session: AsyncSession, sig: Signal) -> bool:
    """Signal status follows the votes: any "incorrect" vote on this evidence keeps it out of scoring.

    Returns True when a status changed (the company must then be rescored).
    """
    await session.flush()
    rejected = (
        await session.execute(
            select(
                exists().where(
                    Feedback.target_type == "signal",
                    Feedback.verdict == "incorrect",
                    Feedback.target_id.in_(select(Signal.id).where(_same_evidence(sig))),
                )
            )
        )
    ).scalar_one()
    new_status = "rejected_by_user" if rejected else "active"
    targets = (
        (await session.execute(select(Signal).where(_same_evidence(sig), Signal.status.in_(_LIVE_STATUSES))))
        .scalars()
        .all()
    )
    changed = False
    for target in targets:
        if target.status != new_status:
            target.status = new_status
            changed = True
    return changed


def _score_summary(row: LeadScore | None) -> ScoreSummary | None:
    return ScoreSummary.model_validate(row) if row is not None else None


async def _current_score(session: AsyncSession, company_id: UUID, service_id: UUID) -> LeadScore | None:
    return (
        await session.execute(
            select(LeadScore).where(
                LeadScore.company_id == company_id,
                LeadScore.service_id == service_id,
                LeadScore.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()


async def _score_after_vote(session: AsyncSession, org_id: UUID, sig: Signal) -> ScoreSummary | None:
    if await _sync_rejection(session, sig):
        # pure ai.score_company over stored signals, same transaction as the vote
        return _score_summary(await rescore_company(session, org_id, sig.company_id, sig.service_id))
    return _score_summary(await _current_score(session, sig.company_id, sig.service_id))


@router.post(
    "/signals/{id}/feedback",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
)
async def feedback_signal(
    id: UUID,
    fb_in: SignalFeedbackIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FeedbackOut:
    sig = await _get_signal(session, id, principal.org_id)
    if fb_in.service_id is not None and fb_in.service_id != sig.service_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="service_id does not match the signal's service",
        )
    fb = await _upsert_feedback(
        session, principal, "signal", sig.id, sig.service_id, fb_in.verdict, fb_in.reason
    )
    score = await _score_after_vote(session, principal.org_id, sig)
    domain_events.feedback_created(session, fb)
    out = FeedbackOut.model_validate(fb)
    out.score = score
    await session.commit()
    return out


@router.delete("/signals/{id}/feedback", response_model=FeedbackWithdrawOut)
async def withdraw_signal_feedback(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FeedbackWithdrawOut:
    """Withdraw the current user's vote on the signal; the signal and the score follow the remaining votes."""
    sig = await _get_signal(session, id, principal.org_id)
    result = await session.execute(
        delete(Feedback)
        .where(
            Feedback.user_id == principal.user_id,
            Feedback.target_type == "signal",
            Feedback.target_id == sig.id,
        )
        .returning(Feedback.id)
    )
    withdrawn = result.first() is not None
    score = await _score_after_vote(session, principal.org_id, sig)
    await session.commit()
    return FeedbackWithdrawOut(signal_id=sig.id, withdrawn=withdrawn, score=score)


@router.post(
    "/leads/{company_id}/feedback",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
)
async def feedback_lead(
    company_id: UUID,
    fb_in: LeadFeedbackIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FeedbackOut:
    company = await session.get(Company, company_id)
    if company is None or company.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    service = await session.get(Service, fb_in.service_id)
    if service is None or service.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    fb = await _upsert_feedback(
        session, principal, "lead", company_id, fb_in.service_id, fb_in.verdict, fb_in.reason
    )
    domain_events.feedback_created(session, fb)
    out = FeedbackOut.model_validate(fb)
    out.score = _score_summary(await _current_score(session, company_id, fb_in.service_id))
    await session.commit()
    return out


def _precision(correct: int, labeled: int) -> float:
    return round(correct / labeled, 2) if labeled else 0.0


@router.get("/quality", response_model=QualityMetricsOut)
async def get_quality(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service_id: Annotated[UUID | None, Query()] = None,
) -> QualityMetricsOut:
    # --- signal votes → precision, by category (signal) and by source (document, signal as fallback)
    source_type = func.coalesce(Document.source_type, Signal.source_type)
    sig_stmt = (
        select(Signal.category, source_type, Feedback.verdict, func.count(Feedback.id))
        .select_from(Feedback)
        .join(Signal, and_(Feedback.target_type == "signal", Feedback.target_id == Signal.id))
        .outerjoin(Document, Signal.document_id == Document.id)
        .where(Feedback.org_id == principal.org_id)
        .group_by(Signal.category, source_type, Feedback.verdict)
    )
    if service_id:
        sig_stmt = sig_stmt.where(Feedback.service_id == service_id)

    labeled = correct = 0
    by_category: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [labeled, correct]
    by_source: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for category, source, verdict, count in (await session.execute(sig_stmt)).all():
        hit = count if verdict == "correct" else 0
        labeled += count
        correct += hit
        for bucket in (by_category[category], by_source[source]):
            bucket[0] += count
            bucket[1] += hit

    # --- lead votes are reported separately and never mixed into precision
    lead_stmt = (
        select(Feedback.verdict, func.count(Feedback.id))
        .where(Feedback.org_id == principal.org_id, Feedback.target_type == "lead")
        .group_by(Feedback.verdict)
    )
    if service_id:
        lead_stmt = lead_stmt.where(Feedback.service_id == service_id)
    lead_counts = dict((await session.execute(lead_stmt)).all())

    # --- verifier stats
    rej_stmt = select(RejectedEvidence.reason, func.count(RejectedEvidence.id)).where(
        RejectedEvidence.org_id == principal.org_id
    )
    if service_id:
        rej_stmt = rej_stmt.where(RejectedEvidence.service_id == service_id)
    rej_stmt = rej_stmt.group_by(RejectedEvidence.reason)
    rejected_reasons = dict((await session.execute(rej_stmt)).all())
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
        precision=_precision(correct, labeled),
        by_category=sorted(
            (
                CategoryQuality(category=k, labeled=n, precision=_precision(c, n))
                for k, (n, c) in by_category.items()
            ),
            key=lambda b: (-b.labeled, b.category),
        ),
        by_source=sorted(
            (
                SourceQuality(source_type=k, labeled=n, precision=_precision(c, n))
                for k, (n, c) in by_source.items()
            ),
            key=lambda b: (-b.labeled, b.source_type),
        ),
        leads=LeadFeedbackStats(
            labeled=sum(lead_counts.values()),
            good_fit=lead_counts.get("good_fit", 0),
            bad_fit=lead_counts.get("bad_fit", 0),
        ),
        verifier={
            "evidence_total": evidence_total,
            "rejected": rejected_reasons,
        },
        hallucination_rate=hallucination_rate,
    )
