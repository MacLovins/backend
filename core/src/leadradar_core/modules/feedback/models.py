from uuid import UUID

from leadradar_core.db.base import Base, CoreTableMixin
from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


class Feedback(Base, CoreTableMixin):
    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("user_id", "target_type", "target_id", name="uq_feedback_user_target"),
        Index("idx_feedback_service_verdict", "service_id", "verdict"),
        {"schema": "core"},
    )

    user_id: Mapped[UUID] = mapped_column(nullable=False)  # loose reference, no FK to auth schema
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)  # signal / lead
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)  # correct / incorrect
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
