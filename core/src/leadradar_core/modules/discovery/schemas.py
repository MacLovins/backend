from uuid import UUID

from pydantic import BaseModel


class DiscoverySearchIn(BaseModel):
    service_id: UUID
    limit: int = 10
    country: str | None = None
    industry: str | None = None
    keywords: list[str] = []


class DiscoveredCompany(BaseModel):
    name: str
    domain: str
    country_code: str | None = None
    industry_ids: list[str] = []
    employees: int | None = None
    fit_score: float = 75.0
    already_tracked: bool = False
    reason: str | None = None


class DiscoverySearchOut(BaseModel):
    items: list[DiscoveredCompany]
    total: int


class DiscoveryAcceptIn(BaseModel):
    name: str
    domain: str
    country_code: str | None = None
    industry_ids: list[str] = []
    employees: int | None = None
    notes: str | None = None
    tags: list[str] = []
    service_id: UUID | None = None
