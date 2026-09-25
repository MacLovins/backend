from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from leadradar_core.db.base import Base, CoreTableMixin
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class LeadScore(Base, CoreTableMixin):
    __tablename__ = "lead_score"
    __table_args__ = (
        Index(
            "idx_lead_score_service_priority_current",
            "org_id",
            "service_id",
            "priority",
            postgresql_where=text("is_current = true"),
        ),
        Index("idx_lead_score_company_service", "company_id", "service_id"),
        {"schema": "core"},
    )

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False
    )
    scoring_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.scoring_profile.id", ondelete="CASCADE"), nullable=False
    )
    fit: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    intent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    risk: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    priority: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)  # hot / warm / cold
    disqualified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rule_hits: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    fit_details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    breakdown: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    why_now: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    data_gaps: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
