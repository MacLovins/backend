import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.accounts.schemas import CompanyOut
from leadradar_core.modules.alerts.service import watched_company_ids, watched_expr
from leadradar_core.modules.config.models import ScoringProfile, Service, SignalQuestion
from leadradar_core.modules.feedback.models import Feedback
from leadradar_core.modules.intelligence.models import Document, Signal
from leadradar_core.modules.leads.models import LeadScore
from leadradar_core.modules.leads.schemas import (
    LeadDetail,
    LeadListItem,
    LeadsSummary,
    QuestionSignals,
    ScoreSummary,
    SignalItem,
    TrendCount,
)
from leadradar_core.modules.leads.trends import (
    STRENGTH_RANK,
    TREND_KINDS,
    evidence_key_expr,
    strength_rank_expr,
    trend_kind,
    trend_kind_expr,
    trend_rows_query,
    trends_from_rows,
)
from leadradar_core.pagination import PaginatedResponse
from sqlalchemy import Date, Select, and_, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/leads", tags=["leads"])

NEW_SIGNAL_WINDOW = timedelta(days=7)
JOBS_OPEN_WINDOW = timedelta(days=30)


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
    trends: list[str]
    trend_min_strength: str | None
    watched: bool | None


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
    trend: Annotated[
        list[str] | None, Query(description="One or more trend kinds (repeat or comma-separate)")
    ] = None,
    trend_min_strength: Annotated[
        Literal["weak", "moderate", "strong"] | None,
        Query(description="Minimum signal strength (temperature) of the trend: weak, moderate or strong"),
    ] = None,
    watched: Annotated[
        bool | None,
        Query(description="true: only companies the current user watches, false: only the others"),
    ] = None,
) -> LeadFilters:
    trends = _multi(trend)
    unknown = [t for t in trends if t not in TREND_KINDS]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown trend kind(s): {', '.join(unknown)}; expected any of {', '.join(TREND_KINDS)}",
        )
    return LeadFilters(
        service_id=service_id,
        tiers=_multi(tier),
        countries=[c.upper() for c in _multi(country)],
        industries=_multi(industry),
        min_priority=min_priority,
        has_new=has_new,
        q=q.strip() if q and q.strip() else None,
        trends=trends,
        trend_min_strength=trend_min_strength,
        watched=watched,
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


def _has_trend(org_id: UUID, kinds: list[str], min_strength: str | None):
    """EXISTS: an active signal (of an active question) of the lead's company and service whose trend kind is
    one of `kinds` (any trend kind when empty) and whose strength is at least `min_strength`."""
    kind = trend_kind_expr()
    where = [kind.in_(kinds) if kinds else kind.is_not(None)]
    if min_strength is not None:
        where.append(strength_rank_expr() >= STRENGTH_RANK[min_strength])
    return (
        select(Signal.id)
        .join(
            SignalQuestion, and_(SignalQuestion.id == Signal.question_id, SignalQuestion.is_active.is_(True))
        )
        .where(
            Signal.org_id == org_id,
            Signal.company_id == LeadScore.company_id,
            Signal.service_id == LeadScore.service_id,
            Signal.status == "active",
            *where,
        )
        .correlate(LeadScore)
        .exists()
    )


def _jobs_open(org_id: UUID):
    """Job postings per company published in the last 30 days."""
    return (
        select(Document.company_id, func.count(Document.id).label("jobs_open"))
        .where(
            Document.org_id == org_id,
            Document.source_type == "jobs",
            Document.published_at >= datetime.now(UTC) - JOBS_OPEN_WINDOW,
        )
        .group_by(Document.company_id)
        .subquery("jobs_open")
    )


TIERS = ("hot", "warm", "cold", "disqualified")
SORT_FIELDS = {"priority", "fit", "intent", "risk", "name", "last_signal_at", "signals_count"}


def _leads_query(org_id: UUID, filters: LeadFilters, sort: str, user_id: UUID | None = None) -> Select:
    """One query for the whole page: current score + company + signal aggregates, filtered in SQL."""
    stats = _signal_stats(org_id)
    jobs = _jobs_open(org_id)
    signals_count = func.coalesce(stats.c.signals_count, 0)
    new_signals = func.coalesce(stats.c.new_signals_7d, 0)
    watched = watched_expr(user_id, Company.id) if user_id is not None else literal(False)
    stmt = (
        select(
            LeadScore,
            Company,
            signals_count.label("signals_count"),
            new_signals.label("new_signals_7d"),
            stats.c.last_signal_at,
            jobs.c.jobs_open,
            watched.label("watched"),
        )
        .join(Company, LeadScore.company_id == Company.id)
        .outerjoin(
            stats,
            and_(stats.c.company_id == LeadScore.company_id, stats.c.service_id == LeadScore.service_id),
        )
        .outerjoin(jobs, jobs.c.company_id == LeadScore.company_id)
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
    if filters.trends or filters.trend_min_strength:
        stmt = stmt.where(_has_trend(org_id, filters.trends, filters.trend_min_strength))
    if filters.watched is True:
        stmt = stmt.where(watched)
    elif filters.watched is False:
        stmt = stmt.where(~watched)

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
    stmt = _leads_query(principal.org_id, filters, sort, principal.user_id)
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    rows = (await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    trends = await _page_trends(session, principal.org_id, [(s.company_id, s.service_id) for s, *_ in rows])
    items = []
    for score, company, signals_count, new_signals_7d, last_signal_at, jobs_open, watched in rows:
        company_out = CompanyOut.model_validate(company)
        company_out.watched = bool(watched)
        items.append(
            LeadListItem(
                company=company_out,
                service_id=score.service_id,
                score=ScoreSummary.model_validate(score),
                top_reasons=score.why_now or [],
                flags=_flags(score.rule_hits),
                signals_count=signals_count,
                new_signals_7d=new_signals_7d,
                last_signal_at=last_signal_at,
                analyzed_at=company.last_analyzed_at,
                trends=trends.get((score.company_id, score.service_id), []),
                jobs_open=jobs_open,
                watched=bool(watched),
            )
        )
    return PaginatedResponse(items=items, total=total, page=page, page_size=page_size)


async def _page_trends(session: AsyncSession, org_id: UUID, keys: list[tuple[UUID, UUID]]):
    """Trends of the page's (company, service) pairs in one query (the third one of the list)."""
    if not keys:
        return {}
    company_ids = {c for c, _ in keys}
    service_ids = {s for _, s in keys}
    rows = await session.execute(
        trend_rows_query(org_id, [Signal.company_id.in_(company_ids), Signal.service_id.in_(service_ids)])
    )
    return trends_from_rows(rows.all())


@router.get("/summary", response_model=LeadsSummary)
async def leads_summary(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service_id: Annotated[UUID | None, Query()] = None,
) -> LeadsSummary:
    """Totals for the Leads start page in three queries: tiers, signal trends, watched companies."""
    org_id = principal.org_id
    scores = select(
        func.count(LeadScore.id),
        *(func.count(LeadScore.id).filter(LeadScore.tier == t) for t in TIERS),
    ).where(LeadScore.org_id == org_id, LeadScore.is_current.is_(True))
    if service_id is not None:
        scores = scores.where(LeadScore.service_id == service_id)
    total, *by_tier = (await session.execute(scores)).one()

    kind = trend_kind_expr()
    cutoff = datetime.now(UTC) - NEW_SIGNAL_WINDOW
    trends_stmt = (
        select(
            kind.label("kind"),
            func.count(func.distinct(evidence_key_expr())).label("count"),
            func.count(Signal.id).filter(Signal.detected_at >= cutoff).label("new_7d"),
        )
        .join(
            SignalQuestion, and_(SignalQuestion.id == Signal.question_id, SignalQuestion.is_active.is_(True))
        )
        .where(Signal.org_id == org_id, Signal.status == "active")
        .group_by(kind)
    )
    if service_id is not None:
        trends_stmt = trends_stmt.where(Signal.service_id == service_id)
    trend_rows = (await session.execute(trends_stmt)).all()
    new_signals_7d = sum(new for _, _, new in trend_rows)
    trends_top = sorted(
        (TrendCount(kind=k, count=c) for k, c, _ in trend_rows if k is not None),
        key=lambda t: (-t.count, t.kind),
    )

    watched = await session.scalar(
        select(func.count(func.distinct(Company.id))).where(
            Company.org_id == org_id, watched_expr(principal.user_id, Company.id)
        )
    )
    return LeadsSummary(
        total=total or 0,
        by_tier=dict(zip(TIERS, by_tier, strict=True)),
        new_signals_7d=new_signals_7d,
        watched=watched or 0,
        trends_top=trends_top,
    )


@router.get("/export.csv")
async def export_leads_csv(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    filters: Annotated[LeadFilters, Depends(lead_filters)],
    sort: Annotated[str, Query()] = "priority:desc",
) -> Response:
    """Same filters and order as GET /leads, without pagination."""
    rows = (await session.execute(_leads_query(principal.org_id, filters, sort, principal.user_id))).all()

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
    for score, company, signals_count, new_signals_7d, last_signal_at, *_ in rows:
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
    watched = company_id in await watched_company_ids(session, principal.org_id, principal.user_id)
    company_out = CompanyOut.model_validate(company)
    company_out.watched = watched
    jobs_open = await session.scalar(
        select(func.count(Document.id)).where(
            Document.company_id == company_id,
            Document.source_type == "jobs",
            Document.published_at >= datetime.now(UTC) - JOBS_OPEN_WINDOW,
        )
    )
    if service is None:
        return LeadDetail(
            company=company_out,
            service={},
            score={},
            sources_summary=sources_summary,
            jobs_open=jobs_open,
            watched=watched,
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
                        trend_kind=trend_kind(q.category, s.summary, s.quote),
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

    trends = trends_from_rows(
        (
            await session.execute(
                trend_rows_query(
                    principal.org_id, [Signal.company_id == company_id, Signal.service_id == service.id]
                )
            )
        ).all()
    )
    return LeadDetail(
        company=company_out,
        service={"id": str(service.id), "name": service.name},
        score=score_data,
        signals_by_question=signals_by_question,
        questions_without_evidence=questions_without_evidence,
        my_feedback=my_lead_feedback,
        decision_makers=service.decision_makers or ["CIO", "COO", "Head of Digital Transformation"],
        history=history,
        sources_summary=sources_summary,
        trends=trends.get((company_id, service.id), []),
        jobs_open=jobs_open,
        watched=watched,
    )
