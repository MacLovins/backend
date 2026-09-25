"""Graph state. Kept small for checkpoints: ids, counters and ≤ 40 prefiltered snippets per service —
never document texts (they stay in the store)."""

import operator
from typing import Annotated, Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel

from leadradar_ai.contracts import (
    AnalysisInput,
    CompanyProfile,
    LeadScore,
    RunStats,
    ScoreChange,
    ServiceBundle,
    StepError,
)
from leadradar_ai.extraction.extract import ServiceExtraction
from leadradar_ai.retrieval.prefilter import PrefilterResult


class ServiceOutcome(BaseModel):
    service_id: UUID
    status: Literal["done", "failed", "paused"]  # paused = LLM quota exhausted; rerun later
    extraction_skipped: bool = False  # fingerprint unchanged: scored on stored signals
    score: LeadScore | None = None
    change: ScoreChange | None = None
    llm_calls: int = 0
    llm_cache_hits: int = 0
    signals_verified: int = 0
    evidence_rejected: int = 0


def add_durations(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    merged = dict(left)
    for stage, ms in right.items():
        merged[stage] = merged.get(stage, 0) + ms
    return merged


Outcomes = Annotated[list[ServiceOutcome], operator.add]
Errors = Annotated[list[StepError], operator.add]
Durations = Annotated[dict[str, int], add_durations]


class AnalysisState(TypedDict, total=False):
    input: AnalysisInput
    company: CompanyProfile  # after resolve
    documents_by_source: dict[str, int]
    snippets_indexed: int
    outcomes: Outcomes
    errors: Errors
    durations: Durations
    scores: list[LeadScore]
    stats: RunStats


class AnalysisOutput(TypedDict, total=False):
    scores: list[LeadScore]
    stats: RunStats
    errors: Errors
    outcomes: Outcomes


class ServiceState(TypedDict, total=False):
    input: AnalysisInput
    company: CompanyProfile
    service: ServiceBundle
    pre: PrefilterResult
    skip_extraction: bool
    extraction: ServiceExtraction
    signals_verified: int
    evidence_rejected: int
    failed: bool
    outcomes: Outcomes
    errors: Errors
    durations: Durations


class ServiceOutput(TypedDict, total=False):
    """Only reducer channels go back to the parent: parallel services must not overwrite each other."""

    outcomes: Outcomes
    errors: Errors
    durations: Durations
