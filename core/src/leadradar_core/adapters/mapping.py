"""The only place where core ORM rows and parser/ai contracts are converted into each other (ARCHITECTURE §4.4)."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import leadradar_ai as ai
import leadradar_parser as parser
from pydantic import ValidationError
from structlog import get_logger

from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.config.models import (
    DisqualificationRule,
    ICPProfile,
    ScoringProfile,
    Service,
    SignalQuestion,
)
from leadradar_core.modules.intelligence.models import Document, Signal
from leadradar_core.modules.leads.models import LeadScore

log = get_logger(__name__)

_SCORING_FIELDS = set(ai.ScoringProfile.model_fields) - {"id", "version"}
_SOURCE_TYPES = {"news", "website", "jobs", "report", "registry", "incident", "derived", "manual"}


def _int(value: Decimal | int | None) -> int | None:
    return None if value is None else int(value)


# --- company -----------------------------------------------------------------------------------


def company_profile(company: Company) -> ai.CompanyProfile:
    return ai.CompanyProfile(
        id=company.id,
        name=company.name,
        domain=company.domain,
        aliases=list(company.aliases or []),
        own_domains=list(company.own_domains or []),
        country_code=company.country_code,
        industry_ids=list(company.industry_ids or []),
        employees=company.employees,
        revenue_eur=_int(company.revenue_eur),
        tags=list(company.tags or []),
    )


def company_ref(company: Company) -> parser.CompanyRef:
    ats = None
    if company.ats:
        try:
            ats = parser.AtsRef.model_validate(company.ats)
        except ValidationError:
            ats = None
    return parser.CompanyRef(
        name=company.name,
        domain=company.domain,
        country_code=company.country_code,
        aliases=list(company.aliases or []),
        careers_url=company.careers_url,
        newsroom_url=company.newsroom_url,
        ats=ats,
        wikidata_qid=company.wikidata_qid,
    )


def apply_resolved(company: Company, resolved: parser.ResolvedCompany) -> None:
    """Fill what the resolver found; manual data always wins."""
    company.homepage_url = company.homepage_url or resolved.homepage_url
    company.own_domains = sorted({*(company.own_domains or []), *resolved.own_domains})
    company.careers_url = company.careers_url or resolved.careers_url
    company.newsroom_url = company.newsroom_url or resolved.newsroom_url
    if resolved.ats and not company.ats:
        company.ats = resolved.ats.model_dump(mode="json")
    firmo = resolved.firmographics
    if firmo:
        company.country_code = company.country_code or firmo.country_code
        company.industry_ids = company.industry_ids or list(firmo.industry_ids)
        company.employees = company.employees or firmo.employees
        if company.revenue_eur is None and firmo.revenue_eur is not None:
            company.revenue_eur = Decimal(firmo.revenue_eur)
        company.hq_city = company.hq_city or firmo.hq_city
        company.wikidata_qid = company.wikidata_qid or firmo.wikidata_qid
        company.lei = company.lei or firmo.lei
        company.crunchbase_id = company.crunchbase_id or firmo.crunchbase_id
    company.resolved_at = resolved.resolved_at


# --- service configuration ---------------------------------------------------------------------


def question_config(q: SignalQuestion) -> ai.QuestionConfig:
    return ai.QuestionConfig(
        id=q.id,
        key=q.key,
        version=q.version,
        text=q.text,
        category=q.category,
        polarity=q.polarity,
        weight=q.weight,
        source_types={s for s in (q.source_types or []) if s in _SOURCE_TYPES} or {"news", "website"},
        recency_days=q.recency_days,
        keywords=dict(q.keywords or {}),
        job_titles=list(q.job_titles or []),
        negative_terms=list(q.negative_terms or []),
    )


def icp_config(icp: ICPProfile | None) -> ai.ICPConfig:
    if icp is None:
        return ai.ICPConfig()
    criteria = (icp.nice_to_have or {}).get("criteria", []) if isinstance(icp.nice_to_have, dict) else []
    return ai.ICPConfig(
        countries=list(icp.countries or []),
        industries_any=list(icp.industries_any or []),
        employees_min=icp.employees_min,
        employees_max=icp.employees_max,
        revenue_min_eur=_int(icp.revenue_min_eur),
        nice_to_have=[ai.Criterion.model_validate(c) for c in criteria],
    )


def rule_configs(rules: list[DisqualificationRule]) -> list[ai.RuleConfig]:
    out = []
    for r in rules:
        if not r.is_active:
            continue
        try:
            out.append(
                ai.RuleConfig(
                    id=r.id,
                    name=r.name,
                    kind=r.kind,
                    condition=dict(r.condition or {}),
                    action=r.action,
                    cap_value=float(r.cap_value) if r.cap_value is not None else None,
                )
            )
        except ValidationError as e:  # an invalid rule must not break scoring of every company
            log.warning("rule_skipped_invalid", rule_id=str(r.id), name=r.name, error=str(e))
    return out


def scoring_profile(profile: ScoringProfile | None, service_id: UUID) -> ai.ScoringProfile:
    """Known parameters override the defaults; unknown or invalid ones are ignored with a warning."""
    if profile is None:
        return ai.ScoringProfile(id=service_id, version=1)
    params = {k: v for k, v in (profile.params or {}).items() if k in _SCORING_FIELDS}
    try:
        return ai.ScoringProfile(id=profile.id, version=profile.version, **params)
    except ValidationError as e:
        log.warning("scoring_params_invalid", profile_id=str(profile.id), error=str(e))
        return ai.ScoringProfile(id=profile.id, version=profile.version)


def service_bundle(
    service: Service,
    questions: list[SignalQuestion],
    icp: ICPProfile | None,
    rules: list[DisqualificationRule],
    profile: ScoringProfile | None,
) -> ai.ServiceBundle:
    return ai.ServiceBundle(
        service_id=service.id,
        key=service.slug,
        name=service.name,
        description=service.description,
        value_proposition=service.value_proposition or "",
        questions=[question_config(q) for q in questions if q.is_active],
        icp=icp_config(icp),
        rules=rule_configs(rules),
        scoring=scoring_profile(profile, service.id),
    )


# --- documents and signals ---------------------------------------------------------------------


def document_row(company_id: UUID, org_id: UUID, doc: parser.Document) -> dict[str, Any]:
    return {
        "company_id": company_id,
        "org_id": org_id,
        "source_type": doc.source_type,
        "source_name": doc.source_name,
        "url": doc.url,
        "canonical_url": doc.canonical_url,
        "title": doc.title,
        "text": doc.text,
        "published_at": doc.published_at,
        "fetched_at": doc.fetched_at,
        "language": doc.language,
        "content_hash": doc.content_hash,
        "meta": doc.meta or {},
    }


def analysis_document(row: Document) -> ai.AnalysisDocument:
    return ai.AnalysisDocument(
        id=row.id,
        source_type=row.source_type if row.source_type in _SOURCE_TYPES else "manual",
        source_name=row.source_name,
        url=row.url,
        title=row.title,
        text=row.text,
        published_at=row.published_at,
        fetched_at=row.fetched_at or datetime.now(UTC),
        language=row.language,
        meta=dict(row.meta or {}),
    )


def signal_row(
    signal: ai.VerifiedSignal, company_id: UUID, service_id: UUID, org_id: UUID, run_id: UUID | None
) -> Signal:
    return Signal(
        org_id=org_id,
        company_id=company_id,
        service_id=service_id,
        run_id=run_id,
        question_id=signal.question_id,
        question_key=signal.question_key,
        question_version=signal.question_version,
        document_id=signal.document_id,
        chunk_id=signal.chunk_id,
        category=signal.category,
        polarity=signal.polarity,
        quote=signal.quote,
        quote_start=signal.quote_start,
        quote_end=signal.quote_end,
        summary=signal.summary,
        strength=signal.strength,
        confidence=Decimal(str(round(signal.confidence, 4))),
        reliability=Decimal(str(round(signal.reliability, 4))),
        event_date=signal.event_date,
        published_at=signal.published_at,
        url=signal.url,
        source_type=signal.source_type,
        source_name=signal.source_name,
        flags=sorted(signal.flags),
        status="active",
        model=signal.model,
        prompt_version=signal.prompt_version,
    )


def stored_signal(row: Signal) -> ai.StoredSignal:
    return ai.StoredSignal(
        id=row.id,
        detected_at=row.detected_at,
        status="active",
        question_id=row.question_id,
        question_key=row.question_key,
        question_version=row.question_version,
        category=row.category,
        polarity=row.polarity,
        document_id=row.document_id or row.id,
        chunk_id=row.chunk_id,
        url=row.url or "",
        source_type=row.source_type if row.source_type in _SOURCE_TYPES else "manual",
        source_name=row.source_name,
        quote=row.quote,
        quote_start=row.quote_start,
        quote_end=row.quote_end,
        summary=row.summary,
        strength=row.strength,
        confidence=float(row.confidence),
        reliability=float(row.reliability) if row.reliability is not None else 0.8,
        event_date=row.event_date,
        published_at=row.published_at,
        flags={
            f
            for f in (row.flags or [])
            if f in {"fuzzy_quote", "headline_only", "undated", "corroborated", "derived"}
        },
        model=row.model,
        prompt_version=row.prompt_version,
    )


def lead_score_row(score: ai.LeadScore, org_id: UUID) -> LeadScore:
    return LeadScore(
        org_id=org_id,
        company_id=score.company_id,
        service_id=score.service_id,
        scoring_profile_id=score.scoring_profile_id,
        fit=Decimal(str(score.fit)),
        intent=Decimal(str(score.intent)),
        risk=Decimal(str(score.risk)),
        priority=Decimal(str(score.priority)),
        tier=score.tier,
        disqualified=score.disqualified,
        rule_hits=score.rule_hits,
        fit_details={"criteria": score.fit_details},
        breakdown=[c.model_dump(mode="json") for c in score.breakdown],
        why_now=[r.model_dump(mode="json") for r in score.why_now],
        data_gaps=score.data_gaps,
        computed_at=score.computed_at,
        is_current=True,
    )
