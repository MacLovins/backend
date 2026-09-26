"""CO-16 / CO-21: reference data from parser + ai, LLM usage for the Pacific-time quota day."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import leadradar_ai as ai
import leadradar_parser as parser
import pytest
from fastapi.testclient import TestClient
from leadradar_auth.schemas import Principal
from leadradar_auth.security import create_access_token
from leadradar_core.main import create_app
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.intelligence.models import Document
from leadradar_core.modules.meta import service as meta_service
from leadradar_core.modules.meta.models import LLMCall
from leadradar_core.modules.meta.router import get_llm_settings
from leadradar_core.settings import settings
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings.ENV = "test"
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    principal = Principal(
        user_id=uuid4(), org_id=settings.DEFAULT_ORG_ID, email="sales@leadradar.ai", role="sales"
    )
    return {"Authorization": f"Bearer {create_access_token(principal)}"}


def _run_db(fn):
    """Run `fn(session)` on a private engine (the app engine belongs to the TestClient loop)."""

    async def main():
        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(main())


@pytest.mark.parametrize("path", ["industries", "countries", "presets", "labels", "usage"])
def test_meta_requires_auth(client: TestClient, path: str) -> None:
    res = client.get(f"/api/v1/meta/{path}")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthorized"


def test_meta_industries_from_parser_taxonomy(client: TestClient, auth_headers: dict[str, str]) -> None:
    res = client.get("/api/v1/meta/industries", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert [i["id"] for i in data] == [i.id for i in parser.industry_taxonomy()]
    assert len(data) == 27
    ids = {i["id"] for i in data}
    assert "energy_utilities" in ids and "energy" not in ids


def test_meta_countries_from_parser_catalog(client: TestClient, auth_headers: dict[str, str]) -> None:
    res = client.get("/api/v1/meta/countries", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert [c["code"] for c in data] == [c.code for c in parser.country_catalog()]
    de = next(c for c in data if c["code"] == "DE")
    assert de["name"] == "Germany" and de["is_eu"] is True


def test_meta_presets(client: TestClient, auth_headers: dict[str, str]) -> None:
    res = client.get("/api/v1/meta/presets", headers=auth_headers)
    assert res.status_code == 200
    data = {p["key"]: p for p in res.json()}
    assert set(data) == set(ai.list_presets())
    assert data["intelligent_automation"]["name"] == ai.load_preset("intelligent_automation").name
    assert data["cybersecurity"]["questions_count"] > 0


def test_meta_labels_from_ai(client: TestClient, auth_headers: dict[str, str]) -> None:
    res = client.get("/api/v1/meta/labels", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["categories"] == dict(ai.SIGNAL_CATEGORIES)
    assert "leadership_change" in data["categories"] and "leadership" not in data["categories"]
    assert set(data["weights"]) == {"high", "medium", "low"}
    assert set(data["tiers"]) == {"hot", "warm", "cold", "disqualified"}
    assert set(data["source_types"]) == {
        "news",
        "website",
        "jobs",
        "report",
        "registry",
        "incident",
        "derived",
        "manual",
    }
    assert "paused" in data["stages"]
    assert set(data["roles"]) == {"admin", "sales"}
    assert "quote_not_found" in data["reject_reasons"]


def test_usage_quota_day_is_pacific() -> None:
    # 06:00 UTC on Sep 26 is 23:00 PDT on Sep 25 → the quota day started at 07:00 UTC on Sep 25
    start, end = meta_service.quota_day(datetime(2026, 9, 26, 6, 0, tzinfo=UTC))
    assert start.astimezone(UTC) == datetime(2026, 9, 25, 7, 0, tzinfo=UTC)
    assert end - start == timedelta(days=1)
    # winter (PST, UTC-8)
    start, _ = meta_service.quota_day(datetime(2026, 1, 10, 12, 0, tzinfo=UTC))
    assert start.astimezone(UTC) == datetime(2026, 1, 10, 8, 0, tzinfo=UTC)


def test_usage_counts_llm_calls_against_limits() -> None:
    model = f"test-main-{uuid4().hex[:8]}"
    other = f"test-other-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    start, _ = meta_service.quota_day(now)
    llm_settings = ai.LLMSettings(
        main_models=[model], cheap_models=["test-cheap"], limits_json={model: {"rpm": 10, "rpd": 100}}
    )

    def call(model_name: str, at: datetime, status: str = "ok", tokens: int = 10, cache_hit: bool = False):
        return LLMCall(
            org_id=settings.DEFAULT_ORG_ID,
            purpose="extract_signals",
            model=model_name,
            prompt_version="t1",
            input_tokens=tokens,
            output_tokens=tokens // 2,
            status=status,
            cache_hit=cache_hit,
            created_at=at,
            updated_at=at,
        )

    async def scenario(session: AsyncSession):
        session.add_all(
            [
                call(model, start + timedelta(seconds=1)),
                call(model, now, tokens=100),
                call(model, now, status="cache_hit", cache_hit=True),
                call(model, now, status="error"),
                call(model, start - timedelta(minutes=1), tokens=5000),  # previous quota day
                call(other, now, tokens=7),
            ]
        )
        await session.commit()
        try:
            return await meta_service.usage(
                session, settings.DEFAULT_ORG_ID, llm_settings=llm_settings, now=now
            )
        finally:
            await session.execute(delete(LLMCall).where(LLMCall.model.in_([model, other])))
            await session.commit()

    usage = _run_db(scenario)
    by_model = {m.model: m for m in usage.models}
    main = by_model[model]
    assert main.pool == "main"
    assert (main.calls, main.quota_calls, main.cache_hits, main.errors) == (4, 3, 1, 1)
    assert main.input_tokens == 10 + 100 + 10 + 10
    assert (main.rpd_limit, main.rpm_limit, main.remaining) == (100, 10, 97)
    assert by_model["test-cheap"].pool == "cheap" and by_model["test-cheap"].calls == 0
    assert (
        by_model[other].calls == 1 and by_model[other].rpd_limit is None and by_model[other].remaining is None
    )
    assert usage.llm_calls_24h >= 5
    assert usage.day_start == start


def test_usage_endpoint(client: TestClient, auth_headers: dict[str, str]) -> None:
    domain = f"usage-{uuid4().hex[:8]}.com"
    model = f"test-api-{uuid4().hex[:8]}"

    async def seed(session: AsyncSession):
        company = Company(org_id=settings.DEFAULT_ORG_ID, name="Usage test", domain=domain)
        session.add(company)
        await session.flush()
        for i, source in enumerate(["news", "news", "jobs"]):
            session.add(
                Document(
                    org_id=settings.DEFAULT_ORG_ID,
                    company_id=company.id,
                    source_type=source,
                    source_name="test",
                    url=f"https://{domain}/{i}",
                    canonical_url=f"https://{domain}/{i}",
                    text="text",
                    content_hash=f"h{i}",
                    fetched_at=datetime.now(UTC),
                )
            )
        session.add(
            LLMCall(
                org_id=settings.DEFAULT_ORG_ID, purpose="x", model=model, prompt_version="t", input_tokens=42
            )
        )
        await session.commit()
        return company.id

    async def cleanup(session: AsyncSession):
        await session.execute(delete(LLMCall).where(LLMCall.model == model))
        await session.execute(delete(Company).where(Company.domain == domain))
        await session.commit()

    client.app.dependency_overrides[get_llm_settings] = lambda: ai.LLMSettings(
        main_models=[model], cheap_models=[], limits_json={model: {"rpd": 50}}
    )
    before = client.get("/api/v1/meta/usage", headers=auth_headers).json()
    _run_db(seed)
    try:
        res = client.get("/api/v1/meta/usage", headers=auth_headers)
        assert res.status_code == 200
        data = res.json()
        entry = next(m for m in data["models"] if m["model"] == model)
        assert (entry["calls"], entry["input_tokens"], entry["rpd_limit"], entry["remaining"]) == (
            1,
            42,
            50,
            49,
        )
        assert data["documents_by_source"]["news"] - before["documents_by_source"].get("news", 0) == 2
        assert data["documents_by_source"]["jobs"] - before["documents_by_source"].get("jobs", 0) == 1
        assert data["documents_scanned_24h"] - before["documents_scanned_24h"] == 3
        assert data["resets_at"] and data["day_start"]
    finally:
        _run_db(cleanup)
        client.app.dependency_overrides.clear()
