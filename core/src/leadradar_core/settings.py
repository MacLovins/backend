from uuid import UUID

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        extra="ignore",
    )

    ENV: str = "dev"
    PUBLIC_ORIGIN: str = "http://localhost:3000"
    DEFAULT_ORG_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")

    # DB & Cache - allow reading directly without APP_ prefix as well
    DATABASE_URL: str = "postgresql+asyncpg://leadradar:leadradar@localhost:5432/leadradar"
    REDIS_URL: str = "redis://localhost:6379/0"

    WORKER_MAX_ASYNC_TASKS: int = 3
    # LangGraph checkpoints (psycopg URL, schema langgraph). Empty: derived from DATABASE_URL.
    LANGGRAPH_DB_URL: str = ""
    # Hard limits around parser calls: resolve can hang on bot-protected sites
    ANALYSIS_RESOLVE_TIMEOUT_S: float = 60
    ANALYSIS_COLLECT_BUDGET_S: int = 90
    # Outreach calls the LLM inside the HTTP request: bounded, then the template draft is returned
    OUTREACH_TIMEOUT_S: float = 25
    # Embedded worker (EMBEDDED_WORKER): at most this many company analyses run at once in the API process
    EMBEDDED_MAX_CONCURRENCY: int = 2
    # HubSpot private app token (scopes in modules/integrations/hubspot.py); push works when the flag is on too
    HUBSPOT_PRIVATE_APP_TOKEN: str = Field(
        "", validation_alias=AliasChoices("APP_HUBSPOT_PRIVATE_APP_TOKEN", "HUBSPOT_PRIVATE_APP_TOKEN")
    )
    REFRESH_CRON: str = "0 */6 * * *"

    # Feature flags
    FEATURE_OUTREACH: bool = True
    FEATURE_ALERTS: bool = False
    FEATURE_HUBSPOT: bool = Field(
        False, validation_alias=AliasChoices("APP_FEATURE_HUBSPOT", "FEATURE_HUBSPOT")
    )
    EMBEDDED_WORKER: bool = True


settings = AppSettings()
