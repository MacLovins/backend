"""Settings of leadradar-ai (SPEC §1.5): LLM_* / AI_* env vars, plus GEMINI_API_KEY (or GOOGLE_API_KEY)."""

from typing import Annotated

from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ModelLimits(BaseModel):
    """Free-tier limits of one model, per Google Cloud project (AI Studio → Rate limits)."""

    rpm: int | None = Field(default=None, gt=0)
    rpd: int | None = Field(default=None, gt=0)


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=".env", extra="ignore")

    api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY")
    )
    # Order = fallback order. Defaults are from the spec — verify availability in AI Studio.
    main_models: Annotated[list[str], NoDecode] = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
    cheap_models: Annotated[list[str], NoDecode] = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    limits_json: dict[str, ModelLimits] = {}  # model → {"rpm": 10, "rpd": 250}; missing model = no limit
    timeout_s: float = Field(default=90, gt=0)
    max_input_tokens: int = Field(default=30_000, gt=0)
    cache_enabled: bool = True

    _split = field_validator("main_models", "cheap_models", mode="before")(_split_csv)

    def pool(self, name: str) -> list[str]:
        return {"main": self.main_models, "cheap": self.cheap_models}[name]

    def limits(self, model: str) -> ModelLimits:
        return self.limits_json.get(model, ModelLimits())


class AISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AI_", env_file=".env", extra="ignore")

    embed_model: str = "intfloat/multilingual-e5-small"
    embed_cache_dir: str | None = None  # default is a temp dir that servers may wipe; set it in Docker
    topk_per_question: int = Field(default=5, gt=0)
    max_snippets_per_service: int = Field(default=40, gt=0)
    max_tokens_per_service: int = Field(default=25_000, gt=0)
    min_cosine: float = Field(default=0.78, ge=0, le=1)
