from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from leadradar_core.db.base import Base, CoreTableMixin
from leadradar_core.settings import settings
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# Canonical enums (ARCHITECTURE §4.7)
RUN_KINDS = ("analyze", "discover", "rescore", "refresh")
RUN_STATUSES = ("queued", "running", "succeeded", "partial", "failed", "cancelled")
RUN_ACTIVE_STATUSES = ("queued", "running")
RUN_TERMINAL_STATUSES = ("succeeded", "partial", "failed", "cancelled")
RUN_MODES = ("incremental", "full")


class AnalysisRun(Base, CoreTableMixin):
    __tablename__ = "analysis_run"
    __table_args__ = (
        Index("idx_analysis_run_org_created", "org_id", "created_at"),
        {"schema": "core"},
    )

    kind: Mapped[str] = mapped_column(String(32), default="analyze", nullable=False)  # RUN_KINDS
    status: Mapped[str] = mapped_column(
        String(32), default="queued", server_default="queued", nullable=False
    )  # RUN_STATUSES
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    progress: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=lambda: {"done": 0, "total": 0, "failed": 0, "paused": 0},
        server_default='{"done": 0, "total": 0, "failed": 0, "paused": 0}',
    )
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunEvent(Base):
    __tablename__ = "run_event"
    __table_args__ = (
        Index("idx_run_event_run_id", "run_id", "id"),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(start=1), primary_key=True)
    org_id: Mapped[UUID] = mapped_column(nullable=False, default=settings.DEFAULT_ORG_ID)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.analysis_run.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("core.company.id", ondelete="SET NULL"), nullable=True
    )
    service_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("core.service.id", ondelete="SET NULL"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
