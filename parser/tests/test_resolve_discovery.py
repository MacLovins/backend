import httpx
import pytest
import respx
from conftest import fixture_json
from leadradar_parser import CompanyRef, DiscoveryQuery, HttpClient
from leadradar_parser.discovery import discover
from leadradar_parser.resolve import detect_ats, resolve_company
from leadradar_parser.wikidata import API_URL, SPARQL_URL, resolve_firmographics


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("https://boards.greenhouse.io/embed/job_board?for=acme&b=1", ("greenhouse", "acme", None, None)),
        (
            "https://job-boards.greenhouse.io/acme/jobs/1",
            ("greenhouse", "acme", "job-boards.greenhouse.io", None),
        ),
        ("https://jobs.eu.lever.co/acme/123", ("lever", "acme", "jobs.eu.lever.co", None)),
        (
            "https://dhl.wd1.myworkdayjobs.com/en-US/DPDHL_Careers/job/x",
            ("workday", "dhl", "dhl.wd1.myworkdayjobs.com", "DPDHL_Careers"),
        ),
        (
            "https://acme.wd3.myworkdayjobs.com/External",
            ("workday", "acme", "acme.wd3.myworkdayjobs.com", "External"),
        ),
        (
            "https://wd3.myworkdaysite.com/en-US/recruiting/acme/Careers",
            ("workday", "acme", "wd3.myworkdaysite.com", "Careers"),
        ),
        ("https://acme.jobs.personio.de/job/1", ("personio", "acme", "acme.jobs.personio.de", None)),
        ("https://acme.recruitee.com/o/dev", ("recruitee", "acme", "acme.recruitee.com", None)),
    ],
)
def test_ats_detection_patterns(content: str, expected: tuple) -> None:
    ats = detect_ats(content)
    assert ats is not None
    assert (ats.kind, ats.token, ats.host if expected[2] else None, ats.site) == expected


def test_ats_detection_ignores_workday_api_paths() -> None:
    ats = detect_ats("https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External/jobs")
    assert ats is not None and ats.site == "External"


def sparql_router(firmographics: dict, websites: dict | None = None):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if "?item ?website WHERE" in query:
            return httpx.Response(200, json=websites or {"results": {"bindings": []}})
        return httpx.Response(200, json=firmographics)

    return handler


@respx.mock
async def test_wikidata_firmographics_mapping(http: HttpClient) -> None:
    respx.get(API_URL).mock(return_value=httpx.Response(200, json={"search": [{"id": "Q157645"}]}))
    sparql = respx.get(SPARQL_URL).mock(side_effect=sparql_router(fixture_json("sparql_firmographics.json")))
    firmographics = await resolve_firmographics(CompanyRef(name="DHL Group", domain="dhl.com"), http)
    assert firmographics is not None
    assert firmographics.model_dump(exclude={"source"}) == {
        "legal_name": "DHL Group",
        "country_code": "DE",
        "hq_city": "Bonn",
        "industry_ids": ["logistics"],
        "employees": 594879,
        "revenue_eur": 81758000000,
        "founded": 1995,
        "lei": "529900PHEJFM2VNSTN51",
        "wikidata_qid": "Q157645",
        "crunchbase_id": "deutsche-post-dhl",
        "ceo": "Tobias Meyer",
    }
    query = sparql.calls[0].request.url.params["query"]
    assert "wd:Q4916" in query and "GROUP BY ?item ?itemLabel" in query and "pq:P582" in query


@respx.mock
async def test_wikidata_homonym_is_resolved_by_domain(http: HttpClient) -> None:
    respx.get(API_URL).mock(
        return_value=httpx.Response(200, json={"search": [{"id": "Q13191"}, {"id": "Q1431486"}]})
    )
    websites = {
        "results": {
            "bindings": [
                {
                    "item": {"value": "http://www.wikidata.org/entity/Q13191"},
                    "website": {"value": "https://example.org/fruit"},
                },
                {
                    "item": {"value": "http://www.wikidata.org/entity/Q1431486"},
                    "website": {"value": "https://www.orange.com/en"},
                },
            ]
        }
    }
    sparql = respx.get(SPARQL_URL).mock(side_effect=sparql_router({"results": {"bindings": [{}]}}, websites))
    firmographics = await resolve_firmographics(CompanyRef(name="Orange", domain="orange.com"), http)
    assert firmographics is not None and firmographics.wikidata_qid == "Q1431486"
    assert "wd:Q1431486" in sparql.calls[-1].request.url.params["query"]


@respx.mock
async def test_discovery_sparql_filters_and_dedupes(http: HttpClient) -> None:
    def row(qid: str, website: str, employees: str | None) -> dict:
        data = {
            "item": {"value": f"http://www.wikidata.org/entity/{qid}"},
            "itemLabel": {"value": f"Company {qid}"},
            "website": {"value": website},
            "cc": {"value": "DE"},
            "industries": {
                "value": "http://www.wikidata.org/entity/Q177777|http://www.wikidata.org/entity/Q46970"
            },
        }
        if employees:
            data["employees"] = {"value": employees}
        return data

    bindings = [
        row("Q1", "https://www.one.example/", "9000"),
        row("Q1", "https://one-alt.example/", "9000"),  # same company, second website
        row("Q2", "https://two.example", None),
        row("Q3", "https://skip.example", "7000"),
    ]
    sparql = respx.get(SPARQL_URL).mock(
        return_value=httpx.Response(200, json={"results": {"bindings": bindings}})
    )
    query = DiscoveryQuery(
        countries=["de", "AT"],
        industries=["logistics", "airlines"],
        employees_min=5000,
        employees_max=100000,
        exclude_domains=["www.skip.example"],
        limit=10,
    )
    results = await discover(query, http=http)

    assert [(item.domain, item.wikidata_qid, item.employees) for item in results] == [
        ("one.example", "Q1", 9000),
        ("two.example", "Q2", None),
    ]
    assert results[0].industry_ids == ["airlines", "logistics"]
    sent = sparql.calls[0].request.url.params["query"]
    assert "wd:Q183 wd:Q40" in sent and "wd:Q46970" in sent
    assert "COALESCE(MAX(?emp), 5000) >= 5000 && COALESCE(MAX(?emp), 100000) <= 100000" in sent


async def test_discovery_unknown_taxonomy_makes_no_request(http: HttpClient) -> None:
    assert await discover(DiscoveryQuery(countries=["DE"], industries=["unknown"]), http=http) == []
    assert http.network_requests == 0


@respx.mock
async def test_resolve_detects_ats_on_careers_page_and_fills_firmographics(http: HttpClient) -> None:
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/en/careers").mock(
        return_value=httpx.Response(
            200, html="<iframe src='https://boards.greenhouse.io/embed/job_board?for=example'></iframe>"
        )
    )
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(
            200,
            html="""
        <a href='https://www.linkedin.com/company/example/jobs'>Jobs on LinkedIn</a>
        <a href='/en/careers'>Careers</a><a href='/en/newsroom'>Newsroom</a>""",
        )
    )
    respx.get(API_URL).mock(return_value=httpx.Response(200, json={"search": [{"id": "Q157645"}]}))
    respx.get(SPARQL_URL).mock(
        return_value=httpx.Response(200, json=fixture_json("sparql_firmographics.json"))
    )

    resolved = await resolve_company(CompanyRef(name="Example", domain="example.com"), http=http)

    assert resolved.careers_url == "https://example.com/en/careers"
    assert resolved.newsroom_url == "https://example.com/en/newsroom"
    assert resolved.ats is not None and (resolved.ats.kind, resolved.ats.token) == ("greenhouse", "example")
    assert resolved.country_code == "DE" and resolved.wikidata_qid == "Q157645"
    assert "ATS: greenhouse" in resolved.notes
    assert resolved.resolved_at.utcoffset().total_seconds() == 0


@respx.mock
async def test_resolve_survives_unreachable_homepage(http: HttpClient) -> None:
    respx.get(url__startswith="https://example.com/").mock(side_effect=httpx.ConnectError("down"))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json={"search": []}))
    resolved = await resolve_company(CompanyRef(name="Example", domain="example.com"), http=http)
    assert resolved.firmographics is None and resolved.ats is None
    assert {"homepage unavailable", "careers not found", "wikidata entity not found"} <= set(resolved.notes)
