from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ParserSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PARSER_", env_file=".env", extra="ignore")

    # NoDecode: PARSER_ADAPTERS is a comma-separated string (.env.example), not JSON.
    adapters: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["gdelt", "website", "jobs_ats", "careers_html", "wikidata"]
    )
    user_agent: str = "LeadRadarBot/0.1 (+https://example.invalid/bot; engineering@example.invalid)"
    cache_dir: Path = Path(".cache/parser")
    cache_ttl_s: int = 86_400
    host_rps: float = 1.0
    max_concurrency: int = 8
    request_timeout_s: float = 30.0
    retry_attempts: int = 3
    retry_min_wait_s: float = 1.0
    retry_max_wait_s: float = 120.0

    @field_validator("adapters", mode="before")
    @classmethod
    def split_adapters(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value
