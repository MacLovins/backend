from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from leadradar_core.db.base import Base, CoreTableMixin
from leadradar_core.settings import settings
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class LLMCall(Base, CoreTableMixin):
    __tablename__ = "llm_call"
    __table_args__ = (
        Index("idx_llm_call_model_created", "model", "created_at"),
        {"schema": "core"},
    )

    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("core.analysis_run.id", ondelete="SET NULL"), nullable=True
    )
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="success", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class LLMCache(Base):
    __tablename__ = "llm_cache"
    __table_args__ = {"schema": "core"}

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    org_id: Mapped[UUID] = mapped_column(nullable=False, default=settings.DEFAULT_ORG_ID)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )
