from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from leadradar_core.modules.accounts.schemas import CompanyOut
from pydantic import BaseModel, ConfigDict


class ScoreSummary(BaseModel):
    """Scores are JSON numbers (the ORM keeps them as Numeric/Decimal)."""

    model_config = ConfigDict(from_attributes=True)

    priority: float
    tier: str
    fit: float
    intent: float
    risk: float
    disqualified: bool = False


class LeadListItem(BaseModel):
    company: CompanyOut
    service_id: UUID
    score: ScoreSummary
    top_reasons: list[dict[str, Any]] = []
    flags: list[str] = []
    signals_count: int = 0
    new_signals_7d: int = 0
    last_signal_at: date | None = None
    analyzed_at: datetime | None = None


class SignalItem(BaseModel):
    id: UUID
    quote: str
    summary: str
    strength: str
    confidence: float
    url: str | None = None
    source_name: str
    source_type: str
    event_date: str | None = None
    flags: list[str] = []
    my_feedback: Literal["correct", "incorrect", "irrelevant"] | None = None


class QuestionSignals(BaseModel):
    question: dict[str, Any]
    strength: float
    points: float
    signals: list[SignalItem] = []


class LeadDetail(BaseModel):
    company: CompanyOut
    service: dict[str, Any]
    score: dict[str, Any]
    signals_by_question: list[QuestionSignals] = []
    # active questions of the service with no active evidence for this company
    questions_without_evidence: list[dict[str, Any]] = []
    # the current user's verdict on the lead for this service
    my_feedback: Literal["good_fit", "bad_fit"] | None = None
    decision_makers: list[str] = []
    history: list[dict[str, Any]] = []
    sources_summary: dict[str, int] = {}
