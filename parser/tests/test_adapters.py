import gzip
import json

import httpx
import pytest
import respx
from conftest import fixture_bytes, fixture_json, fixture_text, make_company, make_plan
from leadradar_parser import HttpClient, collect
from leadradar_parser.adapters.jobs_ats import JobsAtsAdapter
from leadradar_parser.adapters.jobs_careers_html import CareersHtmlAdapter
from leadradar_parser.adapters.news_gdelt import GDELT_URL, GdeltAdapter
from leadradar_parser.adapters.web_site import WebsiteAdapter, page_kind
from leadradar_parser.errors import SourceRateLimited

NO_ROBOTS = httpx.Response(404)


async def run(adapter, company, plan, http: HttpClient):  # type: ignore[no-untyped-def]
    return [item async for item in adapter.fetch(company, plan, http)]


# --- gdelt ---------------------------------------------------------------------------------------------


@respx.mock
async def test_gdelt_queries_extracts_text_and_falls_back_to_headline(http: HttpClient) -> None:
    api = respx.get(GDELT_URL).mock(return_value=httpx.Response(200, json=fixture_json("gdelt_artlist.json")))
    respx.get("https://news.example/robots.txt").mock(return_value=NO_ROBOTS)
    respx.get("https://paywall.example/robots.txt").mock(return_value=NO_ROBOTS)
    body = " ".join(["DHL Group automatisiert seine Lager in ganz Europa mit Robotern und KI."] * 8)
    respx.get("https://news.example/dhl-automation").mock(
        return_value=httpx.Response(200, html=f"<html><body><article><p>{body}</p></article></body></html>")
    )
    respx.get("https://paywall.example/dhl-cfo").mock(return_value=httpx.Response(403))
    plan = make_plan("news", languages=["en", "de"], news_topics=["automation", "cost reduction"])

    documents = await run(GdeltAdapter(), make_company(name="DHL Group", domain="dhl.com"), plan, http)

    queries = [(call.request.url.params["query"], call.request.url.params["timespan"]) for call in api.calls]
    assert queries == [
        ('"DHL Group" (sourcelang:english OR sourcelang:german)', "30d"),
        ('"DHL Group" (automation OR "cost reduction")', "3months"),
    ]
    assert [(item.title, item.meta["headline_only"], item.language) for item in documents] == [
        ("DHL Group expands warehouse automation", False, "de"),
        ("DHL Group names new CFO", True, "en"),
    ]
    assert documents[0].canonical_url == "https://news.example/dhl-automation"
    assert (
        documents[0].published_at is not None and documents[0].published_at.utcoffset().total_seconds() == 0
    )


def test_gdelt_query_single_language_without_parentheses_and_short_name_alias() -> None:
    company = make_company(name="DHL", aliases=["Deutsche Post DHL Group"])
    queries = GdeltAdapter._queries(company, make_plan("news", languages=["en"]))
    assert queries == [('"Deutsche Post DHL Group" sourcelang:english', "30d")]


@respx.mock
async def test_gdelt_429_opens_circuit_breaker(http: HttpClient) -> None:
    api = respx.get(GDELT_URL).mock(return_value=httpx.Response(429))
    company = make_company(name="DHL Group")
    with pytest.raises(SourceRateLimited) as first:
        await run(GdeltAdapter(), company, make_plan("news"), http)
    assert first.value.retry_after_s == 60
    assert api.call_count == 1  # no hammering: a single attempt, then the breaker opens

    with pytest.raises(SourceRateLimited, match="circuit breaker"):
        await run(GdeltAdapter(), company, make_plan("news"), http)
    assert api.call_count == 1

    result = await collect(company, make_plan("news"), http=http)
    assert [(error.adapter, error.kind) for error in result.errors] == [("gdelt", "rate_limited")]
    assert api.call_count == 1


@respx.mock
async def test_gdelt_breaker_escalates_after_repeated_429(http: HttpClient) -> None:
    import leadradar_parser.adapters.news_gdelt as gdelt

    respx.get(GDELT_URL).mock(return_value=httpx.Response(429))
    for expected in (60, 120, 900):
        gdelt._blocked_until = 0.0  # pause elapsed, half-open
        with pytest.raises(SourceRateLimited) as raised:
            await run(GdeltAdapter(), make_company(), make_plan("news"), http)
        assert raised.value.retry_after_s == expected


# --- website -------------------------------------------------------------------------------------------


@respx.mock
async def test_website_sitemap_index_from_robots_newsroom_depth1_and_dedup(http: HttpClient) -> None:
    routes = {
        "/robots.txt": httpx.Response(200, text=fixture_text("site_robots.txt")),
        "/sitemaps/index.xml.gz": httpx.Response(
            200,
            content=gzip.compress(fixture_bytes("site_sitemap_index.xml")),
            headers={"Content-Type": "application/x-gzip"},
        ),
        "/sitemaps/pages.xml": httpx.Response(200, content=fixture_bytes("site_sitemap_pages.xml")),
        "/sitemaps/news.xml": httpx.Response(200, content=fixture_bytes("site_sitemap_news.xml")),
        "/en/news/2026/automation-programme": httpx.Response(200, html=fixture_text("site_article_new.html")),
        "/en/news/2024/old-news": httpx.Response(200, html=fixture_text("site_article_old.html")),
        "/en/news": httpx.Response(200, html=fixture_text("site_newsroom.html")),
        "/en/strategy-2030": httpx.Response(200, html=fixture_text("site_strategy.html")),
        "/en/about-us": httpx.Response(200, html=fixture_text("site_about.html")),
        "/files/annual-report.pdf": httpx.Response(200, content=b"%PDF"),
        "/internal/draft": httpx.Response(200, html="secret"),
        "/sitemap.xml": httpx.Response(200, text="<urlset/>"),
        "/": httpx.Response(200, html=fixture_text("site_home.html")),
    }
    mocked = {
        path: respx.get(f"https://example.com{path}").mock(return_value=resp) for path, resp in routes.items()
    }

    documents = await run(WebsiteAdapter(), make_company(), make_plan("website"), http)

    by_url = {item.canonical_url: item for item in documents}
    article = by_url["https://example.com/en/news/2026/automation-programme"]
    assert article.meta == {"page_kind": "news", "depth": 1}
    assert article.published_at is not None and article.published_at.isoformat().startswith("2026-09-20")
    assert by_url["https://example.com/en/strategy-2030"].meta["page_kind"] == "strategy"
    assert "https://example.com/en/news/2024/old-news" not in by_url  # outside the window
    assert "https://example.com/en/about-us" not in by_url  # < 300 characters
    assert len(by_url) == len(documents)  # canonical duplicates (utm, trailing slash) collapsed
    assert mocked["/sitemap.xml"].call_count == 0  # robots Sitemap: wins over the default path
    assert mocked["/internal/draft"].call_count == 0  # robots-disallowed page never requested
    assert mocked["/files/annual-report.pdf"].call_count == 0
    assert mocked["/en/news/2026/automation-programme"].call_count == 1


@respx.mock
async def test_website_falls_back_to_default_sitemaps(http: HttpClient) -> None:
    respx.get("https://example.com/robots.txt").mock(return_value=NO_ROBOTS)
    default = respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(404))
    index = respx.get("https://example.com/sitemap_index.xml").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/").mock(return_value=httpx.Response(200, html="<p>short</p>"))
    assert await run(WebsiteAdapter(), make_company(), make_plan("website"), http) == []
    assert default.call_count == index.call_count == 1


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://x.com/de/presse/2026/a", "news"),
        ("https://x.com/investors/ir", "ir"),
        ("https://x.com/en/directions", "other"),
        ("https://x.com/unternehmen/ueber-uns", "about"),
        ("https://x.com/karriere", "careers"),
    ],
)
def test_website_page_kind_uses_segments(url: str, kind: str) -> None:
    assert page_kind(url) == kind


# --- ATS -----------------------------------------------------------------------------------------------


@respx.mock
async def test_ats_greenhouse_unescapes_html(http: HttpClient) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/example/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    company = make_company(ats={"kind": "greenhouse", "token": "example"})
    [job] = await run(JobsAtsAdapter(), company, make_plan("jobs"), http)
    assert job.source_name == "greenhouse" and job.meta["location"] == "Berlin"
    assert "public data pipelines" in job.text and "UiPath" in job.text and "<p>" not in job.text
    assert job.published_at is not None and job.published_at.isoformat() == "2026-09-19T14:00:00+00:00"


@respx.mock
async def test_ats_lever_eu(http: HttpClient) -> None:
    route = respx.get("https://api.eu.lever.co/v0/postings/example").mock(
        return_value=httpx.Response(200, json=fixture_json("lever_postings.json"))
    )
    company = make_company(ats={"kind": "lever", "token": "example", "host": "jobs.eu.lever.co"})
    [job] = await run(JobsAtsAdapter(), company, make_plan("jobs"), http)
    assert route.calls[0].request.url.params["mode"] == "json"
    assert job.source_name == "lever" and job.url == "https://jobs.eu.lever.co/example/abc"
    assert job.meta == {
        "location": "Vienna",
        "department": "Finance Operations",
        "employment_type": "Full-time",
        "ats": "lever",
    }
    assert job.published_at is not None and job.published_at.year == 2025


@respx.mock
async def test_ats_workday_searches_keywords_and_dedupes_external_path(http: HttpClient) -> None:
    base = "https://dhl.wd1.myworkdayjobs.com/wday/cxs/dhl/DPDHL"

    def search(request: httpx.Request) -> httpx.Response:
        keyword = json.loads(request.content)["searchText"]
        return httpx.Response(200, json=fixture_json(f"workday_jobs_{keyword}.json"))

    searches = respx.post(f"{base}/jobs").mock(side_effect=search)
    respx.get(f"{base}/job/Bonn/Automation-Engineer_R1").mock(
        return_value=httpx.Response(200, json=fixture_json("workday_job_r1.json"))
    )
    respx.get(f"{base}/job/Bonn/Process-Mining-Lead_R2").mock(
        return_value=httpx.Response(200, json=fixture_json("workday_job_r2.json"))
    )
    company = make_company(
        ats={"kind": "workday", "token": "dhl", "host": "dhl.wd1.myworkdayjobs.com", "site": "DPDHL"}
    )
    plan = make_plan("jobs", job_keywords=["automation", "security", "automation"])
    jobs = await run(JobsAtsAdapter(), company, plan, http)

    assert searches.call_count == 2
    assert [job.title for job in jobs] == ["Automation Engineer", "Process Mining Lead"]
    assert "RPA" in jobs[0].text and "<b>" not in jobs[0].text
    assert jobs[1].url == "https://dhl.wd1.myworkdayjobs.com/DPDHL/job/Bonn/Process-Mining-Lead_R2"
    assert jobs[0].meta["search"] == "automation" and jobs[0].meta["location"] == "Bonn"


@respx.mock
async def test_ats_personio_xml(http: HttpClient) -> None:
    respx.get("https://example.jobs.personio.de/xml").mock(
        return_value=httpx.Response(200, content=fixture_bytes("personio.xml"))
    )
    company = make_company(ats={"kind": "personio", "token": "example", "host": "example.jobs.personio.de"})
    [job] = await run(JobsAtsAdapter(), company, make_plan("jobs"), http)
    assert job.url == "https://example.jobs.personio.de/job/42"
    assert "SIEM" in job.text and "ISO 27001" in job.text and "<strong>" not in job.text
    assert job.published_at is not None and job.published_at.isoformat() == "2026-09-15T07:30:00+00:00"
    assert job.meta["department"] == "IT" and job.meta["location"] == "Munich"


# --- careers_html --------------------------------------------------------------------------------------


@respx.mock
async def test_careers_html_headline_only_skips_blocked_and_dupes(http: HttpClient) -> None:
    respx.get("https://example.com/robots.txt").mock(return_value=NO_ROBOTS)
    respx.get("https://example.com/careers").mock(
        return_value=httpx.Response(200, html=fixture_text("careers.html"))
    )
    company = make_company(careers_url="https://example.com/careers")
    jobs = await run(CareersHtmlAdapter(), company, make_plan("jobs"), http)
    assert [job.title for job in jobs] == ["RPA Developer (m/f/d)", "SOC Analyst"]
    assert all(job.meta["headline_only"] for job in jobs)
    assert not any("linkedin" in job.url for job in jobs)


@respx.mock
async def test_careers_html_is_skipped_when_supported_ats_known(http: HttpClient) -> None:
    page = respx.get("https://example.com/careers").mock(return_value=httpx.Response(200, html="<a/>"))
    company = make_company(careers_url="https://example.com/careers", ats={"kind": "lever", "token": "x"})
    assert await run(CareersHtmlAdapter(), company, make_plan("jobs"), http) == []
    assert page.call_count == 0
