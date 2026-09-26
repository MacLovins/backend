from datetime import UTC, datetime

import pytest
from leadradar_parser import CollectPlan, CompanyRef
from pydantic import ValidationError


def test_contract_defaults_are_not_shared() -> None:
    first = CompanyRef(name="One", domain="https://WWW.Example.com/path")
    second = CompanyRef(name="Two", domain="two.example")
    first.aliases.append("Alias")

    assert first.domain == "example.com"
    assert second.aliases == []


def test_contracts_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CompanyRef(name="One", domain="example.com", surprise=True)


def test_collect_plan_converts_naive_since_to_utc() -> None:
    plan = CollectPlan(source_types={"news"}, since=datetime(2026, 1, 1))
    assert plan.since.tzinfo is UTC


def test_settings_parse_comma_separated_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    from leadradar_parser import ParserSettings, list_adapters

    monkeypatch.setenv("PARSER_ADAPTERS", "gdelt, website")
    monkeypatch.setenv("PARSER_MAX_CONCURRENCY", "3")
    settings = ParserSettings()
    assert settings.adapters == ["gdelt", "website"] and settings.max_concurrency == 3
    assert {item.id for item in list_adapters() if item.enabled} == {"gdelt", "website"}


def test_source_keys_are_read_from_env_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """core runs the parser outside Docker too: keys in .env must reach adapters without os.environ."""
    from leadradar_parser import ParserSettings, list_adapters

    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("NEWSAPI_KEY=from-dotenv\nPARSER_ADAPTERS=newsapi,gdelt\n")
    settings = ParserSettings(_env_file=env_file)
    assert settings.env("NEWSAPI_KEY") == "from-dotenv"
    assert {item.id for item in list_adapters(settings) if item.enabled} == {"newsapi", "gdelt"}
