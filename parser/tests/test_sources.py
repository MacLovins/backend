"""Offline tests for the report, incident, GLEIF, Adzuna and additional ATS sources (respx fixtures)."""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
import respx
from conftest import fixture_bytes, fixture_json, make_company, make_plan
from leadradar_parser import (
    AtsRef,
    CompanyRef,
    Firmographics,
    HttpClient,
    ParserSettings,
    collect,
    resolve_company,
)

NO_ROBOTS = httpx.Response(404)
GLEIF = "https://api.gleif.org/api/v1"


async def run(adapter, company, plan, http: HttpClient):  # type: ignore[no-untyped-def]
    return [item async for item in adapter.fetch(company, plan, http)]


@pytest.fixture(autouse=True)
def reset_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import leadradar_parser.adapters.incident_hibp as hibp
    import leadradar_parser.adapters.jobs_adzuna as adzuna

    monkeypatch.setattr(hibp, "_catalogue", None)
    adzuna._calls.clear()
    yield
    adzuna._calls.clear()


# --- jobs_ats: Ashby, SmartRecruiters, Workable, Recruitee ---------------------------------------------


@respx.mock
async def test_ats_ashby_listed_jobs_keyword_first(http: HttpClient) -> None:
    from leadradar_parser.adapters.jobs_ats import JobsAtsAdapter

    respx.get("https://api.ashbyhq.com/posting-api/job-board/example").mock(
        return_value=httpx.Response(200, json=fixture_json("ashby_job_board.json"))
    )
    company = make_company(ats=AtsRef(kind="ashby", token="example"))
    docs = await run(JobsAtsAdapter(), company, make_plan("jobs", job_keywords=["security"]), http)

    assert [doc.title for doc in docs] == ["Security Engineer, Cloud", "Office Manager"]  # unlisted skipped
    security = docs[0]
    assert (
        security.source_name == "ashby" and "SIEM detections" in security.text and "<p>" not in security.text
    )
    assert security.meta == {
        "location": "Remote (EU)",
        "department": "Engineering",
        "employment_type": "FullTime",
        "ats": "ashby",
    }
    assert security.published_at == datetime(2026, 9, 20, 17, 12, 35, 753000, tzinfo=UTC)


@respx.mock
async def test_ats_smartrecruiters_list_and_details(http: HttpClient) -> None:
    from leadradar_parser.adapters.jobs_ats import JobsAtsAdapter

    base = "https://api.smartrecruiters.com/v1/companies/ExampleGroup/postings"
    listing = respx.get(base).mock(
        return_value=httpx.Response(200, json=fixture_json("smartrecruiters_postings.json"))
    )
    respx.get(f"{base}/744000151900001").mock(
        return_value=httpx.Response(200, json=fixture_json("smartrecruiters_posting.json"))
    )
    respx.get(f"{base}/744000151928744").mock(return_value=httpx.Response(404))
    company = make_company(ats=AtsRef(kind="smartrecruiters", token="ExampleGroup"))
    docs = await run(JobsAtsAdapter(), company, make_plan("jobs", job_keywords=["RPA"]), http)

    assert listing.calls[0].request.url.params["limit"] == "100"
    rpa, design = docs
    assert rpa.title == "RPA Automation Developer" and "Build UiPath bots" in rpa.text
    assert "We build machines" not in rpa.text  # company boilerplate is not part of the job text
    assert rpa.url.endswith("744000151900001-rpa-automation-developer")
    assert rpa.meta["department"] == "IT" and rpa.meta["headline_only"] is False
    assert design.meta["headline_only"] is True and design.meta["department"] == "Engineering"
    assert design.url == "https://jobs.smartrecruiters.com/ExampleGroup/744000151928744"


@respx.mock
async def test_ats_workable_widget_with_details(http: HttpClient) -> None:
    from leadradar_parser.adapters.jobs_ats import JobsAtsAdapter

    route = respx.get("https://apply.workable.com/api/v1/widget/accounts/example").mock(
        return_value=httpx.Response(200, json=fixture_json("workable_account.json"))
    )
    company = make_company(ats=AtsRef(kind="workable", token="example"))
    [doc] = await run(JobsAtsAdapter(), company, make_plan("jobs"), http)

    assert route.calls[0].request.url.params["details"] == "true"
    assert doc.source_name == "workable" and doc.url == "https://apply.workable.com/j/F4C096B22E"
    assert "written in Rust" in doc.text and doc.meta["location"] == "Paris, France"
    assert doc.published_at == datetime(2026, 9, 14, tzinfo=UTC)


@respx.mock
async def test_ats_recruitee_published_offers_and_utc_suffix_dates(http: HttpClient) -> None:
    from leadradar_parser.adapters.jobs_ats import JobsAtsAdapter

    respx.get("https://example.recruitee.com/api/offers/").mock(
        return_value=httpx.Response(200, json=fixture_json("recruitee_offers.json"))
    )
    company = make_company(ats=AtsRef(kind="recruitee", token="example", host="example.recruitee.com"))
    [doc] = await run(JobsAtsAdapter(), company, make_plan("jobs"), http)

    assert doc.title == "(Senior) Legal Counsel" and "Qualified lawyer" in doc.text
    assert doc.published_at == datetime(2026, 9, 25, 15, 46, 7, tzinfo=UTC)
    assert doc.meta["employment_type"] == "fulltime_permanent"


def test_supported_ats_covers_every_ats_kind_so_careers_html_stays_a_fallback() -> None:
    from typing import get_args

    from leadradar_parser import AtsKind
    from leadradar_parser.adapters.jobs_ats import SUPPORTED_ATS

    assert set(get_args(AtsKind)) == SUPPORTED_ATS


# --- reports (PDF) -------------------------------------------------------------------------------------

IR_PAGE = """<html><body>
<a href="/investors/reports/annual-report-2025.pdf">Annual Report 2025 (PDF, 1 MB)</a>
<a href="/investors/reports/annual-report-2019.pdf">Annual Report 2019</a>
<a href="/legal/agb.pdf">AGB</a>
<a href="https://cdn.other.example/brochure-report-2025.pdf">Partner report</a>
</body></html>"""


@respx.mock
async def test_reports_finds_pdf_on_ir_page_and_keeps_keyword_pages_with_neighbours(http: HttpClient) -> None:
    from leadradar_parser.adapters.web_reports import ReportsAdapter

    respx.get("https://example.com/robots.txt").mock(return_value=NO_ROBOTS)
    respx.get("https://example.com/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            content=b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.com/investors/reports</loc></url>
            <url><loc>https://example.com/products</loc></url></urlset>""",
        )
    )
    respx.get("https://example.com/sitemap_index.xml").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(200, html="<a href='/investors'>Investors</a>")
    )
    respx.get("https://example.com/investors").mock(return_value=httpx.Response(200, html="<p>IR</p>"))
    respx.get("https://example.com/investors/reports").mock(return_value=httpx.Response(200, html=IR_PAGE))
    pdf = respx.get("https://example.com/investors/reports/annual-report-2025.pdf").mock(
        return_value=httpx.Response(
            200, content=fixture_bytes("report_annual_2025.pdf"), headers={"Content-Type": "application/pdf"}
        )
    )
    old = respx.get("https://example.com/investors/reports/annual-report-2019.pdf")

    [doc] = await run(ReportsAdapter(), make_company(), make_plan("report", news_topics=["automation"]), http)

    assert pdf.called and not old.called  # reports older than two years are not downloaded
    assert doc.source_type == "report" and doc.title == "Example AG Annual Report 2025"
    assert doc.meta["pages"] == [1, 2, 3] and doc.meta["page_count"] == 4
    assert "Strategy 2030" in doc.text and "Imprint" not in doc.text
    assert doc.meta["source_page"] == "https://example.com/investors/reports"
    assert doc.published_at == datetime(2026, 3, 12, 10, 10, 10, tzinfo=UTC)
    assert doc.meta["date_source"] == "pdf_metadata"


@respx.mock
async def test_reports_size_limit_becomes_source_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from conftest import make_settings

    settings = make_settings(tmp_path, adapters=["reports"], reports_max_pdf_mb=0.001)
    respx.get("https://example.com/robots.txt").mock(return_value=NO_ROBOTS)
    respx.get(url__regex=r"^https://example\.com/sitemap.*").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(200, html="<a href='/ir/geschaeftsbericht-2025.pdf'>Geschäftsbericht</a>")
    )
    respx.get("https://example.com/ir/geschaeftsbericht-2025.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-" + b"x" * 5_000)
    )
    async with HttpClient(settings) as client:
        result = await collect(make_company(), make_plan("report"), http=client)
    assert result.documents == []
    [error] = result.errors
    assert error.adapter == "reports" and error.kind == "not_found" and "limit 1048" in error.message


def test_reports_keyword_pattern_and_pdf_dates() -> None:
    from leadradar_parser.adapters.web_reports import keyword_pattern, pdf_date

    pattern = keyword_pattern(["AI", "automation", "Künstliche Intelligenz"])
    assert pattern.search("We use AI.") and not pattern.search("said the chair")
    assert not pattern.search("ai is lowercase") and pattern.search("AUTOMATION programme")
    assert pattern.search("Einsatz Künstliche Intelligenz")
    assert pdf_date("D:20260312101010+01'00'") == datetime(2026, 3, 12, 10, 10, 10, tzinfo=UTC)
    assert pdf_date("D:2025") == datetime(2025, 1, 1, tzinfo=UTC) and pdf_date("garbage") is None


# --- hibp ----------------------------------------------------------------------------------------------


@respx.mock
async def test_hibp_matches_own_domains_only_with_attribution(http: HttpClient) -> None:
    from leadradar_parser.adapters.incident_hibp import CATALOGUE_URL, HibpAdapter

    respx.get("https://haveibeenpwned.com/robots.txt").mock(return_value=NO_ROBOTS)
    catalogue = respx.get(CATALOGUE_URL).mock(
        return_value=httpx.Response(200, json=fixture_json("hibp_breaches.json"))
    )
    company = make_company(
        own_domains=["example.com", "jobs.lever.co"],
        ats=AtsRef(kind="lever", token="x", host="jobs.lever.co"),
    )
    [doc] = await run(HibpAdapter(), company, make_plan("incident"), http)

    assert doc.source_type == "incident" and doc.title == "Example AG data breach"
    assert doc.meta["attribution"] == "Have I Been Pwned (CC BY 4.0)"
    assert doc.meta["pwn_count"] == 1250000 and doc.meta["breach_date"] == "2026-08-30"
    assert "Email addresses, Names, Passwords" in doc.text and "<a" not in doc.text
    assert doc.published_at == datetime(2026, 9, 12, 2, 14, tzinfo=UTC)

    # The catalogue is fetched once per day for the whole process, not per company.
    await run(
        HibpAdapter(),
        make_company(name="Other", domain="other.org", own_domains=[]),
        make_plan("incident"),
        http,
    )
    assert catalogue.call_count == 1


@respx.mock
async def test_hibp_failure_reaches_collect_errors(http: HttpClient) -> None:
    from leadradar_parser.adapters.incident_hibp import CATALOGUE_URL

    respx.get("https://haveibeenpwned.com/robots.txt").mock(return_value=NO_ROBOTS)
    respx.get(CATALOGUE_URL).mock(return_value=httpx.Response(503))
    result = await collect(make_company(), make_plan("incident"), http=http)
    assert [(error.adapter, error.kind) for error in result.errors] == [("hibp", "not_found")]


# --- gleif ---------------------------------------------------------------------------------------------


@respx.mock
async def test_gleif_adapter_uses_known_lei_and_reports_parent(http: HttpClient) -> None:
    from leadradar_parser.adapters.registry_gleif import GleifAdapter

    record = respx.get(f"{GLEIF}/lei-records/529900EXAMPLE0000055").mock(
        return_value=httpx.Response(200, json=fixture_json("gleif_record.json"))
    )
    respx.get(f"{GLEIF}/lei-records/529900EXAMPLE0000055/ultimate-parent").mock(
        return_value=httpx.Response(200, json=fixture_json("gleif_parent.json"))
    )
    company = make_company(firmographics=Firmographics(lei="529900EXAMPLE0000055", source="wikidata"))
    [doc] = await run(GleifAdapter(), company, make_plan("registry"), http)

    assert record.calls[0].request.headers["Accept"] == "application/vnd.api+json"
    assert doc.source_type == "registry" and doc.title.startswith("Example Logistics GmbH")
    structured = doc.meta["structured"]
    assert (structured["lei"], structured["country_code"], structured["hq_city"]) == (
        "529900EXAMPLE0000055",
        "DE",
        "Cologne",
    )
    assert structured["parent_name"] == "Example Aktiengesellschaft"
    assert "Ultimate parent: Example Aktiengesellschaft" in doc.text


@respx.mock
async def test_gleif_search_accepts_only_an_exact_legal_name(http: HttpClient) -> None:
    from leadradar_parser.gleif import name_key, search_lei

    route = respx.get(f"{GLEIF}/lei-records").mock(
        return_value=httpx.Response(200, json=fixture_json("gleif_search.json"))
    )
    ref = CompanyRef(name="Example AG", domain="example.com", country_code="DE")
    record = await search_lei(ref, http, ["Example AG"])
    assert record is not None and record.lei == "529900EXAMPLEPARENT01"
    assert route.calls[0].request.url.params["filter[entity.legalAddress.country]"] == "DE"
    # "Example Systems" / "Example Berlin-Stiftung" are other legal entities: no fuzzy pick.
    assert await search_lei(ref, http, ["Example Group Holding Services"]) is None
    assert name_key("Deutsche Lufthansa Aktiengesellschaft") == name_key("Deutsche Lufthansa AG")


@respx.mock
async def test_resolve_fills_firmographics_from_gleif_when_wikidata_has_none(http: HttpClient) -> None:
    from leadradar_parser.wikidata import API_URL

    respx.get(url__startswith="https://example.com/").mock(return_value=httpx.Response(404))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json={"search": []}))
    respx.get(f"{GLEIF}/lei-records").mock(
        return_value=httpx.Response(200, json=fixture_json("gleif_search.json"))
    )
    resolved = await resolve_company(CompanyRef(name="Example AG", domain="example.com"), http=http)

    firmographics = resolved.firmographics
    assert firmographics is not None and firmographics.source == "gleif"
    assert (firmographics.lei, firmographics.legal_name) == (
        "529900EXAMPLEPARENT01",
        "Example Aktiengesellschaft",
    )
    assert resolved.country_code == "DE" and "GLEIF: 529900EXAMPLEPARENT01" in resolved.notes


@respx.mock
async def test_resolve_adds_lei_to_wikidata_firmographics_without_one(http: HttpClient) -> None:
    from leadradar_parser.wikidata import API_URL, SPARQL_URL

    sparql = fixture_json("sparql_firmographics.json")
    del sparql["results"]["bindings"][0]["lei"]  # type: ignore[index]
    sparql["results"]["bindings"][0]["itemLabel"]["value"] = "Example AG"  # type: ignore[index]
    respx.get(url__startswith="https://example.com/").mock(return_value=httpx.Response(404))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json={"search": [{"id": "Q157645"}]}))
    respx.get(SPARQL_URL).mock(return_value=httpx.Response(200, json=sparql))
    search = respx.get(f"{GLEIF}/lei-records").mock(
        return_value=httpx.Response(200, json=fixture_json("gleif_search.json"))
    )
    resolved = await resolve_company(CompanyRef(name="Example", domain="example.com"), http=http)

    firmographics = resolved.firmographics
    assert firmographics is not None and firmographics.source == "wikidata+gleif"
    assert firmographics.lei == "529900EXAMPLEPARENT01" and firmographics.legal_name == "Example AG"
    assert firmographics.employees == 594879  # Wikidata values are kept
    assert search.calls[0].request.url.params["filter[entity.legalAddress.country]"] == "DE"


@respx.mock
async def test_resolve_skips_gleif_when_wikidata_has_the_lei(http: HttpClient) -> None:
    from leadradar_parser.wikidata import API_URL, SPARQL_URL

    respx.get(url__startswith="https://example.com/").mock(return_value=httpx.Response(404))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json={"search": [{"id": "Q157645"}]}))
    respx.get(SPARQL_URL).mock(
        return_value=httpx.Response(200, json=fixture_json("sparql_firmographics.json"))
    )
    gleif = respx.get(url__startswith=GLEIF)
    resolved = await resolve_company(CompanyRef(name="Example", domain="example.com"), http=http)
    assert not gleif.called
    assert resolved.firmographics is not None and resolved.firmographics.source == "wikidata"


# --- adzuna --------------------------------------------------------------------------------------------


@respx.mock
async def test_adzuna_country_endpoint_company_filter_and_redacted_keys(
    http: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leadradar_parser.adapters.jobs_adzuna import AdzunaAdapter

    monkeypatch.setenv("ADZUNA_APP_ID", "secret-app-id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "secret-app-key")
    route = respx.get("https://api.adzuna.com/v1/api/jobs/de/search/1").mock(
        return_value=httpx.Response(200, json=fixture_json("adzuna_search.json"))
    )
    company = make_company(name="Example AG", country_code="DE")
    plan = make_plan("jobs", job_keywords=["RPA", "process automation"])
    [doc] = await run(AdzunaAdapter(), company, plan, http)

    params = route.calls[0].request.url.params
    assert params["company"] == "Example AG" and params["what_or"] == "RPA process automation"
    assert doc.title == "RPA Developer (m/w/d)" and "<strong>" not in doc.text  # agency posting dropped
    assert doc.meta["employer"] == "Example AG" and doc.meta["truncated"] is True
    assert doc.published_at == datetime(2026, 9, 20, 9, 15, tzinfo=UTC)

    respx.get("https://api.adzuna.com/v1/api/jobs/de/search/1").mock(return_value=httpx.Response(401))
    settings = http.settings.model_copy(update={"adapters": ["adzuna"]})
    async with HttpClient(settings, cache=False) as uncached:
        result = await collect(company, plan, http=uncached)
    [error] = result.errors
    assert error.adapter == "adzuna" and "secret-app-key" not in error.message
    assert "secret-app-id" not in error.message


async def test_adzuna_disabled_without_keys_and_skips_unsupported_countries(
    http: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leadradar_parser.adapters.jobs_adzuna import AdzunaAdapter

    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    settings = http.settings.model_copy(update={"adapters": ["adzuna"]})
    async with HttpClient(settings, cache=False) as client:
        result = await collect(make_company(country_code="DE"), make_plan("jobs"), http=client)
    assert [(error.adapter, error.kind) for error in result.errors] == [("adzuna", "disabled")]

    monkeypatch.setenv("ADZUNA_APP_KEY", "k")
    async with HttpClient(settings, cache=False) as client:
        result = await collect(make_company(country_code="DE"), make_plan("jobs"), http=client)
    assert [(error.kind, error.message) for error in result.errors] == [
        ("disabled", "ADZUNA_APP_ID is not set")
    ]

    monkeypatch.setenv("ADZUNA_APP_ID", "i")
    assert await run(AdzunaAdapter(), make_company(country_code="JP"), make_plan("jobs"), http) == []
    assert http.network_requests == 0


async def test_adzuna_daily_quota_is_enforced(http: HttpClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from time import monotonic

    import leadradar_parser.adapters.jobs_adzuna as adzuna
    from leadradar_parser.errors import SourceRateLimited

    monkeypatch.setenv("ADZUNA_APP_ID", "i")
    monkeypatch.setenv("ADZUNA_APP_KEY", "k")
    adzuna._calls.extend([monotonic() - 3_600] * adzuna.PER_DAY)
    with pytest.raises(SourceRateLimited):
        await run(adzuna.AdzunaAdapter(), make_company(country_code="DE"), make_plan("jobs"), http)
    assert http.network_requests == 0


# --- settings / user agent / keys ----------------------------------------------------------------------


def test_default_user_agent_is_an_identifiable_bot_with_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PARSER_USER_AGENT", raising=False)
    agent = ParserSettings().user_agent
    assert agent.startswith("LeadRadarBot/") and "+https://" in agent and "Mozilla" not in agent


async def test_http_client_sends_the_bot_user_agent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from conftest import make_settings

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200)

    settings = make_settings(tmp_path)
    async with HttpClient(settings, transport=httpx.MockTransport(handler), cache=False) as client:
        await client.get("https://example.com/", check_robots=False)
        await client.download("https://example.com/a.pdf", max_bytes=10, check_robots=False)
    assert seen == [settings.user_agent] * 2


def test_adzuna_keys_are_read_from_env_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from leadradar_parser import list_adapters

    for key in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("ADZUNA_APP_ID=id\nADZUNA_APP_KEY=key\nPARSER_ADAPTERS=adzuna,hibp\n")
    settings = ParserSettings(_env_file=env_file)
    assert (settings.env("ADZUNA_APP_ID"), settings.env("ADZUNA_APP_KEY")) == ("id", "key")
    assert {item.id for item in list_adapters(settings) if item.enabled} == {"adzuna", "hibp"}


# --- rsshub --------------------------------------------------------------------------------------------


@respx.mock
async def test_rsshub_queries_come_from_search_name_and_topics(
    http: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leadradar_parser.adapters.news_rsshub import RSSHubAdapter

    monkeypatch.setenv("RSSHUB_BASE_URL", "https://rsshub.internal/")
    rss = b"""<rss version="2.0"><channel>
      <item><title>Deutsche Post DHL Group startet KI-Programm in der Logistik</title>
      <description>Der Konzern automatisiert Lagerprozesse in ganz Deutschland mit neuen Robotern.</description>
      <link>https://rss.example/de1</link><pubDate>Fri, 25 Sep 2026 09:00:00 GMT</pubDate></item>
      <item><title>Undated item</title><link>https://rss.example/undated</link></item>
    </channel></rss>"""
    route = respx.get(url__startswith="https://rsshub.internal/bing/search/").mock(
        return_value=httpx.Response(200, content=rss)
    )
    company = make_company(name="DHL", aliases=["Deutsche Post DHL Group"])
    plan = make_plan("news", news_topics=["automation", "cost reduction"])
    docs = await run(RSSHubAdapter(), company, plan, http)

    queries = [call.request.url.path.removeprefix("/bing/search/") for call in route.calls]
    assert queries == [
        '"Deutsche Post DHL Group"',
        '"Deutsche Post DHL Group" (automation OR "cost reduction")',
    ]
    german, undated = docs
    assert german.language == "de" and undated.published_at is None  # no "now" fallback dates


async def test_download_stops_a_body_without_content_length_at_the_limit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from conftest import make_settings
    from leadradar_parser.errors import SourceTooLarge

    sent: list[int] = []

    async def endless() -> AsyncIterator[bytes]:
        for _ in range(1_000):
            sent.append(1)
            yield b"x" * 1_024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=endless())

    async with HttpClient(
        make_settings(tmp_path), transport=httpx.MockTransport(handler), cache=False
    ) as client:
        with pytest.raises(SourceTooLarge):
            await client.download("https://example.com/big.pdf", max_bytes=10_000, check_robots=False)
    assert len(sent) < 20  # the stream was abandoned, not read to the end
