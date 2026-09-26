from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class DiscoverySearchIn(BaseModel):
    """The query is built from the service ICP; the optional fields below override it for this search."""

    service_id: UUID
    limit: int = Field(default=20, ge=1, le=100)
    countries: list[str] | None = None  # ISO2 codes or names
    industries: list[str] | None = None  # taxonomy ids or labels
    employees_min: int | None = Field(default=None, ge=0)
    employees_max: int | None = Field(default=None, ge=0)
    country: str | None = None  # single-value shortcut for `countries`
    industry: str | None = None  # single-value shortcut for `industries`
    keywords: list[str] = []  # reserved: the registry search has no keyword filter yet


class DiscoveredCompany(BaseModel):
    name: str
    domain: str
    country_code: str | None = None
    industry_ids: list[str] = []
    employees: int | None = None
    revenue_eur: int | None = None
    wikidata_qid: str | None = None
    lei: str | None = None
    crunchbase_id: str | None = None
    fit_score: float = Field(ge=0, le=100)
    must_have_passed: bool = True
    fit_details: list[dict[str, Any]] = []
    data_gaps: list[str] = []
    already_tracked: bool = False
    reason: str | None = None


class DiscoverySearchOut(BaseModel):
    items: list[DiscoveredCompany]
    total: int
    query: dict[str, Any] = {}  # the effective DiscoveryQuery sent to the parser


class DiscoveryAcceptIn(BaseModel):
    name: str
    domain: str
    country_code: str | None = None
    industry_ids: list[str] = []
    employees: int | None = None
    revenue_eur: int | None = None
    wikidata_qid: str | None = None
    lei: str | None = None
    crunchbase_id: str | None = None
    notes: str | None = None
    tags: list[str] = []
    service_id: UUID | None = None
