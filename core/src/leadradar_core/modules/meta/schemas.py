from pydantic import BaseModel


class IndustryOut(BaseModel):
    id: str
    label: str


class CountryOut(BaseModel):
    code: str
    name: str
    is_eu: bool = True


class PresetOut(BaseModel):
    key: str
    name: str
    description: str


class LabelsOut(BaseModel):
    categories: dict[str, str]
    weights: dict[str, str]
    statuses: dict[str, str]


class ModelUsageOut(BaseModel):
    model: str
    calls: int  # including cache hits
    cache_hits: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    rpd_limit: int | None = None  # free-tier requests per day (LLM_LIMITS_JSON); None = not configured


class UsageOut(BaseModel):
    llm_calls_24h: int = 0  # real calls: cache hits excluded
    input_tokens_24h: int = 0
    output_tokens_24h: int = 0
    documents_scanned_24h: int = 0
    by_model: list[ModelUsageOut] = []
    documents_by_source: dict[str, int] = {}
