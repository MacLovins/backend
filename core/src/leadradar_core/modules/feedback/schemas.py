from typing import Any, Literal
from uuid import UUID

from leadradar_core.modules.leads.schemas import ScoreSummary
from pydantic import BaseModel, ConfigDict

# canonical enums (ARCHITECTURE §4.7): verdicts depend on what is being rated
SignalVerdict = Literal["correct", "incorrect", "irrelevant"]
LeadVerdict = Literal["good_fit", "bad_fit"]


class SignalFeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: SignalVerdict
    # the signal knows its service; kept optional for older clients and checked against the signal
    service_id: UUID | None = None
    reason: str | None = None


class LeadFeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: LeadVerdict
    service_id: UUID
    reason: str | None = None


class FeedbackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    target_type: Literal["signal", "lead"]
    target_id: UUID
    service_id: UUID
    verdict: str
    reason: str | None
    # signal feedback: the company's score for the service after the vote (rescored when it changed)
    score: ScoreSummary | None = None


class FeedbackWithdrawOut(BaseModel):
    signal_id: UUID
    withdrawn: bool
    score: ScoreSummary | None = None


class QualityBucket(BaseModel):
    labeled: int
    precision: float


class CategoryQuality(QualityBucket):
    category: str


class SourceQuality(QualityBucket):
    source_type: str


class LeadFeedbackStats(BaseModel):
    labeled: int = 0
    good_fit: int = 0
    bad_fit: int = 0


class QualityMetricsOut(BaseModel):
    # precision is computed from signal votes only: correct / (correct + incorrect + irrelevant)
    labeled: int = 0
    precision: float = 0.0
    by_category: list[CategoryQuality] = []
    by_source: list[SourceQuality] = []
    leads: LeadFeedbackStats = LeadFeedbackStats()
    verifier: dict[str, Any] = {
        "evidence_total": 0,
        "rejected": {},
    }
    hallucination_rate: float = 0.0
