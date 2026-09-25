from datetime import datetime
from decimal import Decimal
from typing import Any

from leadradar_core.db.base import Base, CoreTableMixin
from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column


class Org(Base, CoreTableMixin):
    __tablename__ = "org"
    __table_args__ = {"schema": "core"}

    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Company(Base, CoreTableMixin):
    __tablename__ = "company"
    __table_args__ = (
        UniqueConstraint("org_id", "domain", name="uq_company_org_domain"),
        Index("idx_company_org_tracked", "org_id", "is_tracked"),
        {"schema": "core"},
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)  # normalized lowercase
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    own_domains: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    industry_ids: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    employees: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revenue_eur: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    hq_city: Mapped[str | None] = mapped_column(String(255), nullable=True)
    wikidata_qid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lei: Mapped[str | None] = mapped_column(String(64), nullable=True)
    crunchbase_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    homepage_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    careers_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    newsroom_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ats: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    origin: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)  # csv/manual/discovery
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
