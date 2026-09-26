import asyncio
import json
from datetime import date
from typing import Literal

import pytest
from google.genai import errors, types
from leadradar_ai import (
    ExtractionFailed,
    GeminiClient,
    LLMBadRequest,
    LLMClient,
    LLMInputTooLarge,
    LLMRequest,
    LLMSettings,
    LLMUnavailable,
    QuotaExhausted,
)
from leadradar_ai.llm.genai_transport import GenaiTransport, build_config, to_response, to_transport_error
from leadradar_ai.llm.types import TransportError, TransportResponse
from leadradar_ai.testing import (
    BLOCKED,
    FakeLLM,
    FakeTransport,
    InMemoryLLMCache,
    InMemoryUsageSink,
)
from pydantic import BaseModel, Field


class Answer(BaseModel):
    question_id: str
    answer: Literal["yes", "no", "unclear"]
    confidence: float = Field(ge=0, le=1)


class Output(BaseModel):
    answers: list[Answer]


GOOD = json.dumps({"answers": [{"question_id": "Q1", "answer": "yes", "confidence": 0.9}]})
BAD = json.dumps({"answers": [{"question_id": "Q1", "answer": "maybe", "confidence": 2}]})


def request(user: str = "<snippets>…</snippets> Answer every question.", **overrides) -> LLMRequest[Output]:
    data = {
        "purpose": "extract_signals",
        "prompt_version": "extract_signals@v1",
        "system": "<role>analyst</role>",
        "user": user,
        "output_model": Output,
    }
    return LLMRequest(**(data | overrides))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.day = date(2026, 9, 25)

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def settings(**overrides) -> LLMSettings:
    data = {"main_models": ["m1", "m2"], "cheap_models": ["c1"], "limits_json": {}, "_env_file": None}
    return LLMSettings(**(data | overrides))


def make_client(script, *, usage=None, cache=None, clock=None, **settings_overrides):
    clock = clock or Clock()
    transport = FakeTransport(script)
    usage = usage if usage is not None else InMemoryUsageSink()
    client = GeminiClient(
        settings(**settings_overrides),
        transport,
        usage,
        cache,
        clock=clock,
        sleep=clock.sleep,
        today=lambda: clock.day,
    )
    return client, transport, usage, clock


def run(coro):
    return asyncio.run(coro)


def rate_limited(retry_after=None, daily=False):
    return TransportError(429, "RESOURCE_EXHAUSTED", retry_after_s=retry_after, daily_quota=daily)


# --- happy path and cache -----------------------------------------------------------------------


def test_success_parses_records_and_caches():
    cache = InMemoryLLMCache()
    client, transport, usage, _ = make_client({"m1": [GOOD]}, cache=cache)
    result = run(client.generate(request()))
    assert result.output == Output(answers=[Answer(question_id="Q1", answer="yes", confidence=0.9)])
    assert (result.model, result.cache_hit, result.input_tokens, result.output_tokens) == (
        "m1",
        False,
        100,
        20,
    )
    assert [(c.model, c.status) for c in usage.calls] == [("m1", "ok")]
    assert transport.calls[0].system == "<role>analyst</role>"
    assert len(cache.entries) == 1


def test_repeat_request_is_a_cache_hit_without_transport_call():
    cache = InMemoryLLMCache()
    client, transport, usage, _ = make_client({"m1": [GOOD]}, cache=cache)
    first = run(client.generate(request()))
    second = run(client.generate(request()))
    assert second.cache_hit and second.output == first.output and second.model == "m1"
    assert len(transport.calls) == 1
    assert [c.status for c in usage.calls] == ["ok", "cache_hit"]
    # a different prompt version is a different key
    with pytest.raises(AssertionError, match="unexpected call"):
        run(client.generate(request(prompt_version="extract_signals@v2")))


def test_cache_disabled():
    cache = InMemoryLLMCache()
    client, transport, _, _ = make_client({"m1": [GOOD, GOOD]}, cache=cache, cache_enabled=False)
    run(client.generate(request()))
    run(client.generate(request()))
    assert len(transport.calls) == 2 and cache.entries == {}


def test_cheap_pool_and_thinking_are_passed():
    client, transport, _, _ = make_client({"c1": [GOOD]})
    run(client.generate(request(pool="cheap", thinking="minimal")))
    assert (transport.calls[0].model, transport.calls[0].thinking) == ("c1", "minimal")


# --- 429, fallback, quotas ----------------------------------------------------------------------


def test_429_waits_retry_delay_then_succeeds_on_same_model():
    client, _transport, usage, clock = make_client({"m1": [rate_limited(retry_after=7), GOOD]})
    result = run(client.generate(request()))
    assert result.model == "m1"
    assert clock.sleeps == [7]
    assert [c.status for c in usage.calls] == ["rate_limited", "ok"]


def test_429_backoff_then_fallback_to_next_model():
    client, transport, _, clock = make_client({"m1": [rate_limited()] * 4, "m2": [GOOD]})
    result = run(client.generate(request()))
    assert result.model == "m2"
    assert clock.sleeps == [5, 15, 45]
    assert transport.models_called() == ["m1", "m1", "m1", "m1", "m2"]


def test_429_with_long_retry_delay_falls_back_immediately():
    client, _transport, _, clock = make_client({"m1": [rate_limited(retry_after=3600)], "m2": [GOOD]})
    assert run(client.generate(request())).model == "m2"
    assert clock.sleeps == []


def test_daily_429_marks_model_exhausted_until_next_day():
    client, transport, _, clock = make_client(
        {"m1": [rate_limited(daily=True), GOOD], "m2": [GOOD, GOOD]}, cache_enabled=False
    )
    assert run(client.generate(request())).model == "m2"
    assert run(client.generate(request())).model == "m2"  # m1 skipped without a call
    assert transport.models_called() == ["m1", "m2", "m2"]
    clock.day = date(2026, 9, 26)
    assert run(client.generate(request())).model == "m1"


def test_all_models_out_of_daily_quota_raises_quota_exhausted():
    client, _, _, _ = make_client({"m1": [rate_limited(daily=True)], "m2": [rate_limited(daily=True)]})
    with pytest.raises(QuotaExhausted) as exc:
        run(client.generate(request()))
    assert exc.value.pool == "main"


def test_rpd_from_usage_sink_skips_model_without_calling_it():
    usage = InMemoryUsageSink(preset={"m1": 250})
    client, transport, _, _ = make_client(
        {"m2": [GOOD]}, usage=usage, limits_json={"m1": {"rpm": 10, "rpd": 250}}
    )
    assert run(client.generate(request())).model == "m2"
    assert transport.models_called() == ["m2"]


def test_rpd_exhausted_on_every_model_raises_before_any_call():
    usage = InMemoryUsageSink(preset={"m1": 5, "m2": 5})
    client, transport, _, _ = make_client({}, usage=usage, limits_json={"m1": {"rpd": 5}, "m2": {"rpd": 5}})
    with pytest.raises(QuotaExhausted):
        run(client.generate(request()))
    assert transport.calls == []


def test_rpm_prefers_model_with_free_slot():
    client, _transport, _, _ = make_client(
        {"m1": [GOOD, GOOD], "m2": [GOOD]}, cache_enabled=False, limits_json={"m1": {"rpm": 2}}
    )
    models = [run(client.generate(request())).model for _ in range(3)]
    assert models == ["m1", "m1", "m2"]


def test_rpm_waits_for_window_when_pool_is_busy():
    client, _, _, clock = make_client(
        {"c1": [GOOD, GOOD]}, cache_enabled=False, limits_json={"c1": {"rpm": 1}}
    )
    run(client.generate(request(pool="cheap")))
    clock.now += 10
    run(client.generate(request(pool="cheap")))
    assert clock.sleeps == [50]


# --- server errors, bad requests ----------------------------------------------------------------


def test_5xx_retries_then_succeeds():
    client, _, usage, clock = make_client(
        {"m1": [TransportError(503, "UNAVAILABLE"), TransportError(None, "timeout"), GOOD]}
    )
    assert run(client.generate(request())).model == "m1"
    assert clock.sleeps == [2, 4]
    assert [c.status for c in usage.calls] == ["error", "error", "ok"]


def test_5xx_everywhere_raises_unavailable():
    down = [TransportError(500, "INTERNAL")] * 4
    client, transport, _, _ = make_client({"m1": list(down), "m2": list(down)})
    with pytest.raises(LLMUnavailable):
        run(client.generate(request()))
    assert len(transport.calls) == 8


def test_400_is_not_retried():
    client, transport, _, _ = make_client({"m1": [TransportError(400, "INVALID_ARGUMENT")]})
    with pytest.raises(LLMBadRequest):
        run(client.generate(request()))
    assert len(transport.calls) == 1


def test_400_with_thinking_retries_once_without_thinking():
    client, transport, _, _ = make_client({"c1": [TransportError(400, "thinking_level not supported"), GOOD]})
    run(client.generate(request(pool="cheap", thinking="minimal")))
    assert [c.thinking for c in transport.calls] == ["minimal", "default"]


# --- output validation, blocking, budget --------------------------------------------------------


def test_invalid_json_gets_one_repair_attempt_with_errors():
    cache = InMemoryLLMCache()
    client, transport, usage, _ = make_client({"m1": [BAD, GOOD]}, cache=cache)
    result = run(client.generate(request()))
    assert result.repaired and result.output.answers[0].answer == "yes"
    assert "<validation_error>" in transport.calls[1].contents and "answer" in transport.calls[1].contents
    assert [c.status for c in usage.calls] == ["invalid_output", "ok"]
    # the repaired answer is cached under the original request
    assert run(client.generate(request())).cache_hit


def test_invalid_twice_raises_extraction_failed():
    client, _, _, _ = make_client({"m1": ["not json", BAD]})
    with pytest.raises(ExtractionFailed):
        run(client.generate(request()))


def test_safety_block_returns_blocked_result_and_is_not_cached():
    cache = InMemoryLLMCache()
    blocked = TransportResponse(text=None, finish_reason="SAFETY")
    client, _, usage, _ = make_client({"m1": [blocked]}, cache=cache)
    result = run(client.generate(request()))
    assert result.blocked and result.output is None
    assert usage.calls[0].status == "blocked" and cache.entries == {}


def test_input_over_budget_is_rejected_before_any_call():
    client, transport, _, _ = make_client({}, max_input_tokens=10)
    with pytest.raises(LLMInputTooLarge):
        run(client.generate(request(user="x" * 100)))
    assert transport.calls == []


# --- settings -----------------------------------------------------------------------------------


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "secret")
    monkeypatch.setenv("LLM_MAIN_MODELS", "gemini-a, gemini-b")
    monkeypatch.setenv("LLM_LIMITS_JSON", '{"gemini-a": {"rpm": 10, "rpd": 250}}')
    monkeypatch.setenv("LLM_CACHE_ENABLED", "false")
    s = LLMSettings(_env_file=None)
    assert s.api_key.get_secret_value() == "secret"
    assert s.pool("main") == ["gemini-a", "gemini-b"]
    assert (s.limits("gemini-a").rpm, s.limits("gemini-a").rpd) == (10, 250)
    assert s.limits("unknown").rpd is None
    assert not s.cache_enabled
    monkeypatch.setenv("GEMINI_API_KEY", "preferred")
    assert LLMSettings(_env_file=None).api_key.get_secret_value() == "preferred"


def test_from_settings_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiClient.from_settings(LLMSettings(_env_file=None), InMemoryUsageSink())


# --- google-genai adapter (no network) ----------------------------------------------------------


def test_build_config_uses_json_schema_and_no_sampling_params():
    config = build_config("sys", Output, "minimal")
    assert config.system_instruction == "sys"
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == Output.model_json_schema()
    assert config.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL
    assert config.temperature is None and config.top_p is None and config.top_k is None
    assert build_config("sys", Output, "default").thinking_config is None
    assert config.automatic_function_calling.disable is True


def test_api_error_mapping():
    per_minute = errors.APIError(
        429,
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17.5s"},
                ],
            }
        },
    )
    e = to_transport_error(per_minute)
    assert (e.status, e.retry_after_s, e.daily_quota) == (429, 17.5, False)

    per_day = errors.APIError(
        429,
        {
            "error": {
                "code": 429,
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
                    }
                ],
            }
        },
    )
    assert to_transport_error(per_day).daily_quota

    e = to_transport_error(errors.APIError(503, {"error": {"code": 503, "message": "overloaded"}}))
    assert (e.status, e.retry_after_s, e.daily_quota) == (503, None, False)


def test_response_mapping():
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=GOOD)]),
                finish_reason=types.FinishReason.STOP,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1200, candidates_token_count=150, thoughts_token_count=50
        ),
    )
    assert to_response(response) == TransportResponse(
        text=GOOD, finish_reason="STOP", input_tokens=1200, output_tokens=200
    )
    blocked = types.GenerateContentResponse(
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason=types.BlockedReason.SAFETY)
    )
    assert to_response(blocked).finish_reason == "PROMPT_BLOCKED"


def test_genai_transport_calls_sdk_and_maps_errors():
    class Models:
        def __init__(self, outcome):
            self.outcome, self.kwargs = outcome, None

        async def generate_content(self, **kwargs):
            self.kwargs = kwargs
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    class Client:
        def __init__(self, outcome):
            self.aio = type("Aio", (), {})()
            self.aio.models = Models(outcome)

    ok = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=GOOD)]),
                finish_reason=types.FinishReason.STOP,
            )
        ]
    )
    client = Client(ok)
    result = run(
        GenaiTransport(client=client).generate(
            model="m", system="s", contents="c", output_model=Output, thinking="default"
        )
    )
    assert result.text == GOOD
    assert client.aio.models.kwargs["model"] == "m" and client.aio.models.kwargs["contents"] == "c"

    failing = Client(errors.APIError(500, {"error": {"code": 500, "message": "boom"}}))
    with pytest.raises(TransportError) as exc:
        run(
            GenaiTransport(client=failing).generate(
                model="m", system="s", contents="c", output_model=Output, thinking="default"
            )
        )
    assert exc.value.status == 500


# --- FakeLLM ------------------------------------------------------------------------------------


def test_fake_llm_queue_handler_blocked_and_errors():
    fake = FakeLLM([{"answers": []}, BLOCKED, QuotaExhausted("main")])
    assert isinstance(fake, LLMClient)
    assert run(fake.generate(request())).output == Output(answers=[])
    assert run(fake.generate(request())).blocked
    with pytest.raises(QuotaExhausted):
        run(fake.generate(request()))
    with pytest.raises(AssertionError, match="no response queued"):
        run(fake.generate(request()))
    assert len(fake.calls_for("extract_signals")) == 4

    by_purpose = FakeLLM(lambda r: Output(answers=[Answer(question_id=r.purpose, answer="no", confidence=1)]))
    assert run(by_purpose.generate(request())).output.answers[0].question_id == "extract_signals"


def test_gemini_client_implements_llm_client():
    client, *_ = make_client({})
    assert isinstance(client, LLMClient)


# --- provider pools -----------------------------------------------------------------------------


def test_gemini_pools_keep_the_fallback_that_answered_live(monkeypatch):
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    s = LLMSettings(_env_file=None, GEMINI_API_KEY="k")
    assert s.resolved_provider == "gemini"
    assert s.pool("main") == ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash"]


def test_other_providers_get_their_own_models_instead_of_gemini_names(monkeypatch):
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    groq = LLMSettings(_env_file=None, GROQ_API_KEY="k")
    assert groq.resolved_provider == "groq"
    assert groq.pool("main")[0].startswith("llama") and groq.pool("cheap")[0].startswith("llama")
    custom = LLMSettings(_env_file=None, GROQ_API_KEY="k", main_models="my-model")
    assert custom.pool("main") == ["my-model"]  # explicit pools are respected
