from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

RunKindIn = Literal["analyze", "refresh"]
RunMode = Literal["incremental", "full"]


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RunKindIn = "analyze"
    # incremental: documents are deduplicated and the LLM is skipped when a service's fingerprint is unchanged;
    # full: re-collect and re-extract every service, ignoring fingerprints
    mode: RunMode = "incremental"
    company_ids: list[UUID] = Field(min_length=1, max_length=500)
    service_ids: list[UUID] = Field(default=[], description="Empty: every active service of the org")


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
