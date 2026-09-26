from uuid import UUID

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

    # DB & Cache (env: APP_DATABASE_URL, APP_REDIS_URL — only APP_-prefixed variables are read)
    DATABASE_URL: str = "postgresql+asyncpg://leadradar:leadradar@localhost:5432/leadradar"
    REDIS_URL: str = "redis://localhost:6379/0"

    WORKER_MAX_ASYNC_TASKS: int = 3
    # LangGraph checkpoints (psycopg URL, schema langgraph). Empty: derived from DATABASE_URL.
    LANGGRAPH_DB_URL: str = ""
    # Hard limits around parser calls: resolve can hang on bot-protected sites
    ANALYSIS_RESOLVE_TIMEOUT_S: float = 60
    ANALYSIS_COLLECT_BUDGET_S: int = 90
    REFRESH_CRON: str = "0 */6 * * *"
    # Discovery calls parser.discover synchronously (Wikidata SPARQL) with this hard timeout
    DISCOVERY_TIMEOUT_S: float = 60
    # CSV import limits (SPEC §1.7): larger files → 413, more rows → 422
    IMPORT_MAX_BYTES: int = 5 * 1024 * 1024
    IMPORT_MAX_ROWS: int = 5000

    # Feature flags
    FEATURE_OUTREACH: bool = True
    FEATURE_ALERTS: bool = False
    FEATURE_HUBSPOT: bool = False
    # True: the API process runs worker tasks itself (dev without a worker). Production: a separate worker.
    EMBEDDED_WORKER: bool = False

    # Scheduler (CO-22) and outbox dispatcher (CO-17)
    RESUME_PAUSED_CRON: str = "*/15 * * * *"
    DISPATCH_EVENTS_CRON: str = "* * * * *"
    JOBS_ALERTS_CRON: str = "0 * * * *"  # jobs_threshold alert rules are evaluated hourly
    REFRESH_MIN_AGE_H: float = 5  # tracked companies analyzed more recently are skipped by refresh_tracked
    EVENTS_DISPATCH_BATCH: int = 100
    EVENTS_MAX_ATTEMPTS: int = 5  # after that a failing event is marked processed with last_error
    # Add-ons (CO-A2 alerts, CO-A3 HubSpot): a consumer without credentials stays disabled
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_STARTTLS: bool = True
    SMTP_FROM: str = ""
    ALERTS_EMAIL_TO: str = ""  # comma-separated
    HUBSPOT_PRIVATE_APP_TOKEN: str = ""
    HUBSPOT_BASE_URL: str = "https://api.hubapi.com"


settings = AppSettings()
