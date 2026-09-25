import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from leadradar_parser import CollectPlan, HttpClient, ParserSettings, ResolvedCompany

FIXTURES = Path(__file__).parent / "fixtures" / "http"
# Fixed "now" for plans so recorded fixtures never age out of the collection window.
SINCE = datetime(2026, 9, 1, tzinfo=UTC)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> object:
    return json.loads(fixture_text(name))


def make_settings(tmp_path: Path, **updates: object) -> ParserSettings:
    values: dict[str, object] = {
        "cache_dir": tmp_path / "cache",
        "host_rps": 1_000,
        "retry_attempts": 2,
        "retry_min_wait_s": 0,
        "retry_max_wait_s": 0,
    }
    values.update(updates)
    return ParserSettings(**values)


def make_company(**updates: object) -> ResolvedCompany:
    values: dict[str, object] = {
        "name": "Example AG",
        "domain": "example.com",
        "homepage_url": "https://example.com",
        "own_domains": ["example.com"],
        "resolved_at": datetime(2026, 9, 25, tzinfo=UTC),
    }
    values.update(updates)
    return ResolvedCompany.model_validate(values)


def make_plan(*source_types: str, **updates: object) -> CollectPlan:
    return CollectPlan(source_types=set(source_types), since=SINCE, **updates)


@pytest.fixture
def settings(tmp_path: Path) -> ParserSettings:
    return make_settings(tmp_path)


@pytest.fixture
async def http(settings: ParserSettings) -> AsyncIterator[HttpClient]:
    async with HttpClient(settings) as client:
        yield client


@pytest.fixture(autouse=True)
def reset_gdelt_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import leadradar_parser.adapters.news_gdelt as gdelt

    monkeypatch.setattr(gdelt, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(gdelt, "_last_request_at", 0.0)
    monkeypatch.setattr(gdelt, "_blocked_until", 0.0)
    monkeypatch.setattr(gdelt, "_consecutive_429", 0)


@pytest.fixture(autouse=True)
def no_parser_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not depend on the developer's .env / PARSER_* variables."""
    for key in ("PARSER_ADAPTERS", "PARSER_CACHE_DIR", "PARSER_HOST_RPS", "PARSER_MAX_CONCURRENCY"):
        monkeypatch.delenv(key, raising=False)
