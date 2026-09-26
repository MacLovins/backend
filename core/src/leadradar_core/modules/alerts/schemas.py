import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from leadradar_core.modules.leads.trends import TrendKind
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Channel = Literal["inapp", "email"]
TriggerKind = Literal["signal", "tier", "jobs_threshold"]
Strength = Literal["weak", "moderate", "strong"]  # temperature: cold / medium / hot
Polarity = Literal["positive", "negative"]
Tier = Literal["hot", "warm", "cold", "disqualified"]
EmailFrequency = Literal["instant", "twice_daily", "daily"]
# categories of the signal questions (ai.SIGNAL_CATEGORIES; test_alerts keeps them in step)
SignalCategory = Literal[
    "cost_efficiency",
    "digital_transformation",
    "ai_automation",
    "hiring",
    "leadership_change",
    "shared_services",
    "tech_stack",
    "tech_partners",
    "incident",
    "compliance",
    "investment",
    "expansion",
    "internal_capability",
    "distress",
]

DEFAULT_JOBS_WINDOW_H = 24
MAX_JOBS_WINDOW_H = 24 * 30
_ISO2 = re.compile(r"[A-Z]{2}")


class RuleScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_ids: list[UUID] | None = None  # null = all companies
    service_ids: list[UUID] | None = None  # null = all services
    # firmographics like an ICP (null or empty = any): a company whose country or size is unknown is outside
    # a scope that limits them
    countries: list[str] | None = None  # ISO-2 codes
    industries: list[str] | None = None  # industry ids (any of them)
    employees_min: int | None = Field(default=None, ge=0)
    employees_max: int | None = Field(default=None, ge=0)

    @field_validator("countries")
    @classmethod
    def _iso2(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        codes = [c.strip().upper() for c in value]
        invalid = [c for c in codes if not _ISO2.fullmatch(c)]
        if invalid:
            raise ValueError(f"countries must be ISO-2 codes, got {invalid}")
        return list(dict.fromkeys(codes))

    @model_validator(mode="after")
    def _employees_range(self) -> "RuleScope":
        lo, hi = self.employees_min, self.employees_max
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("employees_min must be <= employees_max")
        return self


class RuleTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: TriggerKind
    # signal: trend kinds (null = any signal), polarity and minimum strength
    categories: list[TrendKind] | None = None
    polarity: Polarity | None = None
    min_strength: Strength | None = None
    # signal: minimum strength (temperature) per signal category of the question; when set, only signals of
    # the listed categories fire (null or empty = no per-category levels)
    levels: dict[SignalCategory, Strength] | None = None
    # tier: the tiers a lead must move into (null = any tier change)
    tier_to: list[Tier] | None = None
    # jobs_threshold: at least jobs_min job postings within jobs_window_h hours
    jobs_min: int | None = Field(default=None, ge=1)
    jobs_window_h: int | None = Field(default=None, ge=1, le=MAX_JOBS_WINDOW_H)

    @model_validator(mode="after")
    def _kind_fields(self) -> "RuleTrigger":
        if not self.levels:
            self.levels = None
        if self.kind == "jobs_threshold":
            if self.jobs_min is None:
                raise ValueError("jobs_min is required for a jobs_threshold trigger")
            if self.jobs_window_h is None:
                self.jobs_window_h = DEFAULT_JOBS_WINDOW_H
        elif self.jobs_min is not None or self.jobs_window_h is not None:
            raise ValueError("jobs_min and jobs_window_h apply to jobs_threshold triggers only")
        if self.kind != "tier" and self.tier_to is not None:
            raise ValueError("tier_to applies to tier triggers only")
        if self.kind != "signal" and (self.categories or self.polarity or self.min_strength or self.levels):
            raise ValueError("categories, polarity, min_strength and levels apply to signal triggers only")
        return self


class AlertRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    is_active: bool = True
    channels: list[Channel] = Field(default_factory=lambda: ["inapp"], min_length=1)
    scope: RuleScope = Field(default_factory=RuleScope)
    trigger: RuleTrigger
    # e-mail channel: one e-mail per notification, or a digest (APP_ALERTS_DIGEST_CRON)
    email_frequency: EmailFrequency = "instant"


class AlertRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None
    channels: list[Channel] | None = Field(default=None, min_length=1)
    scope: RuleScope | None = None
    trigger: RuleTrigger | None = None
    email_frequency: EmailFrequency | None = None


class AlertRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    name: str
    is_active: bool
    channels: list[Channel]
    scope: RuleScope
    trigger: RuleTrigger
    email_frequency: EmailFrequency = "instant"
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
    # signal notifications: the signal's category and strength (temperature); null for the other kinds
    category: str | None = None
    strength: Strength | None = None
    title: str
    body: str
    url: str
    created_at: datetime
    read_at: datetime | None
    delivered: dict[str, Any]  # {"email": "sent" | "failed" | "skipped" | "queued"}


class NotificationPreview(BaseModel):
    """A notification the rule would have produced, rendered but not stored."""

    company_id: UUID
    service_id: UUID | None
    kind: TriggerKind
    trend_kind: TrendKind | None = None
    category: str | None = None
    strength: Strength | None = None
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
