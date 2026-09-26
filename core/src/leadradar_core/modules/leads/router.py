import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.accounts.schemas import CompanyOut
from leadradar_core.modules.config.models import Service, SignalQuestion
from leadradar_core.modules.intelligence.models import Document, Signal
from leadradar_core.modules.leads.models import LeadScore
from leadradar_core.modules.leads.schemas import (
    LeadDetail,
    LeadListItem,
    QuestionSignals,
    ScoreSummary,
    SignalItem,
)
from leadradar_core.pagination import PaginatedResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/leads", tags=["leads"])


@router.get("", response_model=PaginatedResponse[LeadListItem])
async def list_leads(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service_id: Annotated[UUID | None, Query()] = None,
    tier: Annotated[str | None, Query()] = None,
    country: Annotated[str | None, Query()] = None,
    industry: Annotated[str | None, Query()] = None,
    min_priority: Annotated[float | None, Query()] = None,
    has_new: Annotated[bool | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "priority:desc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginatedResponse[LeadListItem]:
    base_stmt = (
        select(LeadScore, Company)
        .join(Company, LeadScore.company_id == Company.id)
        .where(
            LeadScore.org_id == principal.org_id,
            LeadScore.is_current == True,  # noqa: E712
        )
    )

    if service_id:
        base_stmt = base_stmt.where(LeadScore.service_id == service_id)
    if tier:
        base_stmt = base_stmt.where(LeadScore.tier == tier)
    if country:
        base_stmt = base_stmt.where(Company.country_code == country.upper())
    if industry:
        base_stmt = base_stmt.where(Company.industry_ids.any(industry))
    if min_priority is not None:
        base_stmt = base_stmt.where(LeadScore.priority >= min_priority)
    if q:
        search_filter = f"%{q.strip().lower()}%"
        base_stmt = base_stmt.where(
            or_(Company.name.ilike(search_filter), Company.domain.ilike(search_filter))
        )

    # Count total matching query
    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    # Sorting
    if sort == "priority:asc":
        stmt = base_stmt.order_by(LeadScore.priority.asc())
    elif sort == "fit:desc":
        stmt = base_stmt.order_by(LeadScore.fit.desc())
    elif sort == "intent:desc":
        stmt = base_stmt.order_by(LeadScore.intent.desc())
    elif sort == "name:asc":
        stmt = base_stmt.order_by(Company.name.asc())
    else:  # priority:desc
        stmt = base_stmt.order_by(LeadScore.priority.desc())

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    res = await session.execute(stmt)
    rows = res.all()

    # Pre-fetch signal metrics for companies
    cutoff_7d = datetime.now(UTC) - timedelta(days=7)
    items: list[LeadListItem] = []
    for score_row, comp_row in rows:
        # Count active signals and recent signals
        sig_count_stmt = select(
            func.count(Signal.id),
            func.count(Signal.id).filter(Signal.detected_at >= cutoff_7d),
        ).where(
            Signal.company_id == comp_row.id,
            Signal.service_id == score_row.service_id,
            Signal.status == "active",
        )
        sig_counts = (await session.execute(sig_count_stmt)).one_or_none()
        total_sigs = sig_counts[0] if sig_counts else 0
        new_7d = sig_counts[1] if sig_counts else 0

        if has_new is True and new_7d == 0:
            continue

        items.append(
            LeadListItem(
                company=CompanyOut.model_validate(comp_row),
                service_id=score_row.service_id,
                score=ScoreSummary(
                    priority=score_row.priority,
                    tier=score_row.tier,
                    fit=score_row.fit,
                    intent=score_row.intent,
                    risk=score_row.risk,
                    disqualified=score_row.disqualified,
                ),
                top_reasons=score_row.why_now or [],
                flags=[],
                signals_count=total_sigs,
                new_signals_7d=new_7d,
                analyzed_at=comp_row.last_analyzed_at,
            )
        )

    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/export.csv")
async def export_leads_csv(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service_id: Annotated[UUID | None, Query()] = None,
    tier: Annotated[str | None, Query()] = None,
) -> Response:
    stmt = (
        select(LeadScore, Company)
        .join(Company, LeadScore.company_id == Company.id)
        .where(
            LeadScore.org_id == principal.org_id,
            LeadScore.is_current == True,  # noqa: E712
        )
    )
    if service_id:
        stmt = stmt.where(LeadScore.service_id == service_id)
    if tier:
        stmt = stmt.where(LeadScore.tier == tier)

    stmt = stmt.order_by(LeadScore.priority.desc())
    res = await session.execute(stmt)
    rows = res.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Company Name",
            "Domain",
            "Country",
            "Employees",
            "Tier",
            "Priority",
            "Fit Score",
            "Intent Score",
            "Risk Score",
            "Disqualified",
            "Top Reasons",
            "Last Analyzed",
        ]
    )

    for score_row, comp_row in rows:
        reasons_text = "; ".join([r.get("text", "") for r in (score_row.why_now or [])])
        writer.writerow(
            [
                comp_row.name,
                comp_row.domain,
                comp_row.country_code or "",
                comp_row.employees or "",
                score_row.tier,
                float(score_row.priority),
                float(score_row.fit),
                float(score_row.intent),
                float(score_row.risk),
                score_row.disqualified,
                reasons_text,
                comp_row.last_analyzed_at.isoformat() if comp_row.last_analyzed_at else "",
            ]
        )

    csv_data = output.getvalue()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leads_export.csv"'},
    )


@router.get("/{company_id}", response_model=LeadDetail)
async def get_lead_detail(
    company_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service_id: Annotated[UUID | None, Query()] = None,
) -> LeadDetail:
    company = await session.get(Company, company_id)
    if not company or company.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    score_stmt = select(LeadScore).where(
        LeadScore.company_id == company_id,
        LeadScore.is_current == True,  # noqa: E712
    )
    if service_id:
        score_stmt = score_stmt.where(LeadScore.service_id == service_id)

    res = await session.execute(score_stmt)
    score = res.scalars().first()

    service_data = {"id": str(service_id), "name": "Service"}
    decision_makers = ["CIO", "COO", "Head of Digital Transformation"]
    resolved_service_id = service_id or (score.service_id if score else None)

    if resolved_service_id:
        s = await session.get(Service, resolved_service_id)
        if s:
            service_data = {"id": str(s.id), "name": s.name}
            if s.decision_makers:
                decision_makers = s.decision_makers

    score_data = {}
    if score:
        score_data = {
            "priority": float(score.priority),
            "tier": score.tier,
            "fit": float(score.fit),
            "intent": float(score.intent),
            "risk": float(score.risk),
            "disqualified": score.disqualified,
            "fit_details": score.fit_details,
            "rule_hits": score.rule_hits,
            "breakdown": score.breakdown,
            "why_now": score.why_now,
            "computed_at": score.computed_at.isoformat() if score.computed_at else None,
        }

    # Fetch active signals grouped by question
    sig_stmt = (
        select(Signal, SignalQuestion)
        .join(SignalQuestion, Signal.question_id == SignalQuestion.id)
        .where(
            Signal.company_id == company_id,
            Signal.status == "active",
        )
    )
    if resolved_service_id:
        sig_stmt = sig_stmt.where(Signal.service_id == resolved_service_id)

    sig_res = await session.execute(sig_stmt)
    sig_rows = sig_res.all()

    # Group signals by question
    grouped_questions: dict[UUID, tuple[SignalQuestion, list[Signal]]] = {}
    for sig, q in sig_rows:
        if q.id not in grouped_questions:
            grouped_questions[q.id] = (q, [])
        grouped_questions[q.id][1].append(sig)

    # strength and points of each question as computed by the scoring engine (score.breakdown)
    contributions = {
        c.get("question_id"): c for c in ((score.breakdown or []) if score else []) if isinstance(c, dict)
    }
    signals_by_question: list[QuestionSignals] = []
    for _qid, (q, sig_list) in grouped_questions.items():
        contribution = contributions.get(str(q.id), {})
        signals_by_question.append(
            QuestionSignals(
                question={
                    "id": str(q.id),
                    "key": q.key,
                    "text": q.text,
                    "category": q.category,
                    "polarity": q.polarity,
                    "weight": q.weight,
                },
                strength=float(contribution.get("strength", 0.0)),
                points=float(contribution.get("points", 0.0)),
                signals=[
                    SignalItem(
                        id=s.id,
                        quote=s.quote,
                        summary=s.summary,
                        strength=s.strength,
                        confidence=s.confidence,
                        url=s.url,
                        source_name=s.source_name,
                        source_type=s.source_type,
                        event_date=str(s.event_date) if s.event_date else None,
                        flags=s.flags or [],
                    )
                    for s in sig_list
                ],
            )
        )

    # Fetch score history
    hist_stmt = (
        select(LeadScore)
        .where(
            LeadScore.company_id == company_id,
            LeadScore.org_id == principal.org_id,
        )
        .order_by(LeadScore.computed_at.desc())
        .limit(10)
    )
    hist_res = await session.execute(hist_stmt)
    history = [
        {
            "computed_at": h.computed_at.isoformat() if h.computed_at else None,
            "priority": float(h.priority),
            "tier": h.tier,
            "is_current": h.is_current,
        }
        for h in hist_res.scalars().all()
    ]

    # Fetch sources summary
    doc_stmt = (
        select(Document.source_type, func.count())
        .where(Document.company_id == company_id)
        .group_by(Document.source_type)
    )
    doc_res = await session.execute(doc_stmt)
    sources_summary = {st: count for st, count in doc_res.all()}

    return LeadDetail(
        company=CompanyOut.model_validate(company),
        service=service_data,
        score=score_data,
        signals_by_question=signals_by_question,
        decision_makers=decision_makers,
        history=history,
        sources_summary=sources_summary,
    )
