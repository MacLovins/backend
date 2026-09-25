"""Contracts of leadradar-ai (SPEC §1.4.2).

Fields are only ever added, never removed or renamed: core maps these models in
`leadradar_core/adapters/mapping.py`. Any change goes through ARCHITECTURE §4.7.
All datetimes are timezone-aware UTC.
"""

from datetime import date
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

SourceType = Literal["news", "website", "jobs", "report", "registry", "incident", "derived", "manual"]
Weight = Literal["high", "medium", "low"]
Polarity = Literal["positive", "negative"]
Strength = Literal["weak", "moderate", "strong"]
Answer = Literal["yes", "no", "unclear"]
Tier = Literal["hot", "warm", "cold", "disqualified"]
Stage = Literal[
    "resolving",
    "collecting",
    "indexing",
    "prefiltering",
    "extracting",
    "verifying",
    "scoring",
    "done",
    "failed",
    "paused",
]
StageStatus = Literal["started", "progress", "done", "failed", "paused"]
SignalFlag = Literal["fuzzy_quote", "headline_only", "undated", "corroborated", "derived"]
RejectReason = Literal["quote_not_found", "wrong_subject", "stale", "below_confidence", "no_evidence_for_yes"]
LLMPurpose = Literal[
    "extract_signals", "expand_question", "suggest_questions", "classify_industry", "outreach"
]
LLMCallStatus = Literal["ok", "cache_hit", "rate_limited", "error", "invalid_output", "blocked"]
_Date = date  # alias: Reason has a field named `date` that shadows the type in its class body


class Contract(BaseModel):
    """Base for all contracts: unknown fields are a mapping bug, not data."""

    model_config = ConfigDict(extra="forbid")


# --- Company and configuration -------------------------------------------------------------------


class CompanyProfile(Contract):
    id: UUID
    name: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    aliases: list[str] = []
    own_domains: list[str] = []  # company's own domains and its ATS hosts — used by the entity filter
    country_code: str | None = None
    industry_ids: list[str] = []
    employees: int | None = Field(default=None, ge=0)
    revenue_eur: int | None = Field(default=None, ge=0)
    tags: list[str] = []


class QuestionConfig(Contract):
    id: UUID
    key: str = Field(min_length=1)
    version: int = Field(ge=1)
    text: str = Field(min_length=1)
    category: str
    polarity: Polarity
    weight: Weight
    source_types: set[SourceType] = Field(min_length=1)
    recency_days: int = Field(gt=0)
    keywords: dict[str, list[str]] = {}  # {"en": [...], "de": [...]}
    job_titles: list[str] = []
    negative_terms: list[str] = []


class Criterion(Contract):
    """Nice-to-have ICP criterion."""

    kind: Literal["country_in", "industry_in", "employees_between", "revenue_at_least", "tag_in"]
    values: list[str | int]
    weight: float = Field(default=1.0, gt=0)


class ICPConfig(Contract):
    """Must-have fields filter (empty / None = any); nice_to_have is weighted."""

    countries: list[str] = []
    industries_any: list[str] = []
    employees_min: int | None = Field(default=None, ge=0)
    employees_max: int | None = Field(default=None, ge=0)
    revenue_min_eur: int | None = Field(default=None, ge=0)
    nice_to_have: list[Criterion] = []

    @model_validator(mode="after")
    def _employees_range(self) -> Self:
        lo, hi = self.employees_min, self.employees_max
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("employees_min must be <= employees_max")
        return self


class FirmographicCondition(Contract):
    field: Literal["employees", "revenue_eur", "country_code", "industry_ids", "domain", "tags"]
    op: Literal["lt", "gt", "eq", "in", "not_in", "intersects"]
    value: int | float | str | list[str | int]

    @model_validator(mode="after")
    def _op_matches_value(self) -> Self:
        numeric = self.field in ("employees", "revenue_eur") and isinstance(self.value, int | float)
        if self.op in ("lt", "gt") and not numeric:
            raise ValueError("lt/gt need a numeric field (employees, revenue_eur) and a number")
        if self.op in ("in", "not_in", "intersects") and not isinstance(self.value, list):
            raise ValueError(f"op '{self.op}' needs a list value")
        return self


class SignalCondition(Contract):
    question_key: str = Field(min_length=1)
    min_strength: float = Field(ge=0, le=1)  # compared with s_q of the question


class ListCondition(Contract):
    domains: list[str] = Field(min_length=1)  # existing clients, competitors


_CONDITIONS: dict[str, type[Contract]] = {
    "firmographic": FirmographicCondition,
    "signal": SignalCondition,
    "list": ListCondition,
}


class RuleConfig(Contract):
    id: UUID
    name: str
    kind: Literal["firmographic", "signal", "list"]
    condition: dict  # shape per kind (SPEC §1.7.5) — validated by the *Condition models above
    action: Literal["exclude", "cap", "flag"]
    cap_value: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.action == "cap" and self.cap_value is None:
            raise ValueError("action 'cap' requires cap_value")
        _CONDITIONS[self.kind].model_validate(self.condition)
        return self

    def parsed_condition(self) -> "FirmographicCondition | SignalCondition | ListCondition":
        return _CONDITIONS[self.kind].model_validate(self.condition)  # type: ignore[return-value]


class ScoringProfile(Contract):
    """Versioned parameters of the scoring formula; defaults are ARCHITECTURE §3.4."""

    id: UUID
    version: int = Field(ge=1)
    weights: dict[Weight, float] = {"high": 3, "medium": 2, "low": 1}
    strength_values: dict[Strength, float] = {"weak": 0.35, "moderate": 0.65, "strong": 1.0}
    reliability: dict[str, float] = {
        "website": 1.0,
        "report": 1.0,
        "jobs": 0.9,
        "incident": 0.9,
        "registry": 0.9,
        "news": 0.8,
        "derived": 0.7,
        "headline_only": 0.6,
    }
    half_life_days: dict[str, int | None] = {
        "jobs": 45,
        "news": 120,
        "website": 240,
        "report": 365,
        "incident": 270,
        "registry": None,
        "derived": None,
    }
    tau_intent: float = Field(default=3.0, gt=0)
    tau_risk: float = Field(default=2.0, gt=0)
    fit_exponent: float = Field(default=0.4, ge=0)
    intent_exponent: float = Field(default=0.6, ge=0)
    risk_penalty: float = Field(default=0.5, ge=0, le=1)
    tiers: dict[str, float] = {"hot": 65, "warm": 40}
    min_confidence: float = Field(default=0.5, ge=0, le=1)
    max_evidence_per_question: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _complete_and_consistent(self) -> Self:
        if set(self.weights) != {"high", "medium", "low"}:
            raise ValueError("weights must define high, medium and low")
        if set(self.strength_values) != {"weak", "moderate", "strong"}:
            raise ValueError("strength_values must define weak, moderate and strong")
        if set(self.tiers) != {"hot", "warm"} or self.tiers["hot"] < self.tiers["warm"]:
            raise ValueError("tiers must define hot >= warm")
        if any(v <= 0 for v in self.half_life_days.values() if v is not None):
            raise ValueError("half_life_days must be positive or None")
        return self


class ServiceBundle(Contract):
    """Everything needed to analyse and score companies for one service."""

    service_id: UUID
    key: str
    name: str
    description: str
    value_proposition: str = ""
    questions: list[QuestionConfig]
    icp: ICPConfig
    rules: list[RuleConfig]
    scoring: ScoringProfile


# --- Documents and snippets ---------------------------------------------------------------------


class AnalysisDocument(Contract):
    id: UUID
    source_type: SourceType
    source_name: str
    url: str
    title: str | None
    text: str
    published_at: AwareDatetime | None
    fetched_at: AwareDatetime
    language: str | None
    meta: dict = {}


class ChunkIn(Contract):
    """Chunk to persist after indexing (store assigns nothing — ids are generated here)."""

    id: UUID
    document_id: UUID
    company_id: UUID
    ord: int = Field(ge=0)
    text: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    embedding: list[float]


class Snippet(Contract):
    id: str  # short id for the prompt: "S12"
    chunk_id: UUID
    document_id: UUID
    text: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    source_type: SourceType
    source_name: str
    url: str
    title: str | None
    published_at: AwareDatetime | None
    fetched_at: AwareDatetime  # recency fallback when published_at is unknown (flag "undated")
    language: str | None
    meta: dict = {}  # document meta, e.g. headline_only
    embedding: list[float] | None = None


class CollectRequest(Contract):
    source_types: set[SourceType]
    since: AwareDatetime
    news_topics: list[str] = []  # keywords of positive questions → focus of news queries
    job_keywords: list[str] = []  # job titles and skills → ATS search (Workday, Adzuna)
    max_items_per_source: int = Field(default=50, ge=1)


# --- Signals and scores -------------------------------------------------------------------------


class VerifiedSignal(Contract):
    question_id: UUID
    question_key: str
    question_version: int
    category: str
    polarity: Polarity
    document_id: UUID
    chunk_id: UUID | None
    url: str
    source_type: SourceType
    source_name: str
    quote: str
    quote_start: int | None  # offsets in AnalysisDocument.text
    quote_end: int | None
    summary: str
    strength: Strength
    confidence: float = Field(ge=0, le=1)
    reliability: float = Field(ge=0, le=1)
    event_date: date | None
    published_at: AwareDatetime | None
    flags: set[SignalFlag] = set()
    model: str | None
    prompt_version: str | None


class StoredSignal(VerifiedSignal):
    id: UUID
    detected_at: AwareDatetime
    status: Literal["active", "rejected_by_user"] = "active"


class RejectedEvidence(Contract):
    question_id: UUID
    snippet_id: str
    quote: str
    reason: RejectReason


class Contribution(Contract):
    question_id: UUID
    key: str
    label: str
    polarity: Polarity
    weight: float
    strength: float  # s_q, 0..1
    points: float
    signal_ids: list[UUID]


class Reason(Contract):
    text: str
    polarity: Polarity | Literal["fit", "data_gap"]
    signal_id: UUID | None = None
    source_name: str | None = None
    url: str | None = None
    date: _Date | None = None


class LeadScore(Contract):
    company_id: UUID
    service_id: UUID
    scoring_profile_id: UUID
    fit: float = Field(ge=0, le=100)
    intent: float = Field(ge=0, le=100)
    risk: float = Field(ge=0, le=100)
    priority: float = Field(ge=0, le=100)
    tier: Tier
    disqualified: bool
    rule_hits: list[dict]
    fit_details: list[dict]
    breakdown: list[Contribution]
    why_now: list[Reason]
    data_gaps: list[str]
    computed_at: AwareDatetime


class FitResult(Contract):
    """Result of fit_score: used by score_company and by discovery (ranking candidates)."""

    fit: float = Field(ge=0, le=100)
    must_have_passed: bool
    details: list[dict]  # {"criterion", "required", "status": pass|fail|unknown|match|no_match, "label"}
    data_gaps: list[str]  # company fields that are unknown: "employees", "industry_ids", …


class ScoreChange(Contract):
    """Returned by AnalysisStore.save_score: tier before/after for domain events."""

    company_id: UUID
    service_id: UUID
    priority_before: float | None
    priority_after: float
    tier_before: Tier | None
    tier_after: Tier

    @property
    def tier_changed(self) -> bool:
        return self.tier_before != self.tier_after


# --- Runs, progress, LLM usage ------------------------------------------------------------------


class ProgressEvent(Contract):
    run_id: UUID
    company_id: UUID
    service_id: UUID | None = None
    stage: Stage
    status: StageStatus
    message: str
    data: dict = {}


class LLMCallRecord(Contract):
    run_id: UUID | None = None
    purpose: LLMPurpose
    model: str
    prompt_version: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    cache_hit: bool = False
    status: LLMCallStatus
    error: str | None = None


class AnalysisInput(Contract):
    run_id: UUID
    company: CompanyProfile
    services: list[ServiceBundle] = Field(min_length=1)
    mode: Literal["full", "incremental"] = "incremental"  # incremental: no LLM if nothing new to check
    now: AwareDatetime


class StepError(Contract):
    stage: Stage
    service_id: UUID | None = None
    error_type: str
    message: str


class RunStats(Contract):
    documents_by_source: dict[str, int] = {}
    snippets_indexed: int = 0
    llm_calls: int = 0
    llm_cache_hits: int = 0
    signals_verified: int = 0
    evidence_rejected: int = 0
    extraction_skipped_services: int = 0  # fingerprint unchanged
    duration_ms_by_stage: dict[str, int] = {}
