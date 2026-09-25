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


class UsageOut(BaseModel):
    llm_calls_24h: int = 0
    input_tokens_24h: int = 0
    output_tokens_24h: int = 0
    documents_scanned_24h: int = 0
