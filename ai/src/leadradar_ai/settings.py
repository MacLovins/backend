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

    provider: str = Field(default="auto", validation_alias=AliasChoices("LLM_PROVIDER", "AI_PROVIDER"))
    api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_GEMINI_API_KEY")
    )
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "LLM_OPENAI_API_KEY")
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "LLM_ANTHROPIC_API_KEY")
    )
    groq_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GROQ_API_KEY", "LLM_GROQ_API_KEY")
    )
    openai_base_url: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_BASE_URL", "LLAMA_BASE_URL", "OLLAMA_BASE_URL")
    )

    # Order = fallback order. Defaults are from the spec — verify availability in AI Studio.
    main_models: Annotated[list[str], NoDecode] = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    cheap_models: Annotated[list[str], NoDecode] = ["gemini-2.5-flash-lite", "gemini-2.0-flash-lite"]
    limits_json: dict[str, ModelLimits] = {}  # model → {"rpm": 10, "rpd": 250}; missing model = no limit
    timeout_s: float = Field(default=90, gt=0)
    max_input_tokens: int = Field(default=30_000, gt=0)
    cache_enabled: bool = True

    _split = field_validator("main_models", "cheap_models", mode="before")(_split_csv)

    @property
    def resolved_provider(self) -> str:
        if self.provider != "auto":
            return self.provider.lower()
        if self.api_key:
            return "gemini"
        if self.groq_api_key:
            return "groq"
        if self.openai_api_key:
            return "openai"
        if self.anthropic_api_key:
            return "anthropic"
        if self.openai_base_url:
            return "llama"
        return "gemini"

    def pool(self, name: str) -> list[str]:
        p = self.resolved_provider
        # If user left default Gemini models but configured OpenAI / Groq / Anthropic, provide fitting defaults
        is_default_models = self.main_models == ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
        if is_default_models:
            if p == "openai":
                return ["gpt-4o", "gpt-4o-mini"] if name == "main" else ["gpt-4o-mini"]
            if p in ("groq", "llama"):
                return (
                    ["llama-3.3-70b-versatile", "llama-3.1-70b-versatile"]
                    if name == "main"
                    else ["llama-3.1-8b-instant"]
                )
            if p == "anthropic":
                return ["claude-3-5-sonnet-20241022"] if name == "main" else ["claude-3-5-haiku-20241022"]
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
