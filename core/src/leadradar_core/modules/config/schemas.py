from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import leadradar_ai as ai
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator

# Canonical enums (ARCHITECTURE §4.7). They mirror the leadradar_ai contracts (QuestionConfig, RuleConfig);
# test_config_validation checks that they stay identical, the router re-validates through the ai contracts.
Weight = Literal["high", "medium", "low"]
Polarity = Literal["positive", "negative"]
SourceType = Literal["news", "website", "jobs", "report", "registry", "incident", "derived", "manual"]
RuleKind = Literal["firmographic", "signal", "list"]
RuleAction = Literal["exclude", "cap", "flag"]


def _known_category(value: str) -> str:
    if value not in ai.SIGNAL_CATEGORIES:
        raise ValueError(f"unknown category '{value}', expected one of {sorted(ai.SIGNAL_CATEGORIES)}")
    return value


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


Category = Annotated[str, AfterValidator(_known_category)]
SourceTypes = Annotated[list[SourceType], Field(min_length=1), AfterValidator(_unique)]


# Service Schemas
class ServiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    value_proposition: str = ""
    decision_makers: list[str] = []
    is_active: bool = True


class ServiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    value_proposition: str | None = None
    decision_makers: list[str] | None = None
    is_active: bool | None = None


class ServiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    name: str
    slug: str
    description: str
    value_proposition: str
    decision_makers: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime


# Signal Question Schemas
class SignalQuestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=128)
    text: str = Field(..., min_length=1)
    category: Category = "ai_automation"
    polarity: Polarity = "positive"
    weight: Weight = "medium"
    source_types: SourceTypes = ["website", "news", "jobs"]
    recency_days: int = Field(default=180, gt=0)
    job_titles: list[str] = []
    negative_terms: list[str] = []


class SignalQuestionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1)
    category: Category | None = None
    polarity: Polarity | None = None
    weight: Weight | None = None
    source_types: SourceTypes | None = None
    recency_days: int | None = Field(default=None, gt=0)
    job_titles: list[str] | None = None
    negative_terms: list[str] | None = None
    is_active: bool | None = None


class SignalQuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    service_id: UUID
    key: str
    text: str
    category: str
    polarity: str
    weight: str
    source_types: list[str]
    recency_days: int
    keywords: dict[str, Any] | None = None
    job_titles: list[str]
    negative_terms: list[str]
    keywords_status: str
    version: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ICP Schemas
class ICPProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    countries: list[str] = []
    industries_any: list[str] = []
    employees_min: int | None = Field(default=None, ge=0)
    employees_max: int | None = Field(default=None, ge=0)
    revenue_min_eur: Decimal | None = Field(default=None, ge=0)
    # {"criteria": [{"kind": "industry_in", "values": [...], "weight": 2}, ...]} — ai.Criterion items
    nice_to_have: dict[str, Any] | None = None

    @field_validator("nice_to_have")
    @classmethod
    def _nice_to_have(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        unknown = set(value) - {"criteria"}
        if unknown:
            raise ValueError(f"unknown keys {sorted(unknown)}, expected {{'criteria': [...]}}")
        criteria = value.get("criteria") or []
        if not isinstance(criteria, list):
            raise ValueError("'criteria' must be a list")
        try:
            parsed = [ai.Criterion.model_validate(c) for c in criteria]
        except ValidationError as e:
            raise ValueError(
                f"invalid criterion: {e.errors(include_url=False, include_context=False)}"
            ) from e
        return {"criteria": [c.model_dump(mode="json") for c in parsed]}


class ICPProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_id: UUID
    countries: list[str]
    industries_any: list[str]
    employees_min: int | None
    employees_max: int | None
    revenue_min_eur: float | None
    nice_to_have: dict[str, Any] | None
    version: int
    created_at: datetime
    updated_at: datetime


# Disqualification Rules
class DisqualificationRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    kind: RuleKind
    condition: dict[str, Any]
    action: RuleAction  # required: no default that could silently mean something else
    cap_value: Decimal | None = Field(default=None, ge=0, le=100)
    is_active: bool = True


class DisqualificationRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    condition: dict[str, Any] | None = None
    action: RuleAction | None = None
    cap_value: Decimal | None = Field(default=None, ge=0, le=100)
    is_active: bool | None = None


class DisqualificationRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_id: UUID
    name: str
    kind: str
    condition: dict[str, Any]
    action: str
    cap_value: float | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# Scoring Profile Schemas
class ScoringProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: dict[str, Any]


class ScoringProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_id: UUID
    version: int
    params: dict[str, Any]
    is_current: bool
    created_at: datetime
    updated_at: datetime


class RescoreResult(BaseModel):
    version: int
    rescored: int
    tier_changes: int
    duration_ms: int
