from typing import Any
from urllib.parse import urlsplit

from .contracts import CompanyCandidate, CompanyRef, DiscoveryQuery, Firmographics
from .http import HttpClient, shared_lock
from .taxonomy import country_catalog, industry_taxonomy

API_URL = "https://www.wikidata.org/w/api.php"
SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIDATA_TIMEOUT_S = 60.0
EURO_QID = "Q4916"

FIRMOGRAPHICS_QUERY = """
SELECT ?item ?itemLabel
       (SAMPLE(?countryCode0) AS ?countryCode) (SAMPLE(?hqLabel0) AS ?hqLabel)
       (MAX(?employees0) AS ?employees) (MAX(?revenue0) AS ?revenue) (MIN(?founded0) AS ?founded)
       (SAMPLE(?lei0) AS ?lei) (SAMPLE(?cb0) AS ?cbId) (SAMPLE(?ceoLabel0) AS ?ceoLabel)
       (GROUP_CONCAT(DISTINCT STR(?industry0); separator="|") AS ?industries)
WHERE {{
  VALUES ?item {{ wd:{qid} }}
  OPTIONAL {{ ?item wdt:P17 ?country . ?country wdt:P297 ?countryCode0 . }}
  OPTIONAL {{ ?item wdt:P159 ?hq . ?hq rdfs:label ?hqLabel0 . FILTER(LANG(?hqLabel0) = "en") }}
  OPTIONAL {{ ?item wdt:P1128 ?employees0 . }}
  OPTIONAL {{
    ?item p:P2139 ?revenueStatement .
    ?revenueStatement a wikibase:BestRank ; psv:P2139 ?revenueValue .
    ?revenueValue wikibase:quantityAmount ?revenue0 ; wikibase:quantityUnit wd:{euro} .
  }}
  OPTIONAL {{ ?item wdt:P571 ?founded0 . }}
  OPTIONAL {{ ?item wdt:P1278 ?lei0 . }}
  OPTIONAL {{ ?item wdt:P2088 ?cb0 . }}
  OPTIONAL {{
    ?item p:P169 ?ceoStatement . ?ceoStatement ps:P169 ?ceo .
    FILTER NOT EXISTS {{ ?ceoStatement pq:P582 ?ceoEnd . }}
    ?ceo rdfs:label ?ceoLabel0 . FILTER(LANG(?ceoLabel0) = "en")
  }}
  OPTIONAL {{ ?item wdt:P452 ?industry0 . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
GROUP BY ?item ?itemLabel
"""

DISCOVERY_QUERY = """
SELECT ?item ?itemLabel ?website ?cc (MAX(?emp) AS ?employees)
       (SAMPLE(?lei0) AS ?lei) (SAMPLE(?cb0) AS ?cbId)
       (GROUP_CONCAT(DISTINCT STR(?industry); separator="|") AS ?industries)
WHERE {{
  VALUES ?industry {{ {industry_qids} }}
  VALUES ?country {{ {country_qids} }}
  ?item wdt:P452 ?industry ; wdt:P17 ?country ; wdt:P856 ?website .
  ?country wdt:P297 ?cc .
  OPTIONAL {{ ?item wdt:P1128 ?emp . }}
  OPTIONAL {{ ?item wdt:P1278 ?lei0 . }}
  OPTIONAL {{ ?item wdt:P2088 ?cb0 . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
GROUP BY ?item ?itemLabel ?website ?cc
HAVING ({having})
ORDER BY DESC(?employees) LIMIT {limit}
"""


async def resolve_firmographics(company: CompanyRef, http: HttpClient) -> Firmographics | None:
    qid = company.wikidata_qid or await _search_qid(company, http)
    if not qid:
        return None
    bindings = await _sparql(http, FIRMOGRAPHICS_QUERY.format(qid=qid, euro=EURO_QID))
    if not bindings:
        return None
    row = bindings[0]
    industry_qids = {_entity_id(uri) for uri in (_value(row, "industries") or "").split("|") if uri}
    founded = _value(row, "founded")
    return Firmographics(
        legal_name=_value(row, "itemLabel"),
        country_code=_value(row, "countryCode"),
        hq_city=_value(row, "hqLabel"),
        industry_ids=[item.id for item in industry_taxonomy() if industry_qids.intersection(item.wikidata)],
        employees=_int_value(row, "employees"),
        revenue_eur=_int_value(row, "revenue"),
        founded=int(founded[:4]) if founded and founded[:4].isdigit() else None,
        lei=_value(row, "lei"),
        wikidata_qid=qid,
        crunchbase_id=_value(row, "cbId"),
        ceo=_value(row, "ceoLabel"),
        source="wikidata",
    )


async def discover_companies(query: DiscoveryQuery, http: HttpClient) -> list[CompanyCandidate]:
    industries = {item.id: item for item in industry_taxonomy()}
    countries = {item.code: item for item in country_catalog()}
    selected_industries = [industries[item] for item in query.industries if item in industries]
    selected_countries = [countries[item.upper()] for item in query.countries if item.upper() in countries]
    if not selected_industries or not selected_countries:
        return []
    qid_to_industry = {qid: item.id for item in selected_industries for qid in item.wikidata}
    if not qid_to_industry:
        return []
    bindings = await _sparql(
        http,
        DISCOVERY_QUERY.format(
            industry_qids=" ".join(f"wd:{qid}" for qid in sorted(qid_to_industry)),
            country_qids=" ".join(f"wd:{item.wikidata_qid}" for item in selected_countries),
            having=_employees_having(query.employees_min, query.employees_max),
            # One company may list several websites; over-fetch, then dedupe by QID and domain.
            limit=query.limit * 2,
        ),
    )
    excluded = {domain.lower().removeprefix("www.") for domain in query.exclude_domains}
    seen_domains: set[str] = set()
    seen_items: set[str] = set()
    candidates: list[CompanyCandidate] = []
    for row in bindings:
        qid = _entity_id(_value(row, "item")) or ""
        domain = (urlsplit(_value(row, "website") or "").hostname or "").lower().removeprefix("www.")
        if not qid or not domain or domain in excluded or domain in seen_domains or qid in seen_items:
            continue
        seen_domains.add(domain)
        seen_items.add(qid)
        industry_ids = sorted(
            {
                qid_to_industry[industry_qid]
                for uri in (_value(row, "industries") or "").split("|")
                if (industry_qid := _entity_id(uri)) in qid_to_industry
            }
        )
        candidates.append(
            CompanyCandidate(
                name=_value(row, "itemLabel") or domain,
                domain=domain,
                country_code=_value(row, "cc"),
                industry_ids=industry_ids,
                employees=_int_value(row, "employees"),
                revenue_eur=None,
                wikidata_qid=qid,
                lei=_value(row, "lei"),
                crunchbase_id=_value(row, "cbId"),
            )
        )
        if len(candidates) >= query.limit:
            break
    return candidates


def _employees_having(minimum: int | None, maximum: int | None) -> str:
    # Unknown size keeps the candidate (SPEC §1.7.6): COALESCE falls back to the bound itself.
    low = max(minimum or 0, 0)
    clauses = [f"COALESCE(MAX(?emp), {low}) >= {low}"]
    if maximum is not None:
        clauses.append(f"COALESCE(MAX(?emp), {int(maximum)}) <= {int(maximum)}")
    return " && ".join(clauses)


async def _search_qid(company: CompanyRef, http: HttpClient) -> str | None:
    async with shared_lock("wikidata"):
        response = await http.get(
            API_URL,
            check_robots=False,
            timeout=WIKIDATA_TIMEOUT_S,
            params={
                "action": "wbsearchentities",
                "search": company.name,
                "language": "en",
                "type": "item",
                "limit": 5,
                "format": "json",
            },
        )
    qids = [str(item["id"]) for item in response.json().get("search", []) if item.get("id")]
    if len(qids) <= 1:
        return qids[0] if qids else None
    # Homonyms ("Orange", "Continental"): pick the entity whose official website matches the domain.
    bindings = await _sparql(
        http,
        f"SELECT ?item ?website WHERE {{ VALUES ?item {{ {' '.join(f'wd:{qid}' for qid in qids)} }} "
        "?item wdt:P856 ?website . }",
    )
    for row in bindings:
        host = (urlsplit(_value(row, "website") or "").hostname or "").lower().removeprefix("www.")
        if host == company.domain or host.endswith(f".{company.domain}"):
            return _entity_id(_value(row, "item"))
    return qids[0]


async def _sparql(http: HttpClient, query: str) -> list[dict[str, Any]]:
    # Wikidata Query Service: one request at a time, 60 s timeout; 429 honours Retry-After in the HTTP layer.
    async with shared_lock("wikidata"):
        response = await http.get(
            SPARQL_URL,
            check_robots=False,
            timeout=WIKIDATA_TIMEOUT_S,
            params={"query": query, "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
        )
    return list(response.json().get("results", {}).get("bindings", []))


def _value(binding: dict[str, Any], key: str) -> str | None:
    item = binding.get(key)
    if not isinstance(item, dict):
        return None
    value = item.get("value")
    return str(value) if value is not None else None


def _int_value(binding: dict[str, Any], key: str) -> int | None:
    value = _value(binding, key)
    try:
        return int(float(value)) if value is not None else None
    except ValueError:
        return None


def _entity_id(uri: str | None) -> str | None:
    return uri.rsplit("/", 1)[-1] if uri else None
