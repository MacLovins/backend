from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from .contracts import CompanyCandidate, CompanyRef, DiscoveryQuery, Firmographics
from .errors import ParserError, SourceTimeout
from .http import HttpClient, shared_lock
from .taxonomy import country_catalog, industry_taxonomy

API_URL = "https://www.wikidata.org/w/api.php"
SPARQL_URL = "https://query.wikidata.org/sparql"
# QLever mirror of Wikidata (Uni Freiburg): same data, answers the discovery lookups in < 1 s while WDQS
# regularly times out on them. Discovery uses it first and falls back to WDQS.
QLEVER_URL = "https://qlever.dev/api/wikidata"
QLEVER_TIMEOUT_S = 20.0
SPARQL_PREFIXES = (
    "PREFIX wd: <http://www.wikidata.org/entity/>\n"
    "PREFIX wdt: <http://www.wikidata.org/prop/direct/>\n"
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
)
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
    # Current CEO: no end date, most recent start date (old statements often lack P582).
    SELECT ?ceoLabel0 WHERE {{
      wd:{qid} p:P169 ?ceoStatement . ?ceoStatement ps:P169 ?ceo .
      FILTER NOT EXISTS {{ ?ceoStatement pq:P582 ?ceoEnd . }}
      OPTIONAL {{ ?ceoStatement pq:P580 ?ceoStart . }}
      ?ceo rdfs:label ?ceoLabel0 . FILTER(LANG(?ceoLabel0) = "en")
    }} ORDER BY DESC(?ceoStart) LIMIT 1
  }}
  OPTIONAL {{ ?item wdt:P452 ?industry0 . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
GROUP BY ?item ?itemLabel
"""

# One light query per industry QID: WDQS times out on a single query over all QIDs (measured 504 / 60 s+).
DISCOVERY_QUERY = (
    SPARQL_PREFIXES
    + """
SELECT ?item ?itemLabel ?website ?cc (MAX(?emp) AS ?employees) (SAMPLE(?lei0) AS ?lei) (SAMPLE(?cb0) AS ?cbId)
WHERE {{
  VALUES ?country {{ {country_qids} }}
  ?item wdt:{prop} wd:{qid} ; wdt:P17 ?country ; wdt:P856 ?website .
  ?country wdt:P297 ?cc .
  OPTIONAL {{ ?item wdt:P1128 ?emp . }}
  OPTIONAL {{ ?item wdt:P1278 ?lei0 . }}
  OPTIONAL {{ ?item wdt:P2088 ?cb0 . }}
  OPTIONAL {{ ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel) = "en") }}
}}
GROUP BY ?item ?itemLabel ?website ?cc
"""
)
DISCOVERY_BUDGET_S = 90.0


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


async def discover_companies(
    query: DiscoveryQuery, http: HttpClient, *, time_budget_s: float = DISCOVERY_BUDGET_S
) -> list[CompanyCandidate]:
    industries = {item.id: item for item in industry_taxonomy()}
    countries = {item.code: item for item in country_catalog()}
    selected_industries = [industries[item] for item in query.industries if item in industries]
    selected_countries = [countries[item.upper()] for item in query.countries if item.upper() in countries]
    lookups = [
        (prop, qid, industry.id)
        for industry in selected_industries
        for prop, qids in (("P452", industry.wikidata), ("P31", industry.wikidata_classes))
        for qid in qids
    ]
    if not lookups or not selected_countries:
        return []
    country_qids = " ".join(f"wd:{item.wikidata_qid}" for item in selected_countries)

    merged: dict[str, dict[str, Any]] = {}
    failures: list[Exception] = []
    succeeded = 0
    deadline = monotonic() + time_budget_s
    for prop, qid, industry_id in lookups:
        remaining = deadline - monotonic()
        if remaining < 1:
            failures.append(SourceTimeout(f"Discovery time budget of {time_budget_s:.0f}s exhausted"))
            break
        sparql = DISCOVERY_QUERY.format(country_qids=country_qids, prop=prop, qid=qid)
        try:
            bindings = await _sparql(
                http, sparql, endpoint=QLEVER_URL, timeout_s=min(QLEVER_TIMEOUT_S, remaining)
            )
        except ParserError:
            try:
                remaining = deadline - monotonic()
                if remaining < 1:
                    raise SourceTimeout(f"Discovery time budget of {time_budget_s:.0f}s exhausted") from None
                bindings = await _sparql(http, sparql, timeout_s=min(WIKIDATA_TIMEOUT_S, remaining))
            except ParserError as exc:  # one slow industry must not lose the others
                failures.append(exc)
                continue
        succeeded += 1
        for row in bindings:
            item_qid = _entity_id(_value(row, "item"))
            domain = (urlsplit(_value(row, "website") or "").hostname or "").lower().removeprefix("www.")
            if not item_qid or not domain:
                continue
            entry = merged.setdefault(
                item_qid,
                {"name": _value(row, "itemLabel"), "domain": domain, "cc": _value(row, "cc"), "employees": None,
                 "lei": _value(row, "lei"), "cb": _value(row, "cbId"), "industries": set()},
            )  # fmt: skip
            employees = _int_value(row, "employees")
            if employees is not None and (entry["employees"] is None or employees > entry["employees"]):
                entry["employees"] = employees
            entry["industries"].add(industry_id)
    if not succeeded and failures:
        raise failures[0]

    excluded = {domain.lower().removeprefix("www.") for domain in query.exclude_domains}
    minimum, maximum = query.employees_min, query.employees_max
    seen_domains: set[str] = set()
    candidates: list[CompanyCandidate] = []
    # Largest first; unknown size last but kept (SPEC §1.7.6: a data gap, not a disqualifier).
    ordered = sorted(merged.items(), key=lambda pair: -(pair[1]["employees"] or -1))
    for item_qid, entry in ordered:
        employees = entry["employees"]
        if employees is not None and (
            (minimum is not None and employees < minimum) or (maximum is not None and employees > maximum)
        ):
            continue
        if entry["domain"] in excluded or entry["domain"] in seen_domains:
            continue
        seen_domains.add(entry["domain"])
        candidates.append(
            CompanyCandidate(
                name=entry["name"] or entry["domain"],
                domain=entry["domain"],
                country_code=entry["cc"],
                industry_ids=sorted(entry["industries"]),
                employees=employees,
                revenue_eur=None,
                wikidata_qid=item_qid,
                lei=entry["lei"],
                crunchbase_id=entry["cb"],
            )
        )
        if len(candidates) >= query.limit:
            break
    return candidates


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


async def _sparql(
    http: HttpClient, query: str, *, endpoint: str = SPARQL_URL, timeout_s: float = WIKIDATA_TIMEOUT_S
) -> list[dict[str, Any]]:
    # One request at a time per endpoint, 60 s timeout; 429 honours Retry-After in the HTTP layer.
    async with shared_lock(f"sparql:{endpoint}"):
        response = await http.get(
            endpoint,
            check_robots=False,
            timeout=timeout_s,
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
