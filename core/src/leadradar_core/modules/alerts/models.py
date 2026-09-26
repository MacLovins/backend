"""User-defined alert rules and the in-app notifications they produce.

alert_rule     what a user wants to hear about: a scope (companies, services, market, industry, company
               size) and a trigger (signal kinds and per-category temperature, tier changes, a jobs-postings
               threshold); channels in-app and/or e-mail, e-mails one by one or bundled into a digest.
notification   one delivered match: rendered title, body and deep link; `delivered` records the e-mail outcome.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from leadradar_core.db.base import Base, CoreTableMixin
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

CHANNELS = ("inapp", "email")
TRIGGER_KINDS = ("signal", "tier", "jobs_threshold")
EMAIL_FREQUENCIES = ("instant", "twice_daily", "daily")


class AlertRule(Base, CoreTableMixin):
    __tablename__ = "alert_rule"
    __table_args__ = (
        Index("idx_alert_rule_org_active", "org_id", "is_active"),
        Index("idx_alert_rule_user", "user_id"),
        {"schema": "core"},
    )

    user_id: Mapped[UUID] = mapped_column(nullable=False)  # auth.user_account.id (no FK across schemas)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    channels: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=lambda: ["inapp"], server_default="{inapp}", nullable=False
    )
    # {"company_ids": [uuid] | null, "service_ids": [uuid] | null, "countries", "industries", "employees_min",
    #  "employees_max"}; null = all
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    # {"kind": "signal" | "tier" | "jobs_threshold", "categories", "polarity", "min_strength", "levels",
    #  "tier_to", "jobs_min", "jobs_window_h"}
    trigger: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    # EMAIL_FREQUENCIES: "instant" e-mails each notification, the others queue it for the digest
    email_frequency: Mapped[str] = mapped_column(
        String(16), default="instant", server_default="instant", nullable=False
    )


class Notification(Base, CoreTableMixin):
    __tablename__ = "notification"
    __table_args__ = (
        Index("idx_notification_user_created", "user_id", "created_at"),
        Index(
            "idx_notification_user_unread",
            "user_id",
            postgresql_where=text("read_at IS NULL"),
        ),
        Index("idx_notification_rule_company_created", "rule_id", "company_id", "created_at"),
        Index("idx_notification_rule_event", "rule_id", "event_id"),
        {"schema": "core"},
    )

    user_id: Mapped[UUID] = mapped_column(nullable=False)
    rule_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("core.alert_rule.id", ondelete="SET NULL"), nullable=True
    )
    event_id: Mapped[UUID | None] = mapped_column(
        nullable=True
    )  # core.domain_event.id (no FK: events age out)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("core.service.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # TRIGGER_KINDS
    trend_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # signal notifications: the signal's category and strength (weak / moderate / strong); null otherwise
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strength: Mapped[str | None] = mapped_column(String(16), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # {"email": "sent" | "failed" | "skipped" | "queued"}; queued: waits for the rule's digest
    delivered: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
