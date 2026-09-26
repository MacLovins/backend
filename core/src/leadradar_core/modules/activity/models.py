from datetime import datetime
from typing import Any

from leadradar_core.db.base import Base, CoreTableMixin
from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column


class DomainEvent(Base, CoreTableMixin):
    __tablename__ = "domain_event"
    __table_args__ = (
        Index(
            "idx_domain_event_unprocessed",
            "created_at",
            postgresql_where=(mapped_column("processed_at") == None),  # noqa: E711
        ),
        Index("idx_domain_event_org_created", "org_id", "created_at"),
        # GET /activity?company_id= filters on the payload
        Index(
            "idx_domain_event_org_company_created", "org_id", text("(payload->>'company_id')"), "created_at"
        ),
        {"schema": "core"},
    )

    type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # dispatcher bookkeeping: consumers that already handled the event, failed attempts, last failure
    delivered_to: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
