from datetime import datetime
from typing import Any
from uuid import UUID

from leadradar_core.db.base import Base, CoreTableMixin
from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

OUTREACH_STATUSES = ("queued", "running", "succeeded", "failed")


class OutreachJob(Base, CoreTableMixin):
    """One outreach draft generation (CO-A1), executed by the worker; the API only enqueues and reads it."""

    __tablename__ = "outreach_job"
    __table_args__ = (
        Index("idx_outreach_job_company_created", "company_id", "created_at"),
        {"schema": "core"},
    )

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("core.service.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="queued", server_default="queued", nullable=False
    )  # OUTREACH_STATUSES
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
