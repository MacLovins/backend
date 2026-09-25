from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "analyze"  # analyze / refresh
    company_ids: list[UUID] = []
    service_ids: list[UUID] = []


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    kind: str
    status: str
    params: dict[str, Any]
    progress: dict[str, Any]
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class RunEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: UUID
    company_id: UUID | None = None
    service_id: UUID | None = None
    stage: str
    status: str
    message: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime
