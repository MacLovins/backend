from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
    category: str = "ai_automation"
    polarity: str = "positive"  # positive / negative
    weight: str = "medium"  # high / medium / low
    source_types: list[str] = ["website", "news", "jobs"]
    recency_days: int = 180
    job_titles: list[str] = []
    negative_terms: list[str] = []


class SignalQuestionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    category: str | None = None
    polarity: str | None = None
    weight: str | None = None
    source_types: list[str] | None = None
    recency_days: int | None = None
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
    employees_min: int | None = None
    employees_max: int | None = None
    revenue_min_eur: Decimal | None = None
    nice_to_have: dict[str, Any] | None = None


class ICPProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_id: UUID
    countries: list[str]
    industries_any: list[str]
    employees_min: int | None
    employees_max: int | None
    revenue_min_eur: Decimal | None
    nice_to_have: dict[str, Any] | None
    version: int
    created_at: datetime
    updated_at: datetime


# Disqualification Rules
class DisqualificationRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    condition: dict[str, Any]
    action: str = "disqualify"
    cap_value: Decimal | None = None
    is_active: bool = True


class DisqualificationRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    condition: dict[str, Any] | None = None
    action: str | None = None
    cap_value: Decimal | None = None
    is_active: bool | None = None


class DisqualificationRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_id: UUID
    name: str
    kind: str
    condition: dict[str, Any]
    action: str
    cap_value: Decimal | None
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
