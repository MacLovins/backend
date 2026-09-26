"""Trends: signal categories (ARCHITECTURE §3.3) folded into the handful of kinds a sales person scans for.

The same mapping exists twice on purpose: `trend_kind()` for one event or signal in Python (alert matching,
notification titles) and `trend_kind_expr()` as a SQL expression for aggregates over many signals (the leads
list and the leads summary). A test keeps them in step.
"""

import re
from collections.abc import Iterable
from datetime import date
from typing import Any, Literal

from leadradar_core.modules.config.models import SignalQuestion
from leadradar_core.modules.intelligence.models import Signal
from pydantic import BaseModel
from sqlalchemy import Date, Text, and_, case, cast, func, literal, or_, select

TrendKind = Literal[
    "hiring",
    "layoffs",
    "cost_cutting",
    "cyber_incident",
    "leadership_change",
    "growth",
    "ai_automation",
    "compliance",
    "distress",
]
TREND_KINDS: tuple[str, ...] = (
    "hiring",
    "layoffs",
    "cost_cutting",
    "cyber_incident",
    "leadership_change",
    "growth",
    "ai_automation",
    "compliance",
    "distress",
)

CATEGORY_TO_KIND: dict[str, str] = {
    "hiring": "hiring",
    "distress": "distress",  # or "layoffs" when the evidence talks about job cuts
    "cost_efficiency": "cost_cutting",
    "incident": "cyber_incident",
    "leadership_change": "leadership_change",
    "expansion": "growth",
    "investment": "growth",
    "ai_automation": "ai_automation",
    "digital_transformation": "ai_automation",
    "compliance": "compliance",
}
LAYOFF_TERMS = ("layoff", "job cut", "stellenabbau", "redundanc")
_LAYOFF_RE = re.compile("|".join(re.escape(t) for t in LAYOFF_TERMS), re.IGNORECASE)

STRENGTH_RANK = {"weak": 1, "moderate": 2, "strong": 3}
RANK_STRENGTH = {v: k for k, v in STRENGTH_RANK.items()}

# Short labels for titles and e-mails; the SPA has its own copy for chips.
TREND_LABELS: dict[str, str] = {
    "hiring": "Hiring",
    "layoffs": "Layoffs",
    "cost_cutting": "Cost cutting",
    "cyber_incident": "Cyber incident",
    "leadership_change": "Leadership change",
    "growth": "Growth",
    "ai_automation": "AI & automation",
    "compliance": "Compliance",
    "distress": "Financial distress",
}

WHY_IT_MATTERS: dict[str, str] = {
    "hiring": "hiring for these roles usually means budget for tooling and a team that needs to deliver.",
    "layoffs": "job cuts come with pressure to keep output up with fewer people — automation budgets open.",
    "cost_cutting": "a cost programme needs quick, measurable savings; efficiency projects get funded.",
    "cyber_incident": "after an incident, security spend is approved fast and the board asks for a plan.",
    "leadership_change": "a new leader reviews vendors and starts initiatives in the first 100 days.",
    "growth": "expansion and fresh capital mean new processes to build and money to spend on them.",
    "ai_automation": "an active AI or automation agenda is the best sign of an open door for this service.",
    "compliance": "a regulatory deadline creates a budget and a date the buyer cannot move.",
    "distress": "spending blockers: worth knowing before investing sales time.",
}


def trend_kind(category: str | None, summary: str | None = None, quote: str | None = None) -> str | None:
    """Kind of one signal or event, None when the category carries no trend (tech_stack, vendors...)."""
    if not category:
        return None
    kind = CATEGORY_TO_KIND.get(category)
    if kind == "distress" and _LAYOFF_RE.search(f"{summary or ''} {quote or ''}"):
        return "layoffs"
    return kind


def trend_kind_expr():
    """The same mapping as `trend_kind()` for Signal rows, as a SQL CASE expression."""
    text = func.lower(func.concat(Signal.summary, literal(" "), Signal.quote))
    layoffs = or_(*(text.contains(term) for term in LAYOFF_TERMS))
    branches = [((Signal.category == "distress") & layoffs, literal("layoffs"))]
    branches += [(Signal.category == cat, literal(kind)) for cat, kind in CATEGORY_TO_KIND.items()]
    return case(*branches, else_=None)


def strength_rank_expr():
    return case(*[(Signal.strength == s, literal(r)) for s, r in STRENGTH_RANK.items()], else_=0)


def signal_date_expr():
    """event_date, else the source's publication date, else when the signal was detected."""
    return func.coalesce(Signal.event_date, cast(Signal.published_at, Date), cast(Signal.detected_at, Date))


def evidence_key_expr():
    """Distinct evidence events: the evidence key when known, else the row itself."""
    return func.coalesce(Signal.evidence_key, cast(Signal.id, Text))


class TrendOut(BaseModel):
    kind: TrendKind
    count: int
    latest_at: date | None = None
    strength: Literal["weak", "moderate", "strong"] | None = None


def trend_rows_query(org_id, where: Iterable[Any]):
    """Grouped (company_id, service_id, kind, count, latest_at, strength_rank) over active signals."""
    kind = trend_kind_expr()
    return (
        select(
            Signal.company_id,
            Signal.service_id,
            kind.label("kind"),
            func.count(func.distinct(evidence_key_expr())).label("count"),
            func.max(signal_date_expr()).label("latest_at"),
            func.max(strength_rank_expr()).label("strength_rank"),
        )
        .join(
            SignalQuestion, and_(SignalQuestion.id == Signal.question_id, SignalQuestion.is_active.is_(True))
        )
        .where(Signal.org_id == org_id, Signal.status == "active", kind.is_not(None), *where)
        .group_by(Signal.company_id, Signal.service_id, kind)
    )


def trends_from_rows(rows: Iterable[Any]) -> dict[tuple[Any, Any], list[TrendOut]]:
    """{(company_id, service_id): trends sorted by count desc, kind} from `trend_rows_query` rows."""
    out: dict[tuple[Any, Any], list[TrendOut]] = {}
    for company_id, service_id, kind, count, latest_at, strength_rank in rows:
        out.setdefault((company_id, service_id), []).append(
            TrendOut(
                kind=kind, count=count, latest_at=latest_at, strength=RANK_STRENGTH.get(strength_rank or 0)
            )
        )
    for trends in out.values():
        trends.sort(key=lambda t: (-t.count, t.kind))
    return out


def lead_link(public_origin: str, company_id: object, service_id: object | None = None) -> str:
    """The lead page in the SPA (`/companies/:companyId?service=`), used by alerts, e-mails and HubSpot."""
    url = f"{public_origin.rstrip('/')}/companies/{company_id}"
    return f"{url}?service={service_id}" if service_id else url
