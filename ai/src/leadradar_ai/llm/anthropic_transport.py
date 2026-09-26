"""Anthropic Claude transport (Claude 3.5 Sonnet, Claude 3.5 Haiku)."""

import json
from typing import Any

import httpx
from pydantic import BaseModel

from leadradar_ai.llm.types import Thinking, TransportError, TransportResponse


class AnthropicTransport:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com/v1",
        *,
        timeout_s: float = 90,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or "sk-ant-dummy"
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
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
        system_prompt = (
            f"{system}\n\nIMPORTANT: You must output ONLY a valid JSON object matching this schema:\n"
            f"{json.dumps(output_model.model_json_schema())}\nDo not include Markdown blocks or explanation."
        )

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": contents},
            ],
            "temperature": 0.1,
        }

        url = f"{self.base_url}/messages"
        try:
            response = await self._client.post(url, json=payload)
        except (httpx.TimeoutException, httpx.TransportError, TimeoutError) as e:
            raise TransportError(None, repr(e)) from e

        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            raise TransportError(
                429, response.text, retry_after_s=float(retry_after) if retry_after else None
            )

        if response.status_code >= 400:
            raise TransportError(response.status_code, response.text)

        try:
            data = response.json()
        except Exception as e:
            raise TransportError(
                response.status_code, f"Failed to parse JSON response: {response.text}"
            ) from e

        content_blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")

        usage = data.get("usage", {})
        return TransportResponse(
            text=text,
            finish_reason=str(data.get("stop_reason", "end_turn")).upper(),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
