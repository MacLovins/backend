import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.accounts.schemas import CompanyOut
from leadradar_core.modules.config.models import ScoringProfile, Service, SignalQuestion
from leadradar_core.modules.feedback.models import Feedback
from leadradar_core.modules.intelligence.models import Document, Signal
from leadradar_core.modules.leads.models import LeadScore
from leadradar_core.modules.leads.schemas import (
    LeadDetail,
    LeadListItem,
    OutreachDraftOut,
    OutreachGenerateIn,
    QuestionSignals,
    ScoreSummary,
    SignalItem,
)
from leadradar_core.pagination import PaginatedResponse
from sqlalchemy import Date, Select, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/leads", tags=["leads"])

NEW_SIGNAL_WINDOW = timedelta(days=7)


# --- list filters (shared by the list and the CSV export) -------------------------------------


def _multi(values: list[str] | None) -> list[str]:
    """`?tier=hot&tier=warm` and `?tier=hot,warm` are both accepted."""
    return [part.strip() for value in values or [] for part in value.split(",") if part.strip()]


@dataclass(frozen=True)
class LeadFilters:
    service_id: UUID | None
    tiers: list[str]
    countries: list[str]
    industries: list[str]
    min_priority: float | None
    has_new: bool | None
    q: str | None


def lead_filters(
    service_id: Annotated[UUID | None, Query()] = None,
    tier: Annotated[
        list[str] | None, Query(description="One or more tiers (repeat or comma-separate)")
    ] = None,
    country: Annotated[list[str] | None, Query(description="One or more ISO2 country codes")] = None,
    industry: Annotated[list[str] | None, Query(description="One or more industry ids")] = None,
    min_priority: Annotated[float | None, Query()] = None,
    has_new: Annotated[bool | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
) -> LeadFilters:
    return LeadFilters(
        service_id=service_id,
        tiers=_multi(tier),
        countries=[c.upper() for c in _multi(country)],
        industries=_multi(industry),
        min_priority=min_priority,
        has_new=has_new,
        q=q.strip() if q and q.strip() else None,
    )


def _signal_stats(org_id: UUID):
    """Active signals per company and service: count, new in the last 7 days and the latest signal date."""
    cutoff = datetime.now(UTC) - NEW_SIGNAL_WINDOW
    signal_date = func.coalesce(
        Signal.event_date, cast(Signal.published_at, Date), cast(Signal.detected_at, Date)
    )
    return (
        select(
            Signal.company_id,
            Signal.service_id,
            func.count(Signal.id).label("signals_count"),
            func.count(Signal.id).filter(Signal.detected_at >= cutoff).label("new_signals_7d"),
            func.max(signal_date).label("last_signal_at"),
        )
        .join(
            SignalQuestion, and_(SignalQuestion.id == Signal.question_id, SignalQuestion.is_active.is_(True))
        )
        .where(Signal.org_id == org_id, Signal.status == "active")
        .group_by(Signal.company_id, Signal.service_id)
        .subquery("signal_stats")
    )


SORT_FIELDS = {"priority", "fit", "intent", "risk", "name", "last_signal_at", "signals_count"}


def _leads_query(org_id: UUID, filters: LeadFilters, sort: str) -> Select:
    """One query for the whole page: current score + company + signal aggregates, filtered in SQL."""
    stats = _signal_stats(org_id)
    signals_count = func.coalesce(stats.c.signals_count, 0)
    new_signals = func.coalesce(stats.c.new_signals_7d, 0)
    stmt = (
        select(
            LeadScore,
            Company,
            signals_count.label("signals_count"),
            new_signals.label("new_signals_7d"),
            stats.c.last_signal_at,
        )
        .join(Company, LeadScore.company_id == Company.id)
        .outerjoin(
            stats,
            and_(stats.c.company_id == LeadScore.company_id, stats.c.service_id == LeadScore.service_id),
        )
        .where(LeadScore.org_id == org_id, LeadScore.is_current.is_(True))
    )
    if filters.service_id:
        stmt = stmt.where(LeadScore.service_id == filters.service_id)
    if filters.tiers:
        stmt = stmt.where(LeadScore.tier.in_(filters.tiers))
    if filters.countries:
        stmt = stmt.where(Company.country_code.in_(filters.countries))
    if filters.industries:
        stmt = stmt.where(Company.industry_ids.overlap(filters.industries))
    if filters.min_priority is not None:
        stmt = stmt.where(LeadScore.priority >= filters.min_priority)
    if filters.has_new is True:
        stmt = stmt.where(new_signals > 0)
    elif filters.has_new is False:
        stmt = stmt.where(new_signals == 0)
    if filters.q:
        pattern = f"%{filters.q.lower()}%"
        stmt = stmt.where(or_(Company.name.ilike(pattern), Company.domain.ilike(pattern)))

    field, _, direction = sort.partition(":")
    field = field if field in SORT_FIELDS else "priority"
    descending = direction != "asc" if field != "name" else direction == "desc"
    column = {
        "priority": LeadScore.priority,
        "fit": LeadScore.fit,
        "intent": LeadScore.intent,
        "risk": LeadScore.risk,
        "name": func.lower(Company.name),
        "last_signal_at": stats.c.last_signal_at,
        "signals_count": signals_count,
    }[field]
    primary = column.desc().nulls_last() if descending else column.asc().nulls_last()
    # ties: intent ↓, fit ↓, name ↑ (the ranking order of ai.lead_sort_key), id for stable pages
    return stmt.order_by(
        primary,
        LeadScore.intent.desc(),
        LeadScore.fit.desc(),
        func.lower(Company.name).asc(),
        LeadScore.id,
    )


def _flags(rule_hits: list[dict[str, Any]] | None) -> list[str]:
    """Names of the disqualification rules that fired (exclude, cap or flag), in rule order."""
    names = [h.get("name") for h in rule_hits or [] if isinstance(h, dict) and h.get("name")]
    return list(dict.fromkeys(names))


@router.get("", response_model=PaginatedResponse[LeadListItem])
async def list_leads(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    filters: Annotated[LeadFilters, Depends(lead_filters)],
    sort: Annotated[
        str,
        Query(description="priority|fit|intent|risk|name|last_signal_at|signals_count, ':asc' or ':desc'"),
    ] = "priority:desc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginatedResponse[LeadListItem]:
    stmt = _leads_query(principal.org_id, filters, sort)
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    rows = (await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    items = [
        LeadListItem(
            company=CompanyOut.model_validate(company),
            service_id=score.service_id,
            score=ScoreSummary.model_validate(score),
            top_reasons=score.why_now or [],
            flags=_flags(score.rule_hits),
            signals_count=signals_count,
            new_signals_7d=new_signals_7d,
            last_signal_at=last_signal_at,
            analyzed_at=company.last_analyzed_at,
        )
        for score, company, signals_count, new_signals_7d, last_signal_at in rows
    ]
    return PaginatedResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/export.csv")
async def export_leads_csv(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    filters: Annotated[LeadFilters, Depends(lead_filters)],
    sort: Annotated[str, Query()] = "priority:desc",
) -> Response:
    """Same filters and order as GET /leads, without pagination."""
    rows = (await session.execute(_leads_query(principal.org_id, filters, sort))).all()

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
            "Flags",
            "Signals",
            "New Signals 7d",
            "Last Signal",
        ]
    )
    for score, company, signals_count, new_signals_7d, last_signal_at in rows:
        reasons_text = "; ".join(r.get("text", "") for r in (score.why_now or []))
        writer.writerow(
            [
                company.name,
                company.domain,
                company.country_code or "",
                company.employees or "",
                score.tier,
                float(score.priority),
                float(score.fit),
                float(score.intent),
                float(score.risk),
                score.disqualified,
                reasons_text,
                company.last_analyzed_at.isoformat() if company.last_analyzed_at else "",
                "; ".join(_flags(score.rule_hits)),
                signals_count,
                new_signals_7d,
                last_signal_at.isoformat() if last_signal_at else "",
            ]
        )

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leads_export.csv"'},
    )


# --- lead card ---------------------------------------------------------------------------------


async def _resolve_service(
    session: AsyncSession, org_id: UUID, company_id: UUID, service_id: UUID | None
) -> Service | None:
    """The requested service, else the one where the company ranks best, else the first active service."""
    if service_id is not None:
        service = await session.get(Service, service_id)
        if service is None or service.org_id != org_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
        return service
    best = (
        await session.execute(
            select(Service)
            .join(LeadScore, LeadScore.service_id == Service.id)
            .where(
                LeadScore.company_id == company_id,
                LeadScore.org_id == org_id,
                LeadScore.is_current.is_(True),
            )
            .order_by(
                Service.is_active.desc(),
                LeadScore.priority.desc(),
                LeadScore.intent.desc(),
                LeadScore.fit.desc(),
                Service.slug,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if best is not None:
        return best
    return (
        await session.execute(
            select(Service)
            .where(Service.org_id == org_id, Service.is_active.is_(True))
            .order_by(Service.slug)
            .limit(1)
        )
    ).scalar_one_or_none()


def _question_dict(q: SignalQuestion) -> dict[str, Any]:
    return {
        "id": str(q.id),
        "key": q.key,
        "text": q.text,
        "category": q.category,
        "polarity": q.polarity,
        "weight": q.weight,
    }


async def _my_signal_feedback(
    session: AsyncSession, user_id: UUID, company_id: UUID, service_id: UUID
) -> tuple[dict[UUID, str], dict[str, str]]:
    """The user's verdicts by signal id and by evidence key (a vote on an older run's copy still shows)."""
    rows = (
        await session.execute(
            select(Signal.id, Signal.evidence_key, Feedback.verdict)
            .join(Feedback, and_(Feedback.target_type == "signal", Feedback.target_id == Signal.id))
            .where(
                Feedback.user_id == user_id,
                Signal.company_id == company_id,
                Signal.service_id == service_id,
            )
            .order_by(Feedback.updated_at)
        )
    ).all()
    by_id = {sid: verdict for sid, _, verdict in rows}
    by_key = {key: verdict for _, key, verdict in rows if key}  # latest vote wins
    return by_id, by_key


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

    service = await _resolve_service(session, principal.org_id, company_id, service_id)
    sources_summary = dict(
        (
            await session.execute(
                select(Document.source_type, func.count())
                .where(Document.company_id == company_id)
                .group_by(Document.source_type)
            )
        ).all()
    )
    if service is None:
        return LeadDetail(
            company=CompanyOut.model_validate(company),
            service={},
            score={},
            sources_summary=sources_summary,
        )

    score_row = (
        await session.execute(
            select(LeadScore, ScoringProfile.version)
            .outerjoin(ScoringProfile, ScoringProfile.id == LeadScore.scoring_profile_id)
            .where(
                LeadScore.company_id == company_id,
                LeadScore.service_id == service.id,
                LeadScore.is_current.is_(True),
            )
        )
    ).first()
    score, profile_version = score_row if score_row else (None, None)

    score_data: dict[str, Any] = {}
    if score:
        score_data = {
            **ScoreSummary.model_validate(score).model_dump(),
            "fit_details": score.fit_details,
            "rule_hits": score.rule_hits,
            "flags": _flags(score.rule_hits),
            "data_gaps": score.data_gaps or [],
            "breakdown": score.breakdown,
            "why_now": score.why_now,
            "scoring_profile_version": profile_version,
            "computed_at": score.computed_at.isoformat() if score.computed_at else None,
        }

    questions = (
        (
            await session.execute(
                select(SignalQuestion)
                .where(SignalQuestion.service_id == service.id, SignalQuestion.is_active.is_(True))
                .order_by(SignalQuestion.created_at, SignalQuestion.key)
            )
        )
        .scalars()
        .all()
    )
    signals = (
        (
            await session.execute(
                select(Signal)
                .where(
                    Signal.company_id == company_id,
                    Signal.service_id == service.id,
                    Signal.status == "active",
                )
                .order_by(Signal.confidence.desc(), Signal.detected_at.desc())
            )
        )
        .scalars()
        .all()
    )
    by_question: dict[UUID, list[Signal]] = {}
    for s in signals:
        by_question.setdefault(s.question_id, []).append(s)

    fb_by_id, fb_by_key = await _my_signal_feedback(session, principal.user_id, company_id, service.id)
    # strength and points of each question as computed by the scoring engine (score.breakdown)
    contributions = {
        c.get("question_id"): c for c in ((score.breakdown or []) if score else []) if isinstance(c, dict)
    }
    signals_by_question: list[QuestionSignals] = []
    questions_without_evidence: list[dict[str, Any]] = []
    for q in questions:
        sig_list = by_question.get(q.id)
        if not sig_list:
            questions_without_evidence.append(_question_dict(q))
            continue
        contribution = contributions.get(str(q.id), {})
        signals_by_question.append(
            QuestionSignals(
                question=_question_dict(q),
                strength=float(contribution.get("strength", 0.0)),
                points=float(contribution.get("points", 0.0)),
                signals=[
                    SignalItem(
                        id=s.id,
                        quote=s.quote,
                        summary=s.summary,
                        strength=s.strength,
                        confidence=float(s.confidence),
                        url=s.url,
                        source_name=s.source_name,
                        source_type=s.source_type,
                        event_date=str(s.event_date) if s.event_date else None,
                        flags=s.flags or [],
                        my_feedback=fb_by_id.get(s.id) or fb_by_key.get(s.evidence_key or ""),
                    )
                    for s in sig_list
                ],
            )
        )
    signals_by_question.sort(key=lambda g: g.points, reverse=True)

    my_lead_feedback = (
        await session.execute(
            select(Feedback.verdict).where(
                Feedback.user_id == principal.user_id,
                Feedback.target_type == "lead",
                Feedback.target_id == company_id,
                Feedback.service_id == service.id,
            )
        )
    ).scalar_one_or_none()

    history_rows = (
        (
            await session.execute(
                select(LeadScore)
                .where(
                    LeadScore.company_id == company_id,
                    LeadScore.service_id == service.id,
                    LeadScore.org_id == principal.org_id,
                )
                .order_by(LeadScore.computed_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    history = [
        {
            "computed_at": h.computed_at.isoformat() if h.computed_at else None,
            "priority": float(h.priority),
            "tier": h.tier,
            "is_current": h.is_current,
        }
        for h in history_rows
    ]

    return LeadDetail(
        company=CompanyOut.model_validate(company),
        service={"id": str(service.id), "name": service.name},
        score=score_data,
        signals_by_question=signals_by_question,
        questions_without_evidence=questions_without_evidence,
        my_feedback=my_lead_feedback,
        decision_makers=service.decision_makers or ["CIO", "COO", "Head of Digital Transformation"],
        history=history,
        sources_summary=sources_summary,
    )


@router.post("/{company_id}/outreach", response_model=OutreachDraftOut)
async def generate_lead_outreach(
    company_id: UUID,
    generate_in: OutreachGenerateIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> OutreachDraftOut:
    company = await session.get(Company, company_id)
    if not company or company.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    service_id = generate_in.service_id
    if not service_id:
        # Pick the top service with the highest lead score
        top_score_stmt = (
            select(LeadScore.service_id)
            .where(
                LeadScore.company_id == company_id,
                LeadScore.org_id == principal.org_id,
                LeadScore.is_current == True,  # noqa: E712
            )
            .order_by(LeadScore.priority.desc())
            .limit(1)
        )
        service_id = (await session.execute(top_score_stmt)).scalar_one_or_none()

    if not service_id:
        # Pick any active service in the organization
        any_svc = (
            await session.execute(
                select(Service.id)
                .where(Service.org_id == principal.org_id, Service.is_active.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none()
        service_id = any_svc

    if not service_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active service configured")

    service = await session.get(Service, service_id)
    if not service or service.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    import leadradar_ai as ai
    from leadradar_core.adapters import mapping
    from leadradar_core.modules.intelligence.service import load_bundle
    from leadradar_core.worker.deps import worker_context

    bundle = await load_bundle(session, service)

    sig_stmt = (
        select(Signal)
        .where(
            Signal.company_id == company_id,
            Signal.service_id == service_id,
            Signal.org_id == principal.org_id,
            Signal.status == "active",
        )
        .order_by(Signal.confidence.desc())
        .limit(5)
    )
    sig_rows = (await session.execute(sig_stmt)).scalars().all()

    profile = mapping.company_profile(company)
    signals = [
        ai.VerifiedSignal(
            question_id=s.question_id,
            question_key=s.question_key,
            question_version=s.question_version,
            category=s.category,
            polarity=s.polarity,
            document_id=s.document_id or s.id,
            chunk_id=s.chunk_id,
            url=s.url or "",
            source_type=s.source_type if s.source_type in ai.contracts.SourceType.__args__ else "news",
            source_name=s.source_name,
            quote=s.quote,
            quote_start=s.quote_start,
            quote_end=s.quote_end,
            summary=s.summary,
            strength=s.strength if s.strength in ("weak", "moderate", "strong") else "moderate",
            confidence=float(s.confidence),
            reliability=float(s.reliability) if s.reliability is not None else 0.8,
            event_date=s.event_date,
            published_at=s.published_at,
            flags=set(s.flags or []),
            model=s.model or "unknown",
            prompt_version=s.prompt_version or "extract_signals@v1",
        )
        for s in sig_rows
    ]

    outreach_req = ai.OutreachRequest(
        channel=generate_in.channel
        if generate_in.channel in ("email", "linkedin_inmail", "call_script")
        else "email",
        language=generate_in.language,
        tone=generate_in.tone
        if generate_in.tone in ("professional", "conversational", "direct")
        else "professional",
        sender_name=generate_in.sender_name,
        sender_title=generate_in.sender_title,
        sender_company=generate_in.sender_company,
    )

    try:
        await worker_context.start()
        llm = worker_context.llm
    except Exception:
        llm = None

    if llm is not None:
        draft = await ai.generate_outreach(llm, profile, bundle, signals, outreach_req)
    else:
        from leadradar_ai.outreach.generator import fallback_draft

        draft = fallback_draft(profile, bundle, signals, outreach_req)

    return OutreachDraftOut(
        channel=draft.channel,
        subject=draft.subject,
        body=draft.body,
        referenced_signals=draft.referenced_signals,
        referenced_quotes=draft.referenced_quotes,
        hook=draft.hook,
        call_to_action=draft.call_to_action,
        language=draft.language,
    )
