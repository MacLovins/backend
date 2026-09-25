import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from conftest import make_company, make_plan, make_settings
from leadradar_parser import CollectPlan, Document, HttpClient, RateLimit, ResolvedCompany, collect
from leadradar_parser.adapters.common import make_document
from leadradar_parser.errors import SourceRateLimited

LIMIT = RateLimit(requests=1, per_seconds=1)


def document(url: str, text: str) -> Document:
    return make_document(source_type="news", source_name="fake", url=url, text=text)


class FakeAdapter:
    source_type = "news"
    requires_env: str | None = None
    rate_limit = LIMIT

    def __init__(self, adapter_id: str, *documents: Document, error: Exception | None = None) -> None:
        self.id = adapter_id
        self.documents = documents
        self.error = error

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        for item in self.documents:
            yield item
        if self.error:
            raise self.error


class HangingAdapter(FakeAdapter):
    def __init__(self, adapter_id: str, *documents: Document) -> None:
        super().__init__(adapter_id, *documents)
        self.cancelled = False

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        for item in self.documents:
            yield item
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def install(monkeypatch: pytest.MonkeyPatch, *adapters: FakeAdapter) -> None:
    import leadradar_parser.collector as collector

    monkeypatch.setattr(collector, "ADAPTERS", {adapter.id: adapter for adapter in adapters})


async def test_collect_isolates_a_failing_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = FakeAdapter("good", document("https://a.test/1", "one"), document("https://a.test/2", "two"))
    limited = FakeAdapter(
        "limited", document("https://b.test/1", "partial"), error=SourceRateLimited("429", retry_after_s=60)
    )
    broken = FakeAdapter("broken", error=KeyError("jobs"))
    install(monkeypatch, good, limited, broken)
    settings = make_settings(tmp_path, adapters=["good", "limited", "broken"])

    async with HttpClient(settings) as http:
        result = await collect(make_company(), make_plan("news"), http=http)

    assert sorted(item.text for item in result.documents) == ["one", "partial", "two"]
    assert result.stats == {"good": 2, "limited": 1, "broken": 0}
    errors = {error.adapter: error for error in result.errors}
    assert errors["limited"].kind == "rate_limited" and errors["limited"].retry_after_s == 60
    assert errors["broken"].kind == "parse_error"
    assert "good" not in errors


async def test_collect_times_out_one_adapter_and_awaits_its_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast = FakeAdapter("fast", document("https://a.test/1", "fast"))
    hanging = HangingAdapter("hanging", document("https://b.test/1", "before the hang"))
    install(monkeypatch, fast, hanging)
    settings = make_settings(tmp_path, adapters=["fast", "hanging"])

    async with HttpClient(settings) as http:
        result = await collect(make_company(), make_plan("news", time_budget_s=1), http=http)

    assert hanging.cancelled  # cancelled *and* awaited before collect() returned
    assert sorted(item.text for item in result.documents) == ["before the hang", "fast"]
    assert [(error.adapter, error.kind) for error in result.errors] == [("hanging", "timeout")]
    assert result.stats == {"fast": 1, "hanging": 1}
    assert 1_000 <= result.duration_ms < 5_000
    assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]


async def test_collect_respects_enabled_adapters_source_types_and_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyed = FakeAdapter("keyed", document("https://k.test/1", "keyed"))
    keyed.requires_env = "FAKE_NEWS_KEY"
    jobs = FakeAdapter("jobs", document("https://j.test/1", "job"))
    jobs.source_type = "jobs"
    disabled = FakeAdapter("off", document("https://o.test/1", "off"))
    install(monkeypatch, keyed, jobs, disabled)
    monkeypatch.delenv("FAKE_NEWS_KEY", raising=False)
    settings = make_settings(tmp_path, adapters=["keyed", "jobs"])

    async with HttpClient(settings) as http:
        result = await collect(make_company(), make_plan("news"), http=http)

    assert result.documents == []
    assert [(error.adapter, error.kind) for error in result.errors] == [("keyed", "disabled")]


async def test_collect_deduplicates_across_adapters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = FakeAdapter("first", document("https://a.test/story?utm_source=x", "Same story"))
    second = FakeAdapter("second", document("https://mirror.test/story", "same   STORY"))
    install(monkeypatch, first, second)
    async with HttpClient(make_settings(tmp_path, adapters=["first", "second"])) as http:
        result = await collect(make_company(), make_plan("news"), http=http)
    assert len(result.documents) == 1
    assert result.stats == {"first": 1, "second": 1}


@respx.mock
async def test_collect_with_real_adapters_survives_source_500(tmp_path: Path) -> None:
    """End-to-end over the registry: ATS answers 500, the website still yields documents."""
    long_text = " ".join(["Our strategy 2030 invests in automation and digital transformation."] * 10)
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/sitemap_index.xml").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/strategy").mock(
        return_value=httpx.Response(
            200, html=f"<html><body><article><p>{long_text}</p></article></body></html>"
        )
    )
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(200, html="<a href='/strategy'>Strategy</a>")
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/example/jobs").mock(
        return_value=httpx.Response(500)
    )
    settings = make_settings(tmp_path, adapters=["website", "jobs_ats"])
    company = make_company(ats={"kind": "greenhouse", "token": "example"})

    async with HttpClient(settings) as http:
        result = await collect(company, make_plan("website", "jobs"), http=http)

    assert [item.meta["page_kind"] for item in result.documents] == ["strategy"]
    assert [(error.adapter, error.kind) for error in result.errors] == [("jobs_ats", "not_found")]
