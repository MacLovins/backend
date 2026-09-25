from uuid import UUID

from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUTH_",
        env_file=".env",
        extra="ignore",
    )

    JWT_SECRET: str = "leadradar-dev-secret-key-at-least-32-chars-long-123456"
    JWT_ALG: str = "HS256"
    ACCESS_TTL_MIN: int = 720
    COOKIE_NAME: str = "lr_session"
    COOKIE_SECURE: bool = False
    ISSUER: str = "leadradar"
    AUDIENCE: str = "leadradar-web"
    DEFAULT_ORG_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")


auth_settings = AuthSettings()
