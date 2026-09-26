from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from leadradar_core.db.base import Base, CoreTableMixin
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column


class Document(Base, CoreTableMixin):
    __tablename__ = "document"
    __table_args__ = (
        UniqueConstraint("company_id", "content_hash", name="uq_document_company_content_hash"),
        Index("idx_document_company_source_pub", "company_id", "source_type", "published_at"),
        {"schema": "core"},
    )

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)  # news/website/jobs/etc.
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class DocumentChunk(Base, CoreTableMixin):
    __tablename__ = "document_chunk"
    __table_args__ = (
        Index("idx_document_chunk_company", "company_id"),
        {"schema": "core"},
    )

    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.document.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False
    )
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)


class ExtractionState(Base, CoreTableMixin):
    __tablename__ = "extraction_state"
    __table_args__ = (
        UniqueConstraint("company_id", "service_id", name="uq_extraction_state_company_service"),
        {"schema": "core"},
    )

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class Signal(Base, CoreTableMixin):
    __tablename__ = "signal"
    __table_args__ = (
        Index("idx_signal_company_service_status", "company_id", "service_id", "status"),
        Index("idx_signal_evidence_key", "company_id", "service_id", "evidence_key"),
        {"schema": "core"},
    )

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.signal_question.id", ondelete="CASCADE"), nullable=False
    )
    question_key: Mapped[str] = mapped_column(String(128), nullable=False)
    question_version: Mapped[int] = mapped_column(Integer, nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("core.document.id", ondelete="SET NULL"), nullable=True
    )
    chunk_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("core.document_chunk.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("core.analysis_run.id", ondelete="SET NULL"), nullable=True
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    polarity: Mapped[str] = mapped_column(String(16), nullable=False)  # positive / negative
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quote_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    strength: Mapped[str] = mapped_column(String(16), nullable=False)  # strong / moderate / weak
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    reliability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    flags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    status: Mapped[str] = mapped_column(
        String(32), default="active", nullable=False
    )  # active / superseded / rejected_by_user
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # sha256 of question key + normalized quote + URL: the same evidence across runs (see evidence.py)
    evidence_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class RejectedEvidence(Base, CoreTableMixin):
    __tablename__ = "rejected_evidence"
    __table_args__ = (
        Index("idx_rejected_evidence_service_reason", "service_id", "reason"),
        {"schema": "core"},
    )

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.signal_question.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("core.analysis_run.id", ondelete="SET NULL"), nullable=True
    )
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
