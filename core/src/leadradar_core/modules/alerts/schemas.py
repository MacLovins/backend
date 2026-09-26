from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from leadradar_core.modules.leads.trends import TrendKind
from pydantic import BaseModel, ConfigDict, Field, model_validator

Channel = Literal["inapp", "email"]
TriggerKind = Literal["signal", "tier", "jobs_threshold"]
Strength = Literal["weak", "moderate", "strong"]
Polarity = Literal["positive", "negative"]
Tier = Literal["hot", "warm", "cold", "disqualified"]

DEFAULT_JOBS_WINDOW_H = 24
MAX_JOBS_WINDOW_H = 24 * 30


class RuleScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_ids: list[UUID] | None = None  # null = all companies
    service_ids: list[UUID] | None = None  # null = all services


class RuleTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: TriggerKind
    # signal: trend kinds (null = any signal), polarity and minimum strength
    categories: list[TrendKind] | None = None
    polarity: Polarity | None = None
    min_strength: Strength | None = None
    # tier: the tiers a lead must move into (null = any tier change)
    tier_to: list[Tier] | None = None
    # jobs_threshold: at least jobs_min job postings within jobs_window_h hours
    jobs_min: int | None = Field(default=None, ge=1)
    jobs_window_h: int | None = Field(default=None, ge=1, le=MAX_JOBS_WINDOW_H)

    @model_validator(mode="after")
    def _kind_fields(self) -> "RuleTrigger":
        if self.kind == "jobs_threshold":
            if self.jobs_min is None:
                raise ValueError("jobs_min is required for a jobs_threshold trigger")
            if self.jobs_window_h is None:
                self.jobs_window_h = DEFAULT_JOBS_WINDOW_H
        elif self.jobs_min is not None or self.jobs_window_h is not None:
            raise ValueError("jobs_min and jobs_window_h apply to jobs_threshold triggers only")
        if self.kind != "tier" and self.tier_to is not None:
            raise ValueError("tier_to applies to tier triggers only")
        if self.kind != "signal" and (self.categories or self.polarity or self.min_strength):
            raise ValueError("categories, polarity and min_strength apply to signal triggers only")
        return self


class AlertRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    is_active: bool = True
    channels: list[Channel] = Field(default_factory=lambda: ["inapp"], min_length=1)
    scope: RuleScope = Field(default_factory=RuleScope)
    trigger: RuleTrigger


class AlertRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None
    channels: list[Channel] | None = Field(default=None, min_length=1)
    scope: RuleScope | None = None
    trigger: RuleTrigger | None = None


class AlertRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    name: str
    is_active: bool
    channels: list[Channel]
    scope: RuleScope
    trigger: RuleTrigger
    created_at: datetime
    updated_at: datetime


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    rule_id: UUID | None
    event_id: UUID | None
    company_id: UUID
    service_id: UUID | None
    kind: TriggerKind
    trend_kind: TrendKind | None = None
    title: str
    body: str
    url: str
    created_at: datetime
    read_at: datetime | None
    delivered: dict[str, Any]


class NotificationPreview(BaseModel):
    """A notification the rule would have produced, rendered but not stored."""

    company_id: UUID
    service_id: UUID | None
    kind: TriggerKind
    trend_kind: TrendKind | None = None
    title: str
    body: str
    url: str
    occurred_at: datetime


class RulePreviewOut(BaseModel):
    count: int
    notifications: list[NotificationPreview] = []


class UnreadCountOut(BaseModel):
    count: int


class ReadAllOut(BaseModel):
    updated: int
