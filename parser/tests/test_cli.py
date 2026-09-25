import json
from pathlib import Path

import pytest
from conftest import make_company
from leadradar_parser import CollectResult, CompanyCandidate, Document
from leadradar_parser.adapters.common import make_document
from leadradar_parser.cli import app
from typer.testing import CliRunner

runner = CliRunner()


def test_cli_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("adapters", "resolve", "collect", "discover"):
        assert command in result.output


def test_cli_adapters_prints_json_lines() -> None:
    result = runner.invoke(app, ["adapters"])
    assert result.exit_code == 0
    rows = [json.loads(line) for line in result.output.splitlines()]
    assert {row["id"] for row in rows} == {"gdelt", "website", "jobs_ats", "careers_html", "wikidata"}
    assert next(row for row in rows if row["id"] == "gdelt")["rate_limit"]["per_seconds"] == 5


def test_cli_collect_writes_valid_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import leadradar_parser.cli as cli

    seen: dict[str, object] = {}

    async def fake_resolve(ref, *, http=None):  # type: ignore[no-untyped-def]
        return make_company(name=ref.name, domain=ref.domain)

    async def fake_collect(company, plan, *, http=None):  # type: ignore[no-untyped-def]
        seen.update(types=plan.source_types, adapters=http.settings.adapters, budget=plan.time_budget_s)
        document = make_document(
            source_type="news", source_name="gdelt", url="https://a.test/1", text="Hello"
        )
        return CollectResult(documents=[document], errors=[], stats={"gdelt": 1}, duration_ms=5)

    monkeypatch.setattr(cli, "resolve_company", fake_resolve)
    monkeypatch.setattr(cli, "collect", fake_collect)
    monkeypatch.setenv("PARSER_CACHE_DIR", str(tmp_path / "cache"))
    out = tmp_path / "out" / "dhl.jsonl"
    result = runner.invoke(
        app,
        [
            "collect",
            "--name",
            "DHL Group",
            "--domain",
            "dhl.com",
            "--sources",
            "gdelt,website",
            "--since",
            "90d",
            "--time-budget",
            "30",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    [line] = out.read_text().splitlines()
    assert Document.model_validate_json(line).source_name == "gdelt"
    assert seen == {"types": {"news", "website"}, "adapters": ["gdelt", "website"], "budget": 30}
    assert json.loads(result.stderr.splitlines()[-1])["stats"] == {"gdelt": 1}


@pytest.mark.parametrize(
    "args",
    [
        ["--sources", "linkedin"],
        ["--since", "yesterday"],
    ],
)
def test_cli_collect_rejects_bad_input(args: list[str]) -> None:
    result = runner.invoke(app, ["collect", "--name", "X", "--domain", "x.com", *args])
    assert result.exit_code == 2


def test_cli_discover(monkeypatch: pytest.MonkeyPatch) -> None:
    import leadradar_parser.cli as cli

    async def fake_discover(query):  # type: ignore[no-untyped-def]
        assert query.countries == ["DE", "AT"] and query.employees_min == 5000
        return [
            CompanyCandidate(
                name="DHL Group",
                domain="dhl.com",
                country_code="DE",
                industry_ids=["logistics"],
                employees=1,
                revenue_eur=None,
                wikidata_qid="Q1",
                lei=None,
                crunchbase_id=None,
            )
        ]

    monkeypatch.setattr(cli, "discover", fake_discover)
    result = runner.invoke(
        app,
        ["discover", "--countries", "DE,AT", "--industries", "logistics,airlines", "--min-employees", "5000"],
    )
    assert result.exit_code == 0
    assert json.loads(result.output)["domain"] == "dhl.com"
