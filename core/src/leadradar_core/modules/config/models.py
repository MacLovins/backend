from decimal import Decimal
from typing import Any
from uuid import UUID

from leadradar_core.db.base import Base, CoreTableMixin
from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship


class Service(Base, CoreTableMixin):
    __tablename__ = "service"
    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_service_org_slug"),
        {"schema": "core"},
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    value_proposition: Mapped[str] = mapped_column(Text, default="", nullable=False)
    decision_makers: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    questions = relationship("SignalQuestion", back_populates="service", cascade="all, delete-orphan")
    icp = relationship("ICPProfile", back_populates="service", uselist=False, cascade="all, delete-orphan")
    rules = relationship("DisqualificationRule", back_populates="service", cascade="all, delete-orphan")
    scoring_profiles = relationship("ScoringProfile", back_populates="service", cascade="all, delete-orphan")


class SignalQuestion(Base, CoreTableMixin):
    __tablename__ = "signal_question"
    __table_args__ = (
        UniqueConstraint("service_id", "key", name="uq_signal_question_service_key"),
        Index("idx_signal_question_service_active", "service_id", "is_active"),
        {"schema": "core"},
    )

    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    polarity: Mapped[str] = mapped_column(
        String(16), default="positive", nullable=False
    )  # positive / negative
    weight: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)  # high / medium / low
    source_types: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    recency_days: Mapped[int] = mapped_column(Integer, default=180, nullable=False)
    keywords: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    job_titles: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    negative_terms: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    keywords_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )  # pending / ready / failed
    # own cold / medium / hot guide {"weak", "moderate", "strong"} → text; null = the category default
    temperature: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    service = relationship("Service", back_populates="questions")


class ICPProfile(Base, CoreTableMixin):
    __tablename__ = "icp_profile"
    __table_args__ = (
        UniqueConstraint("service_id", name="uq_icp_profile_service"),
        {"schema": "core"},
    )

    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False
    )
    countries: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    industries_any: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    employees_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    employees_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revenue_min_eur: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    nice_to_have: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    service = relationship("Service", back_populates="icp")


class DisqualificationRule(Base, CoreTableMixin):
    __tablename__ = "disqualification_rule"
    __table_args__ = (
        Index("idx_disqualification_rule_service", "service_id"),
        {"schema": "core"},
    )

    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    condition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    cap_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    service = relationship("Service", back_populates="rules")


class ScoringProfile(Base, CoreTableMixin):
    __tablename__ = "scoring_profile"
    __table_args__ = (
        UniqueConstraint("service_id", "version", name="uq_scoring_profile_service_version"),
        Index("idx_scoring_profile_current", "service_id", postgresql_where=text("is_current = true")),
        {"schema": "core"},
    )

    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    service = relationship("Service", back_populates="scoring_profiles")
