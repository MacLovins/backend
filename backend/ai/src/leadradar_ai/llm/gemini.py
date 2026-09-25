"""GeminiClient — the LLM gateway (SPEC §1.7.6, ARCHITECTURE §4.8).

Structured output by Pydantic schema, model pools with fallback, RPM/RPD limiter, retries, response cache,
usage accounting and one JSON repair attempt. The SDK lives behind `Transport` (genai_transport.py).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Literal

import structlog
from pydantic import BaseModel, ValidationError

from leadradar_ai.contracts import LLMCallRecord, LLMCallStatus
from leadradar_ai.errors import (
    ExtractionFailed,
    LLMBadRequest,
    LLMInputTooLarge,
    LLMUnavailable,
    QuotaExhausted,
)
from leadradar_ai.llm.cache_key import cache_key, estimate_tokens
from leadradar_ai.llm.limiter import NoDailyBudget, RateLimiter, pacific_today
from leadradar_ai.llm.types import LLMRequest, LLMResult, Transport, TransportError, TransportResponse
from leadradar_ai.ports import LLMCache, UsageSink
from leadradar_ai.settings import LLMSettings

log = structlog.get_logger(__name__)

BACKOFF_429_S = (5.0, 15.0, 45.0)
MAX_RETRY_AFTER_S = 60.0  # the API asks to wait longer → move on to the next model
BACKOFF_5XX_S = (2.0, 4.0, 8.0)
BLOCKED_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "PROMPT_BLOCKED"}

REPAIR_SUFFIX = """

<validation_error>
{errors}
</validation_error>
Your previous answer did not match the required JSON schema (errors above). Return the complete corrected JSON only."""

_ModelOutcome = Literal["quota", "error"]


class GeminiClient:
    def __init__(
        self,
        settings: LLMSettings,
        transport: Transport,
        usage: UsageSink,
        cache: LLMCache | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        today: Callable[[], date] = pacific_today,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._usage = usage
        self._cache = cache if settings.cache_enabled else None
        self._clock = clock
        self._sleep = sleep
        self._limiter = RateLimiter(settings, usage, clock=clock, sleep=sleep, today=today)

    @classmethod
    def from_settings(
        cls, settings: LLMSettings, usage: UsageSink, cache: LLMCache | None = None
    ) -> "GeminiClient":
        from leadradar_ai.llm.genai_transport import GenaiTransport

        if settings.api_key is None:
            raise ValueError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
        transport = GenaiTransport(settings.api_key.get_secret_value(), timeout_s=settings.timeout_s)
        return cls(settings, transport, usage, cache)

    async def generate[T: BaseModel](self, request: LLMRequest[T]) -> LLMResult[T]:
        tokens = estimate_tokens(request.system, request.user)
        if tokens > self._settings.max_input_tokens:
            raise LLMInputTooLarge(
                f"~{tokens} tokens > LLM_MAX_INPUT_TOKENS={self._settings.max_input_tokens}"
            )

        key = cache_key(request.prompt_version, request.system, request.user, request.output_model)
        cached = await self._from_cache(request, key)
        if cached is not None:
            return cached

        pool = self._settings.pool(request.pool)
        outcomes: dict[str, _ModelOutcome] = {}
        while remaining := [m for m in pool if m not in outcomes]:
            try:
                model = await self._limiter.acquire(remaining)
            except NoDailyBudget:
                break
            result = await self._try_model(model, request, key)
            if isinstance(result, LLMResult):
                return result
            outcomes[model] = result
            log.warning("llm_model_fallback", model=model, reason=result, purpose=request.purpose)

        if "error" in outcomes.values():
            raise LLMUnavailable(f"all models of pool '{request.pool}' failed: {outcomes}")
        raise QuotaExhausted(request.pool)

    async def _from_cache[T: BaseModel](self, request: LLMRequest[T], key: str) -> LLMResult[T] | None:
        if self._cache is None:
            return None
        entry = await self._cache.get(key)
        if entry is None:
            return None
        try:
            output = request.output_model.model_validate(entry["output"])
        except (ValidationError, KeyError):
            return None
        model = entry.get("model", "unknown")
        await self._record(request, model, "cache_hit", cache_hit=True)
        return LLMResult(
            output=output,
            model=model,
            cache_hit=True,
            input_tokens=entry.get("input_tokens", 0),
            output_tokens=entry.get("output_tokens", 0),
        )

    async def _try_model[T: BaseModel](
        self, model: str, request: LLMRequest[T], key: str
    ) -> LLMResult[T] | _ModelOutcome:
        """Calls one model until success or a reason to fall back. The first slot is already acquired."""
        contents, thinking = request.user, request.thinking
        rate_limited = server_errors = 0
        repair_attempted = False
        first = True
        while True:
            if not first:
                try:
                    await self._limiter.acquire([model])
                except NoDailyBudget:
                    return "quota"
            first = False

            started = self._clock()
            try:
                response = await self._transport.generate(
                    model=model,
                    system=request.system,
                    contents=contents,
                    output_model=request.output_model,
                    thinking=thinking,
                )
            except TransportError as e:
                latency = self._ms_since(started)
                status: LLMCallStatus = "rate_limited" if e.status == 429 else "error"
                await self._record(request, model, status, latency_ms=latency, error=str(e))
                if e.status == 429:
                    if e.daily_quota:
                        self._limiter.mark_exhausted(model)
                        return "quota"
                    if rate_limited >= len(BACKOFF_429_S):
                        return "quota"
                    wait = e.retry_after_s if e.retry_after_s is not None else BACKOFF_429_S[rate_limited]
                    rate_limited += 1
                    if wait > MAX_RETRY_AFTER_S:
                        return "quota"
                    await self._sleep(wait)
                    continue
                if e.status is None or e.status >= 500:
                    if server_errors >= len(BACKOFF_5XX_S):
                        return "error"
                    await self._sleep(BACKOFF_5XX_S[server_errors])
                    server_errors += 1
                    continue
                if e.status == 400 and thinking != "default":
                    log.warning("llm_thinking_unsupported", model=model, error=str(e))
                    thinking = "default"
                    continue
                log.error("llm_bad_request", model=model, purpose=request.purpose, error=str(e))
                raise LLMBadRequest(str(e)) from e

            latency = self._ms_since(started)
            if response.finish_reason in BLOCKED_REASONS:
                await self._record(
                    request,
                    model,
                    "blocked",
                    response=response,
                    latency_ms=latency,
                    error=response.finish_reason,
                )
                log.warning(
                    "llm_blocked", model=model, purpose=request.purpose, reason=response.finish_reason
                )
                return self._result(None, model, response, latency, blocked=True)

            try:
                output = request.output_model.model_validate_json(response.text or "")
            except ValidationError as e:
                await self._record(
                    request,
                    model,
                    "invalid_output",
                    response=response,
                    latency_ms=latency,
                    error=str(e)[:500],
                )
                if repair_attempted:
                    raise ExtractionFailed(f"{request.purpose}: invalid output after repair: {e}") from e
                repair_attempted = True
                contents = request.user + REPAIR_SUFFIX.format(errors=str(e)[:2000])
                continue

            await self._record(request, model, "ok", response=response, latency_ms=latency)
            if self._cache is not None:
                await self._cache.set(
                    key,
                    {
                        "output": output.model_dump(mode="json"),
                        "model": model,
                        "input_tokens": response.input_tokens,
                        "output_tokens": response.output_tokens,
                    },
                    {"model": model, "prompt_version": request.prompt_version, "purpose": request.purpose},
                )
            return self._result(output, model, response, latency, repaired=repair_attempted)

    @staticmethod
    def _result[T: BaseModel](
        output: T | None, model: str, response: TransportResponse, latency: int, **flags: bool
    ) -> LLMResult[T]:
        return LLMResult(
            output=output,
            model=model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=latency,
            **flags,
        )

    def _ms_since(self, started: float) -> int:
        return int((self._clock() - started) * 1000)

    async def _record(
        self,
        request: LLMRequest,
        model: str,
        status: LLMCallStatus,
        *,
        response: TransportResponse | None = None,
        latency_ms: int = 0,
        cache_hit: bool = False,
        error: str | None = None,
    ) -> None:
        await self._usage.record(
            LLMCallRecord(
                run_id=request.run_id,
                purpose=request.purpose,
                model=model,
                prompt_version=request.prompt_version,
                input_tokens=response.input_tokens if response else 0,
                output_tokens=response.output_tokens if response else 0,
                latency_ms=latency_ms,
                cache_hit=cache_hit,
                status=status,
                error=error,
            )
        )
