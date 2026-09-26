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
            "website",
            "jobs_ats",
            "careers_html",
            "wikidata",
            # One cached catalogue request per day / a few PDFs per company / one registry lookup: cheap and polite.
            "reports",
            "hibp",
            "gleif",
            *(["newsapi"] if os.getenv("NEWSAPI_KEY") else []),
            *(["serpapi"] if os.getenv("SERPAPI_KEY") else []),
            *(["rsshub"] if os.getenv("RSSHUB_BASE_URL") else []),
            *(["crunchbase"] if os.getenv("CRUNCHBASE_API_KEY") else []),
            *(["adzuna"] if os.getenv("ADZUNA_APP_ID") and os.getenv("ADZUNA_APP_KEY") else []),
        ]
    )
    # Identifiable bot UA with a contact (SPEC §1.7.1). Measured: a spoofed browser UA gets HTTP 429
    # (Retry-After 1000 s) from Wikidata and tarpitted connections from Akamai sites such as dhl.com.
    user_agent: str = "LeadRadarBot/0.1 (+https://github.com/MacLovins/backend)"
    cache_dir: Path = Path(".cache/parser")
    cache_ttl_s: int = 86_400
    host_rps: float = 1.0
    max_concurrency: int = 8
    request_timeout_s: float = 10.0
    retry_attempts: int = 2
    retry_min_wait_s: float = 0.5
    retry_max_wait_s: float = 15.0
    # reports adapter (PDF annual / strategy reports)
    reports_max_pdfs: int = Field(default=3, ge=0, le=20)
    reports_max_pdf_mb: float = Field(default=30.0, gt=0, le=200)
    reports_download_timeout_s: float = Field(default=60.0, gt=0)

    # Source credentials use their conventional names (no PARSER_ prefix). Declaring them here means they are
    # read from .env like every other setting: pydantic-settings does not export .env into os.environ, so a
    # plain os.getenv() in an adapter misses them when core runs outside Docker.
    newsapi_key: str | None = Field(default=None, validation_alias="NEWSAPI_KEY")
    serpapi_key: str | None = Field(default=None, validation_alias="SERPAPI_KEY")
    rsshub_base_url: str | None = Field(default=None, validation_alias="RSSHUB_BASE_URL")
    # RSSHub keyword-search route; {query} is URL-encoded. RSSHub's /google/news/:category/:locale takes a
    # Google News section title, not a search, so it cannot be used for company queries.
    rsshub_route: str = Field(default="/bing/search/{query}", validation_alias="RSSHUB_ROUTE")
    crunchbase_api_key: str | None = Field(default=None, validation_alias="CRUNCHBASE_API_KEY")
    adzuna_app_id: str | None = Field(default=None, validation_alias="ADZUNA_APP_ID")
    adzuna_app_key: str | None = Field(default=None, validation_alias="ADZUNA_APP_KEY")

    def env(self, name: str) -> str | None:
        """A source credential: the process environment first, then the same key loaded from .env."""
        values = {
            "NEWSAPI_KEY": self.newsapi_key,
            "SERPAPI_KEY": self.serpapi_key,
            "RSSHUB_BASE_URL": self.rsshub_base_url,
            "CRUNCHBASE_API_KEY": self.crunchbase_api_key,
            "ADZUNA_APP_ID": self.adzuna_app_id,
            "ADZUNA_APP_KEY": self.adzuna_app_key,
        }
        return os.getenv(name) or values.get(name) or None

    @field_validator("adapters", mode="before")
    @classmethod
    def split_adapters(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value
