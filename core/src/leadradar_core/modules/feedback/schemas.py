from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str  # correct / incorrect
    service_id: UUID
    reason: str | None = None


class FeedbackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    target_type: str
    target_id: UUID
    service_id: UUID
    verdict: str
    reason: str | None


class QualityMetricsOut(BaseModel):
    labeled: int = 0
    precision: float = 0.0
    by_category: list[dict[str, Any]] = []
    by_source: list[dict[str, Any]] = []
    verifier: dict[str, Any] = {
        "evidence_total": 0,
        "rejected": {},
    }
    hallucination_rate: float = 0.0
