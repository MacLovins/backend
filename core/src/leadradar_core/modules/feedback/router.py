from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.activity.router import emit_event
from leadradar_core.modules.feedback.models import Feedback
from leadradar_core.modules.feedback.schemas import FeedbackIn, FeedbackOut, QualityMetricsOut
from leadradar_core.modules.intelligence.models import RejectedEvidence, Signal
from leadradar_core.modules.intelligence.service import rescore_company
from sqlalchemy import func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["feedback"])


async def _upsert_feedback(
    session: AsyncSession, principal: Principal, target_type: str, target_id: UUID, fb_in: FeedbackIn
) -> Feedback:
    """One verdict per user and target (uq_feedback_user_target): voting again changes the verdict."""
    fb = (
        await session.execute(
            select(Feedback).where(
                Feedback.user_id == principal.user_id,
                Feedback.target_type == target_type,
                Feedback.target_id == target_id,
            )
        )
    ).scalar_one_or_none()
    if fb is None:
        fb = Feedback(
            org_id=principal.org_id,
            user_id=principal.user_id,
            target_type=target_type,
            target_id=target_id,
            service_id=fb_in.service_id,
        )
        session.add(fb)
    fb.verdict, fb.reason = fb_in.verdict, fb_in.reason
    return fb


@router.post(
    "/signals/{id}/feedback",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
)
@router.post(
    "/companies/{company_id}/signals/{id}/feedback",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,  # the frontend's path for the same action
)
async def feedback_signal(
    id: UUID,
    fb_in: FeedbackIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FeedbackOut:
    """An incorrect or irrelevant signal drops out of scoring and the lead is rescored at once; correct
    restores it. The verdict also feeds GET /quality."""
    sig = await session.get(Signal, id)
    if sig is None or sig.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signal not found")
    fb = await _upsert_feedback(session, principal, "signal", id, fb_in)

    new_status = "active" if fb_in.verdict == "correct" else "rejected_by_user"
    rescored = None
    if sig.status in ("active", "rejected_by_user") and sig.status != new_status:
        sig.status = new_status
        await session.flush()
        rescored = await rescore_company(session, principal.org_id, sig.service_id, sig.company_id)
    await emit_event(
        session,
        principal.org_id,
        "signal.feedback",
        {
            "signal_id": str(id),
            "company_id": str(sig.company_id),
            "service_id": str(sig.service_id),
            "verdict": fb_in.verdict,
            "rescored": bool(rescored and rescored["rescored"]),
        },
    )
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
    fb = await _upsert_feedback(session, principal, "lead", company_id, fb_in)
    await session.commit()
    await session.refresh(fb)
    return FeedbackOut.model_validate(fb)


def _precision_rows(rows: list[tuple[str, str]], key_name: str) -> list[dict]:
    """rows: (group, verdict) → [{key_name, labeled, precision}]; irrelevant is not a precision error."""
    groups: dict[str, list[str]] = {}
    for group, verdict in rows:
        groups.setdefault(group, []).append(verdict)
    out = []
    for group, verdicts in sorted(groups.items()):
        judged = [v for v in verdicts if v in ("correct", "incorrect")]
        precision = round(judged.count("correct") / len(judged), 2) if judged else None
        out.append({key_name: group, "labeled": len(verdicts), "precision": precision})
    return out


@router.get("/quality", response_model=QualityMetricsOut)
async def get_quality(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service_id: Annotated[UUID | None, Query()] = None,
) -> QualityMetricsOut:
    """Precision of extracted signals from user verdicts (overall, by category, by source type) and the
    verifier's rejection statistics (SPEC CO-15)."""
    stmt = (
        select(Signal.category, Signal.source_type, Feedback.verdict)
        .join(Signal, Signal.id == Feedback.target_id)
        .where(Feedback.org_id == principal.org_id, Feedback.target_type == "signal")
    )
    if service_id:
        stmt = stmt.where(Feedback.service_id == service_id)
    labeled_rows = (await session.execute(stmt)).all()
    verdicts = [v for _, _, v in labeled_rows]
    judged = [v for v in verdicts if v in ("correct", "incorrect")]
    precision = judged.count("correct") / len(judged) if judged else 0.0

    rej_stmt = select(RejectedEvidence.reason, func.count(RejectedEvidence.id)).where(
        RejectedEvidence.org_id == principal.org_id
    )
    if service_id:
        rej_stmt = rej_stmt.where(RejectedEvidence.service_id == service_id)
    rejected_reasons = dict((await session.execute(rej_stmt.group_by(RejectedEvidence.reason))).all())
    evidence_rejected_total = sum(rejected_reasons.values())

    # every stored extraction counts (active, superseded, rejected by user); derived signals are not LLM output
    sig_count_stmt = select(func.count(Signal.id)).where(
        Signal.org_id == principal.org_id, not_(Signal.flags.contains(["derived"]))
    )
    if service_id:
        sig_count_stmt = sig_count_stmt.where(Signal.service_id == service_id)
    total_signals = (await session.execute(sig_count_stmt)).scalar() or 0
    evidence_total = total_signals + evidence_rejected_total

    hallucination_rate = (
        round(rejected_reasons.get("quote_not_found", 0) / evidence_total, 4) if evidence_total > 0 else 0.0
    )
    return QualityMetricsOut(
        labeled=len(verdicts),
        precision=round(precision, 2),
        by_category=_precision_rows([(c, v) for c, _, v in labeled_rows], "category"),
        by_source=_precision_rows([(st, v) for _, st, v in labeled_rows], "source_type"),
        verifier={"evidence_total": evidence_total, "rejected": rejected_reasons},
        hallucination_rate=hallucination_rate,
    )
