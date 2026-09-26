"""OpenAI-compatible transport (OpenAI, Groq/Llama, Ollama, OpenRouter, vLLM).

Supports structured outputs, model fallbacks, and standard JSON schema.
"""

import json
from typing import Any

import httpx
from pydantic import BaseModel

from leadradar_ai.llm.types import Thinking, TransportError, TransportResponse


class OpenAITransport:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        timeout_s: float = 90,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or "sk-dummy"
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={
                "Authorization": f"Bearer {self.api_key}",
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
            f"{system}\n\nIMPORTANT: You must respond ONLY with valid JSON conforming to the following schema:\n"
            f"{json.dumps(output_model.model_json_schema())}"
        )

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": contents},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }

        # Try structured schema first if supported by model (OpenAI gpt-4o / gpt-4o-mini)
        if "gpt-4" in model.lower() or "gpt-3.5" in model.lower():
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": output_model.__name__,
                    "strict": False,
                    "schema": output_model.model_json_schema(),
                },
            }

        url = f"{self.base_url}/chat/completions"
        try:
            response = await self._client.post(url, json=payload)
        except (httpx.TimeoutException, httpx.TransportError, TimeoutError) as e:
            raise TransportError(None, repr(e)) from e

        if response.status_code == 429:
            retry_after = None
            header_val = response.headers.get("retry-after")
            if header_val and header_val.isdigit():
                retry_after = float(header_val)
            raise TransportError(429, response.text, retry_after_s=retry_after)

        if response.status_code >= 400:
            # If json_schema wasn't accepted, retry once with basic json_object
            if payload.get("response_format", {}).get("type") == "json_schema":
                payload["response_format"] = {"type": "json_object"}
                try:
                    retry_res = await self._client.post(url, json=payload)
                    if retry_res.status_code == 200:
                        response = retry_res
                    else:
                        raise TransportError(retry_res.status_code, retry_res.text)
                except Exception as exc:
                    raise TransportError(response.status_code, response.text) from exc
            else:
                raise TransportError(response.status_code, response.text)

        try:
            data = response.json()
        except Exception as e:
            raise TransportError(
                response.status_code, f"Failed to parse JSON response: {response.text}"
            ) from e

        choices = data.get("choices", [])
        if not choices:
            return TransportResponse(text=None, finish_reason="EMPTY", input_tokens=0, output_tokens=0)

        choice = choices[0]
        message = choice.get("message", {})
        text = message.get("content")
        finish_reason = choice.get("finish_reason", "stop")

        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        return TransportResponse(
            text=text,
            finish_reason=str(finish_reason).upper(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
