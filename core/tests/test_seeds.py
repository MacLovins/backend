"""Demo seed accounts use only taxonomy ids and known countries; .env.example uses the real prefixes."""

import csv
from pathlib import Path

import leadradar_parser as parser
from dotenv import dotenv_values
from leadradar_auth import AuthSettings, insecure_jwt_secret_reason
from leadradar_core.settings import AppSettings

ROOT = Path(__file__).resolve().parents[2]


def test_seed_accounts_industries_in_taxonomy() -> None:
    ids = {i.id for i in parser.industry_taxonomy()}
    countries = {c.code for c in parser.country_catalog()}
    rows = list(csv.DictReader((ROOT / "seeds" / "demo_accounts.csv").open(encoding="utf-8-sig")))
    assert len(rows) >= 40
    for row in rows:
        industries = [i for i in row["industry_ids"].split(";") if i]
        assert industries, row["name"]
        assert set(industries) <= ids, (row["name"], industries)
        assert row["country_code"] in countries, row["name"]
    by_name = {r["name"]: r["industry_ids"] for r in rows}
    assert by_name["SAP SE"] == "software"
    assert by_name["Lufthansa Group"] == "airlines"
    assert by_name["Novartis AG"] == "pharma"
    assert sum(r["industry_ids"] == "energy_utilities" for r in rows) == 5


def test_env_example_uses_prefixes_that_settings_read() -> None:
    values = dotenv_values(ROOT / ".env.example")
    app_fields = set(AppSettings.model_fields)
    for key in ("DATABASE_URL", "REDIS_URL", "REFRESH_CRON", "LANGGRAPH_DB_URL", "WORKER_MAX_ASYNC_TASKS"):
        assert key not in values, f"{key} is ignored by core; use APP_{key}"
        assert f"APP_{key}" in values
        assert key in app_fields
    assert not [k for k in values if k.startswith("FEATURE_")]
    for key in values:
        if key.startswith("APP_") and not key.startswith(("APP_TELEGRAM", "APP_HUBSPOT")):
            assert key.removeprefix("APP_") in app_fields, key
        if key.startswith("AUTH_"):
            assert key.removeprefix("AUTH_") in AuthSettings.model_fields, key
    # the placeholder secret is refused outside dev
    assert insecure_jwt_secret_reason(values["AUTH_JWT_SECRET"] or "") is not None
