"""Transport over the google-genai SDK — the only module that imports it.

SDK retries stay off (the default): retries, fallback and limits belong to GeminiClient.
temperature / top_p / top_k are never set: Gemini 3.x loops with them (SPEC §1.7.3).
"""

import re
from typing import Any

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from leadradar_ai.llm.types import Thinking, TransportError, TransportResponse

_THINKING_LEVELS = {"low": types.ThinkingLevel.LOW, "minimal": types.ThinkingLevel.MINIMAL}
_DURATION = re.compile(r"^\s*([\d.]+)s\s*$")


def build_config(
    system: str, output_model: type[BaseModel], thinking: Thinking
) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=output_model.model_json_schema(),
        thinking_config=(
            types.ThinkingConfig(thinking_level=_THINKING_LEVELS[thinking]) if thinking != "default" else None
        ),
    )


def _error_details(details: Any) -> list[dict]:
    if not isinstance(details, dict):
        return []
    items = details.get("error", details).get("details", [])
    return [d for d in items if isinstance(d, dict)]


def to_transport_error(e: errors.APIError) -> TransportError:
    retry_after = None
    daily = False
    for d in _error_details(e.details):
        kind = d.get("@type", "")
        if kind.endswith("RetryInfo") and (m := _DURATION.match(str(d.get("retryDelay", "")))):
            retry_after = float(m.group(1))
        if kind.endswith("QuotaFailure"):
            daily = daily or any("PerDay" in str(v.get("quotaId", "")) for v in d.get("violations", []))
    return TransportError(e.code, e.message or str(e), retry_after_s=retry_after, daily_quota=daily)


def to_response(response: types.GenerateContentResponse) -> TransportResponse:
    finish = None
    if response.candidates:
        reason = response.candidates[0].finish_reason
        finish = getattr(reason, "name", None) or (str(reason) if reason else None)
    elif response.prompt_feedback and response.prompt_feedback.block_reason:
        finish = "PROMPT_BLOCKED"
    usage = response.usage_metadata
    return TransportResponse(
        text=response.text if response.candidates else None,
        finish_reason=finish,
        input_tokens=(usage.prompt_token_count or 0) if usage else 0,
        output_tokens=((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0))
        if usage
        else 0,
    )


class GenaiTransport:
    def __init__(self, api_key: str | None = None, *, timeout_s: float = 90, client: Any = None) -> None:
        self._client = client or genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000))
        )

    async def generate(
        self,
        *,
        model: str,
        system: str,
        contents: str,
        output_model: type[BaseModel],
        thinking: Thinking,
    ) -> TransportResponse:
        try:
            response = await self._client.aio.models.generate_content(
                model=model, contents=contents, config=build_config(system, output_model, thinking)
            )
        except errors.APIError as e:
            raise to_transport_error(e) from e
        except (httpx.TimeoutException, httpx.TransportError, TimeoutError) as e:
            raise TransportError(None, repr(e)) from e
        return to_response(response)
