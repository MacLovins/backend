from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SourceType = Literal["news", "website", "jobs", "report", "registry", "incident"]
AtsKind = Literal[
    "greenhouse", "lever", "workday", "personio", "ashby", "smartrecruiters", "workable", "recruitee"
]
ErrorKind = Literal["rate_limited", "blocked", "robots", "not_found", "timeout", "parse_error", "disabled"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AtsRef(Contract):
    kind: AtsKind
    token: str
    host: str | None = None
    site: str | None = None


class CompanyRef(Contract):
    name: str
    domain: str
    country_code: str | None = None
    aliases: list[str] = Field(default_factory=list)
    careers_url: str | None = None
    newsroom_url: str | None = None
    ats: AtsRef | None = None
    wikidata_qid: str | None = None

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        value = value.strip().lower().removeprefix("https://").removeprefix("http://")
        return value.split("/", 1)[0].removeprefix("www.")

    @field_validator("country_code")
    @classmethod
    def uppercase_country(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class Firmographics(Contract):
    legal_name: str | None = None
    country_code: str | None = None
    hq_city: str | None = None
    industry_ids: list[str] = Field(default_factory=list)
    employees: int | None = None
    revenue_eur: int | None = None
    founded: int | None = None
    lei: str | None = None
    wikidata_qid: str | None = None
    crunchbase_id: str | None = None
    ceo: str | None = None
    source: str


class ResolvedCompany(CompanyRef):
    homepage_url: str
    own_domains: list[str]
    firmographics: Firmographics | None = None
    resolved_at: datetime
    notes: list[str] = Field(default_factory=list)


class CollectPlan(Contract):
    source_types: set[SourceType]
    since: datetime
    news_topics: list[str] = Field(default_factory=list)
    job_keywords: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["en", "de", "fr"])
    max_items_per_source: int = Field(default=50, ge=1, le=500)
    max_website_pages: int = Field(default=25, ge=1, le=100)
    time_budget_s: int = Field(default=90, ge=1, le=900)

    @field_validator("since")
    @classmethod
    def ensure_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Document(Contract):
    source_type: SourceType
    source_name: str
    url: str
    canonical_url: str
    title: str | None = None
    text: str
    published_at: datetime | None = None
    fetched_at: datetime
    language: str | None = None
    content_hash: str
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("published_at", "fetched_at")
    @classmethod
    def ensure_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SourceError(Contract):
    adapter: str
    kind: ErrorKind
    message: str
    retry_after_s: int | None = None


class CollectResult(Contract):
    documents: list[Document]
    errors: list[SourceError]
    stats: dict[str, int]
    duration_ms: int


class DiscoveryQuery(Contract):
    countries: list[str]
    industries: list[str]
    employees_min: int | None = None
    employees_max: int | None = None
    limit: int = Field(default=100, ge=1, le=500)
    exclude_domains: list[str] = Field(default_factory=list)


class CompanyCandidate(Contract):
    name: str
    domain: str
    country_code: str | None
    industry_ids: list[str]
    employees: int | None
    revenue_eur: int | None
    wikidata_qid: str
    lei: str | None
    crunchbase_id: str | None
    source: Literal["wikidata"] = "wikidata"


class RateLimit(Contract):
    requests: int = Field(ge=1)
    per_seconds: float = Field(gt=0)
    scope: Literal["global", "host"] = "host"


class AdapterInfo(Contract):
    id: str
    source_type: SourceType
    enabled: bool
    requires_key: str | None
    rate_limit: RateLimit


class Industry(Contract):
    id: str
    label: str
    wikidata: list[str]
    # Classes matched through P31 "instance of" (e.g. airline Q46970), in addition to P452 industry values.
    wikidata_classes: list[str] = Field(default_factory=list)
    nace: list[str]
    nis2: str | None = None
    dora: bool = False


class Country(Contract):
    code: str
    label: str
    wikidata_qid: str
    languages: list[str]
    is_eu: bool
