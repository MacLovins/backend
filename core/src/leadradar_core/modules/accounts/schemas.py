from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CompanyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    domain: str = Field(..., min_length=1, max_length=255)
    country_code: str | None = None
    industry_ids: list[str] = []
    employees: int | None = None
    revenue_eur: Decimal | None = None
    hq_city: str | None = None
    homepage_url: str | None = None
    careers_url: str | None = None
    newsroom_url: str | None = None
    linkedin_url: str | None = None
    notes: str | None = None
    tags: list[str] = []


class CompanyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    country_code: str | None = None
    industry_ids: list[str] | None = None
    employees: int | None = None
    revenue_eur: Decimal | None = None
    hq_city: str | None = None
    homepage_url: str | None = None
    careers_url: str | None = None
    newsroom_url: str | None = None
    linkedin_url: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    is_tracked: bool | None = None


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    name: str
    domain: str
    aliases: list[str]
    own_domains: list[str]
    country_code: str | None
    industry_ids: list[str]
    employees: int | None
    revenue_eur: Decimal | None
    hq_city: str | None
    wikidata_qid: str | None
    lei: str | None
    crunchbase_id: str | None
    homepage_url: str | None
    careers_url: str | None
    newsroom_url: str | None
    ats: dict[str, Any] | None
    linkedin_url: str | None
    notes: str | None
    tags: list[str]
    origin: str
    is_tracked: bool
    resolved_at: datetime | None
    last_analyzed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ImportDuplicateOut(BaseModel):
    """A row whose domain already appeared earlier in the same file."""

    row: int
    first_row: int
    domain: str
    action: Literal["merge", "skip"]


class CompanyImportReport(BaseModel):
    created: int = 0
    updated: int = 0  # existing companies where at least one empty field was filled
    skipped: int = 0  # rows that created or changed nothing (invalid, duplicate, no new data)
    errors: list[str] = []
    total_rows: int = 0
    warnings: list[str] = []  # row-level issues that did not block the row (unknown country, industry...)
    duplicates: list[ImportDuplicateOut] = []


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    source_type: str
    source_name: str
    url: str
    canonical_url: str
    title: str | None
    published_at: datetime | None
    fetched_at: datetime
    language: str | None
