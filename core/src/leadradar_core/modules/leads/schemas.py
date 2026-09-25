from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from leadradar_core.modules.accounts.schemas import CompanyOut
from pydantic import BaseModel


class ScoreSummary(BaseModel):
    priority: Decimal
    tier: str
    fit: Decimal
    intent: Decimal
    risk: Decimal
    disqualified: bool = False


class LeadListItem(BaseModel):
    company: CompanyOut
    service_id: UUID
    score: ScoreSummary
    top_reasons: list[dict[str, Any]] = []
    flags: list[str] = []
    signals_count: int = 0
    new_signals_7d: int = 0
    last_signal_at: str | None = None
    analyzed_at: datetime | None = None


class SignalItem(BaseModel):
    id: UUID
    quote: str
    summary: str
    strength: str
    confidence: Decimal
    url: str | None = None
    source_name: str
    source_type: str
    event_date: str | None = None
    flags: list[str] = []


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
    decision_makers: list[str] = []
    history: list[dict[str, Any]] = []
    sources_summary: dict[str, int] = {}
