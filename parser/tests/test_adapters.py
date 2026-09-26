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

    async with HttpClient(http.settings.model_copy(update={"adapters": ["gdelt"]})) as gdelt_only:
        result = await collect(company, make_plan("news"), http=gdelt_only)
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


# --- google_news, newsapi, serpapi, rsshub, crunchbase, playwright -------------------------------------

GOOGLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Google News</title>
  <item>
    <title>DHL deploys AI robotics across supply chain - TechCrunch</title>
    <link>https://news.google.com/rss/articles/12345</link>
    <pubDate>Thu, 24 Sep 2026 12:00:00 GMT</pubDate>
    <description>&lt;a href="https://techcrunch.com/x"&gt;DHL deploys AI robotics&lt;/a&gt; major automation expansion</description>
    <source url="https://techcrunch.com">TechCrunch</source>
  </item>
  <item>
    <title>DHL deploys AI robotics across supply chain - Reuters</title>
    <link>https://news.google.com/rss/articles/67890</link>
    <pubDate>Thu, 24 Sep 2026 13:00:00 GMT</pubDate>
    <source url="https://reuters.com">Reuters</source>
  </item>
  <item>
    <title>Old DHL story - Example</title>
    <link>https://news.google.com/rss/articles/old</link>
    <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
    <source url="https://example.org">Example</source>
  </item>
  <item>
    <title>Undated DHL story - Example</title>
    <link>https://news.google.com/rss/articles/undated</link>
    <source url="https://example.org">Example</source>
  </item>
</channel></rss>"""


@respx.mock
async def test_google_news_rss_parsing_dates_and_editions(http: HttpClient) -> None:
    from leadradar_parser.adapters.news_google import GoogleNewsAdapter

    feed = respx.get(url__regex=r"^https://news\.google\.com/rss/search.*").mock(
        return_value=httpx.Response(200, content=GOOGLE_RSS)
    )
    company = make_company(name="DHL", aliases=["DHL Group"])
    plan = make_plan("news", languages=["de", "en"], news_topics=["automation", "cost reduction"])
    docs = await run(GoogleNewsAdapter(), company, plan, http)

    assert [doc.title for doc in docs] == ["DHL deploys AI robotics across supply chain", "Undated DHL story"]
    first, undated = docs
    assert first.meta["publisher"] == "TechCrunch" and first.meta["headline_only"] is True
    assert "<a" not in first.text and "automation expansion" in first.text
    assert first.published_at is not None and first.published_at.isoformat() == "2026-09-24T12:00:00+00:00"
    assert undated.published_at is None  # unknown date stays None, never "now"
    params = [call.request.url.params for call in feed.calls]
    assert {p["ceid"] for p in params} == {"DE:de", "US:en"}
    assert {p["q"] for p in params} == {'"DHL Group"', '"DHL Group" (automation OR "cost reduction")'}


@respx.mock
async def test_google_news_total_failure_reaches_collect_errors(http: HttpClient) -> None:
    respx.get(url__regex=r"^https://news\.google\.com/rss/search.*").mock(return_value=httpx.Response(503))
    result = await collect(make_company(name="DHL Group"), make_plan("news"), http=http)
    assert ("google_news", "not_found") in {(error.adapter, error.kind) for error in result.errors}


@respx.mock
async def test_newsapi_key_in_header_not_url(http: HttpClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from leadradar_parser.adapters.news_newsapi import NewsApiAdapter

    monkeypatch.setenv("NEWSAPI_KEY", "secret-newsapi-key")
    payload = {
        "status": "ok",
        "articles": [
            {
                "source": {"id": "reuters", "name": "Reuters"},
                "title": "DHL expands autonomous warehouse operations",
                "description": "Logistics giant invests 500M in AI.",
                "content": "DHL Group said on Friday it will invest 500M in AI… [+2310 chars]",
                "url": "https://reuters.com/dhl-ai",
                "publishedAt": "2026-09-25T10:00:00Z",
            },
            {"title": "[Removed]", "url": "https://removed.example"},
        ],
    }
    route = respx.get(url__regex=r"^https://newsapi\.org/v2/everything.*").mock(
        return_value=httpx.Response(200, json=payload)
    )
    [doc] = await run(NewsApiAdapter(), make_company(name="DHL Group"), make_plan("news"), http)

    request = route.calls[0].request
    assert "secret-newsapi-key" not in str(request.url)
    assert request.headers["X-Api-Key"] == "secret-newsapi-key"
    assert doc.meta["publisher"] == "Reuters" and "[+2310 chars]" not in doc.text
    assert doc.published_at is not None and doc.published_at.isoformat() == "2026-09-25T10:00:00+00:00"


@respx.mock
async def test_serpapi_dates_and_key_redacted_in_errors(
    http: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leadradar_parser.adapters.news_serpapi import SerpApiAdapter

    monkeypatch.setenv("SERPAPI_KEY", "secret-serp-key")
    payload = {
        "news_results": [
            {
                "title": "DHL integrates cybersecurity mesh architecture",
                "link": "https://cybernews.com/dhl-mesh",
                "snippet": "New security posture covers global endpoints.",
                "source": {"name": "CyberNews"},
                "iso_date": "2026-09-22T08:00:00Z",
            },
            {"title": "Undated result", "link": "https://example.org/undated"},
        ]
    }
    respx.get(url__regex=r"^https://serpapi\.com/search\.json.*").mock(
        side_effect=[httpx.Response(200, json=payload), httpx.Response(200, json={})]
    )
    docs = await run(SerpApiAdapter(), make_company(name="DHL Group"), make_plan("news"), http)
    assert [doc.title for doc in docs] == ["DHL integrates cybersecurity mesh architecture", "Undated result"]
    assert docs[0].published_at is not None and docs[1].published_at is None

    respx.get(url__regex=r"^https://serpapi\.com/search\.json.*").mock(return_value=httpx.Response(401))
    settings = make_settings_for(http, adapters=["serpapi"])
    async with HttpClient(settings, cache=False) as uncached:
        result = await collect(make_company(name="DHL Group"), make_plan("news"), http=uncached)
    [error] = result.errors
    assert error.kind == "not_found" and "secret-serp-key" not in error.message


def make_settings_for(http: HttpClient, **updates: object):  # type: ignore[no-untyped-def]
    return http.settings.model_copy(update=updates)


@respx.mock
async def test_rsshub_requires_self_hosted_instance(
    http: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leadradar_parser.adapters.news_rsshub import RSSHubAdapter

    monkeypatch.delenv("RSSHUB_BASE_URL", raising=False)
    assert await run(RSSHubAdapter(), make_company(), make_plan("news"), http) == []

    monkeypatch.setenv("RSSHUB_BASE_URL", "https://rsshub.internal/")
    rss = b"""<rss version="2.0"><channel><item><title>DHL signs cloud contract</title>
      <description>Major cloud migration.</description><link>https://rss.example/item1</link>
      <pubDate>Fri, 25 Sep 2026 09:00:00 GMT</pubDate></item></channel></rss>"""
    respx.get(url__regex=r"^https://rsshub\.internal/bing/search/.*").mock(
        return_value=httpx.Response(200, content=rss)
    )
    docs = await run(RSSHubAdapter(), make_company(name="DHL Group"), make_plan("news"), http)
    assert [doc.title for doc in docs] == ["DHL signs cloud contract"]


async def test_feed_parser_rejects_entity_expansion() -> None:
    from leadradar_parser.adapters.common import parse_feed

    bomb = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>
      <rss><channel><item><title>&b;</title></item></channel></rss>"""
    [item] = parse_feed(bomb)
    assert "aaaaaaaaaa" not in item.title


@respx.mock
async def test_crunchbase_uses_header_key_and_identifier_lists(
    http: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leadradar_parser.adapters.registry_crunchbase import CrunchbaseAdapter

    monkeypatch.setenv("CRUNCHBASE_API_KEY", "secret-cb-key")
    respx.get("https://api.crunchbase.com/api/v4/autocompletes").mock(
        return_value=httpx.Response(200, json={"entities": [{"identifier": {"permalink": "dhl"}}]})
    )
    entity = respx.get("https://api.crunchbase.com/api/v4/entities/organizations/dhl").mock(
        return_value=httpx.Response(
            200,
            json={
                "properties": {
                    "identifier": {"value": "DHL Group"},
                    "short_description": "Global logistics.",
                    "categories": [{"value": "Logistics"}, {"value": "Shipping"}],
                    "num_employees_enum": "c_10001_max",
                    "location_identifiers": [{"value": "Bonn"}],
                }
            },
        )
    )
    [doc] = await run(CrunchbaseAdapter(), make_company(name="DHL Group"), make_plan("registry"), http)
    assert "Categories: Logistics, Shipping" in doc.text and "Headquarters: Bonn" in doc.text
    request = entity.calls[0].request
    assert request.headers["X-cb-user-key"] == "secret-cb-key" and "secret-cb-key" not in str(request.url)


async def test_playwright_adapter_without_package_does_nothing(
    http: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    from leadradar_parser.adapters.web_playwright import PlaywrightAdapter

    real_import = builtins.__import__

    def no_playwright(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name.startswith("playwright"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_playwright)
    assert await run(PlaywrightAdapter(), make_company(), make_plan("website"), http) == []
    assert http.network_requests == 0


def test_website_keeps_one_copy_per_locale_preferring_primary_domain_and_english() -> None:
    from leadradar_parser.adapters.web_site import _one_per_locale

    urls = {
        "https://mydhl.express.dhl/al/en/about": None,
        "https://www.dhl.com/de-de/about": None,
        "https://www.dhl.com/global-en/about": None,
        "https://www.dhl.com/fr-en/about": None,
        "https://www.dhl.com/global-en/strategy": None,
    }
    assert _one_per_locale(urls, "dhl.com") == [
        "https://www.dhl.com/global-en/about",
        "https://www.dhl.com/global-en/strategy",
    ]
