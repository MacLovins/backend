from datetime import datetime
from typing import Any

from leadradar_core.db.base import Base, CoreTableMixin
from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class DomainEvent(Base, CoreTableMixin):
    __tablename__ = "domain_event"
    __table_args__ = (
        Index(
            "idx_domain_event_unprocessed",
            "created_at",
            postgresql_where=(mapped_column("processed_at") == None),  # noqa: E711
        ),
        {"schema": "core"},
    )

    type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
