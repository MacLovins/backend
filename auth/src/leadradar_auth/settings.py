from uuid import UUID

from pydantic_settings import BaseSettings, SettingsConfigDict

# Development-only fallback. The app refuses to start with it outside dev/test (see insecure_jwt_secret_reason).
DEV_JWT_SECRET = "leadradar-dev-secret-key-at-least-32-chars-long-123456"
MIN_JWT_SECRET_LENGTH = 32
# Values that were ever published in the repository (defaults, .env.example) — never valid in production.
KNOWN_INSECURE_JWT_SECRETS = frozenset(
    {
        DEV_JWT_SECRET,
        "leadradar-super-secret-development-jwt-key-32chars!",
        "change-me-to-a-random-secret-of-at-least-32-chars",
    }
)


def insecure_jwt_secret_reason(secret: str) -> str | None:
    """Why a JWT secret must not be used outside development, or None if it is acceptable."""
    if not secret:
        return "empty"
    if secret in KNOWN_INSECURE_JWT_SECRETS:
        return "known default value"
    if len(secret) < MIN_JWT_SECRET_LENGTH:
        return f"shorter than {MIN_JWT_SECRET_LENGTH} characters"
    return None


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUTH_",
        env_file=".env",
        extra="ignore",
    )

    JWT_SECRET: str = DEV_JWT_SECRET
    JWT_ALG: str = "HS256"
    ACCESS_TTL_MIN: int = 720
    COOKIE_NAME: str = "lr_session"
    # None = decided by the host app from its environment (core: true outside dev/test)
    COOKIE_SECURE: bool | None = None
    ISSUER: str = "leadradar"
    AUDIENCE: str = "leadradar-web"
    DEFAULT_ORG_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")
    # Login rate limit per (client IP, e-mail): at most LOGIN_RATE_LIMIT attempts per window
    LOGIN_RATE_LIMIT: int = 5
    LOGIN_RATE_WINDOW_S: int = 60


auth_settings = AuthSettings()
