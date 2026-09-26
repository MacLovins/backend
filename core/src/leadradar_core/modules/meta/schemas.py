from datetime import datetime

from pydantic import BaseModel


class IndustryOut(BaseModel):
    id: str
    label: str
    nace: list[str] = []
    nis2: str | None = None  # NIS2 annex ("I" / "II") when the sector is covered
    dora: bool = False


class CountryOut(BaseModel):
    code: str
    name: str
    is_eu: bool = True
    languages: list[str] = []


class PresetOut(BaseModel):
    key: str
    name: str
    description: str
    questions_count: int = 0
    categories: list[str] = []


class LabelsOut(BaseModel):
    categories: dict[str, str]
    weights: dict[str, str]
    statuses: dict[str, str]
    polarities: dict[str, str] = {}
    source_types: dict[str, str] = {}
    strengths: dict[str, str] = {}
    answers: dict[str, str] = {}
    tiers: dict[str, str] = {}
    stages: dict[str, str] = {}
    run_kinds: dict[str, str] = {}
    run_statuses: dict[str, str] = {}
    signal_feedback: dict[str, str] = {}
    lead_feedback: dict[str, str] = {}
    roles: dict[str, str] = {}
    reject_reasons: dict[str, str] = {}
    signal_flags: dict[str, str] = {}


class ModelUsageOut(BaseModel):
    model: str
    pool: str | None = None  # "main" / "cheap" when the model is in a configured fallback pool
    calls: int = 0  # all recorded calls today, including cache hits
    quota_calls: int = 0  # calls that count against the daily quota (no cache hits / local rate limits)
    cache_hits: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    rpd_limit: int | None = None
    rpm_limit: int | None = None
    remaining: int | None = None  # rpd_limit - quota_calls (None = no configured limit)


class UsageOut(BaseModel):
    """LLM usage for the current quota day (Gemini resets daily quotas at midnight Pacific time)."""

    day_start: datetime | None = None
    resets_at: datetime | None = None
    llm_calls_24h: int = 0  # kept for compatibility: same numbers as the current quota day
    input_tokens_24h: int = 0
    output_tokens_24h: int = 0
    documents_scanned_24h: int = 0
    models: list[ModelUsageOut] = []
    documents_by_source: dict[str, int] = {}
