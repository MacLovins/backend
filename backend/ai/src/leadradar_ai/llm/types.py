"""LLM interface used by the graph and config helpers; GeminiClient and FakeLLM implement it.

A Transport is the thin SDK adapter under GeminiClient: swapping the SDK or provider changes only it.
"""

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

from leadradar_ai.contracts import LLMPurpose

Pool = Literal["main", "cheap"]
Thinking = Literal["default", "low", "minimal"]


@dataclass(frozen=True)
class LLMRequest[T: BaseModel]:
    purpose: LLMPurpose
    prompt_version: str  # "extract_signals@v1" — part of the cache key
    system: str
    user: str
    output_model: type[T]
    pool: Pool = "main"
    thinking: Thinking = "default"  # "minimal" for cheap tasks, if the model supports it
    run_id: UUID | None = None


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    output: T | None  # None only when blocked
    model: str
    cache_hit: bool = False
    blocked: bool = False  # finish_reason SAFETY / RECITATION / …: the caller treats all answers as "unclear"
    repaired: bool = False  # the first answer failed validation and the repair attempt succeeded
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


@runtime_checkable
class LLMClient(Protocol):
    async def generate[T: BaseModel](self, request: LLMRequest[T]) -> LLMResult[T]: ...


# --- transport ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportResponse:
    text: str | None
    finish_reason: str | None  # "STOP", "SAFETY", …
    input_tokens: int = 0
    output_tokens: int = 0


class TransportError(Exception):
    """status None = network error or timeout."""

    def __init__(
        self,
        status: int | None,
        message: str,
        *,
        retry_after_s: float | None = None,
        daily_quota: bool = False,
    ) -> None:
        self.status = status
        self.retry_after_s = retry_after_s
        self.daily_quota = daily_quota  # 429 caused by the per-day quota: waiting will not help today
        super().__init__(f"{status}: {message}")


class Transport(Protocol):
    async def generate(
        self,
        *,
        model: str,
        system: str,
        contents: str,
        output_model: type[BaseModel],
        thinking: Thinking,
    ) -> TransportResponse: ...
