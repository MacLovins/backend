"""Response schema of extract_signals (SPEC §1.7.3) — passed to Gemini as JSON schema."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    snippet_id: str = Field(description="Id of the snippet the quote is copied from, e.g. S3")
    quote: str = Field(max_length=400, description="Verbatim substring of the snippet, original language")
    subject: Literal["target_company", "other_company", "industry_general"]
    event_date: date | None = Field(
        description="Date of the event if stated, else the snippet's published date"
    )
    strength: Literal["weak", "moderate", "strong"]
    summary: str = Field(max_length=240, description="One plain-English sentence for a salesperson")


class Answer(BaseModel):
    question_id: str = Field(description="Question id from <questions>, e.g. Q1")
    answer: Literal["yes", "no", "unclear"]
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list, max_length=3)
    rationale: str = Field(max_length=300)


class ExtractionOutput(BaseModel):
    answers: list[Answer]
