import os
import sys
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ParserSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PARSER_",
        env_file=None if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST") else ".env",
        extra="ignore",
    )

    # NoDecode: PARSER_ADAPTERS is a comma-separated string (.env.example), not JSON.
    adapters: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "google_news",
            "gdelt",
            "rsshub",
            "website",
            "jobs_ats",
            "careers_html",
            "wikidata",
            *(["newsapi"] if os.getenv("NEWSAPI_KEY") else []),
            *(["serpapi"] if os.getenv("SERPAPI_KEY") else []),
            *(["crunchbase"] if os.getenv("CRUNCHBASE_API_KEY") else []),
        ]
    )
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    cache_dir: Path = Path(".cache/parser")
    cache_ttl_s: int = 86_400
    host_rps: float = 1.0
    max_concurrency: int = 8
    request_timeout_s: float = 10.0
    retry_attempts: int = 2
    retry_min_wait_s: float = 0.5
    retry_max_wait_s: float = 15.0

    @field_validator("adapters", mode="before")
    @classmethod
    def split_adapters(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value
