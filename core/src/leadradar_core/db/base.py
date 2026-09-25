from datetime import UTC, datetime
from uuid import UUID, uuid4

from leadradar_core.settings import settings
from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

core_metadata = MetaData(schema="core")


class Base(DeclarativeBase):
    metadata = core_metadata


class CoreTableMixin:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(nullable=False, default=settings.DEFAULT_ORG_ID)
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
