from typing import Any, Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

Verdict = Literal["correct", "incorrect", "irrelevant"]


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # SPEC: correct / incorrect. "wrong" (the frontend's word) means incorrect; "irrelevant" = true but useless.
    verdict: Verdict = Field(validation_alias=AliasChoices("verdict", "feedback"))
    service_id: UUID = Field(validation_alias=AliasChoices("service_id", "serviceId"))
    reason: str | None = None

    @field_validator("verdict", mode="before")
    @classmethod
    def _synonyms(cls, value: object) -> object:
        return "incorrect" if value == "wrong" else value


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
