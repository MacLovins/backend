import re
from datetime import UTC, datetime
from functools import lru_cache
from urllib.parse import urljoin, urlsplit

import httpx
from lxml import etree, html

from .contracts import AtsRef, CompanyRef, ResolvedCompany
from .errors import ParserError
from .http import HttpClient, create_http_client, is_blocked_host
from .normalize import canonicalize_url
from .taxonomy import load_data
from .wikidata import resolve_firmographics

CAREER_MARKERS = ("career", "karriere", "jobs", "vacancies", "stellen", "join-us")
NEWS_MARKERS = ("newsroom", "news", "press", "presse", "media")
CAREER_PATHS = ("/careers", "/karriere", "/jobs", "/en/careers")


@lru_cache
def ats_patterns() -> list[tuple[str, re.Pattern[str]]]:
    data = load_data("ats_patterns.yaml")
    if not isinstance(data, dict):
        raise ValueError("ats_patterns.yaml must contain a mapping")
    return [(str(item["kind"]), re.compile(str(item["pattern"]), re.I)) for item in data.values()]


async def resolve_company(ref: CompanyRef, *, http: HttpClient | None = None) -> ResolvedCompany:
    if http is not None:
        return await _resolve(ref, http)
    async with create_http_client() as owned_http:
        return await _resolve(ref, owned_http)


async def _resolve(ref: CompanyRef, http: HttpClient) -> ResolvedCompany:
    homepage = f"https://{ref.domain}"
    own_domains = {ref.domain}
    careers_url = ref.careers_url
    newsroom_url = ref.newsroom_url
    ats = ref.ats
    notes: list[str] = []
    try:
        response = await http.get(homepage)
        homepage = str(response.url)
        redirected_host = (response.url.host or "").lower().removeprefix("www.")
        if redirected_host:
            own_domains.add(redirected_host)
        links = _links(response.text, homepage)
        careers_url = careers_url or _first_matching_link(links, CAREER_MARKERS, own_domains)
        newsroom_url = newsroom_url or _first_matching_link(links, NEWS_MARKERS, own_domains)
        ats = ats or detect_ats("\n".join([response.text, *links]))
    except (ParserError, httpx.HTTPError, etree.ParserError, ValueError):
        notes.append("homepage unavailable")

    if careers_url is None:
        careers_url = await _probe_career_paths(homepage, http)
    if careers_url is None:
        notes.append("careers not found")
    elif ats is None:
        ats = detect_ats(careers_url)
        if ats is None:
            ats = await _detect_ats_on_page(careers_url, http)

    if ats:
        notes.append(f"ATS: {ats.kind}")
        if ats.host:
            own_domains.add(ats.host.lower().removeprefix("www."))
    elif careers_url:
        notes.append("ATS not detected; careers_html fallback")

    try:
        firmographics = await resolve_firmographics(ref, http)
    except (ParserError, httpx.HTTPError, ValueError):
        firmographics = None
        notes.append("wikidata unavailable")
    if firmographics is None and "wikidata unavailable" not in notes:
        notes.append("wikidata entity not found")
    return ResolvedCompany(
        **ref.model_dump(exclude={"careers_url", "newsroom_url", "ats", "country_code", "wikidata_qid"}),
        country_code=ref.country_code or (firmographics.country_code if firmographics else None),
        wikidata_qid=ref.wikidata_qid or (firmographics.wikidata_qid if firmographics else None),
        careers_url=careers_url,
        newsroom_url=newsroom_url,
        ats=ats,
        homepage_url=homepage,
        own_domains=sorted(own_domains),
        firmographics=firmographics,
        resolved_at=datetime.now(UTC),
        notes=notes,
    )


def detect_ats(content: str) -> AtsRef | None:
    for kind, pattern in ats_patterns():
        match = pattern.search(content)
        if not match:
            continue
        groups = match.groupdict()
        host = groups.get("host")
        return AtsRef(
            kind=kind,  # type: ignore[arg-type]
            token=groups["token"],
            host=host.lower() if host else urlsplit(f"https://{match.group(0)}").hostname,
            site=groups.get("site"),
        )
    return None


async def _detect_ats_on_page(url: str, http: HttpClient) -> AtsRef | None:
    try:
        response = await http.get(url)
    except (ParserError, httpx.HTTPError):
        return None
    candidates = [str(response.url), response.text]
    try:
        tree = html.fromstring(response.text)
        candidates.extend(str(value) for value in tree.xpath("//a/@href | //iframe/@src | //script/@src"))
    except (etree.ParserError, ValueError):
        pass
    return detect_ats("\n".join(candidates))


async def _probe_career_paths(homepage: str, http: HttpClient) -> str | None:
    for path in CAREER_PATHS:
        try:
            response = await http.get(urljoin(homepage, path), attempts=1)
        except (ParserError, httpx.HTTPError):
            continue
        final = canonicalize_url(str(response.url))
        if final != canonicalize_url(homepage):
            return str(response.url)
    return None


def _links(document: str, base: str) -> list[str]:
    return [urljoin(base, str(value)) for value in html.fromstring(document).xpath("//a[@href]/@href")]


def _first_matching_link(links: list[str], markers: tuple[str, ...], own_domains: set[str]) -> str | None:
    """Prefer links on the company's own site; never return block-listed hosts."""
    matching = []
    for link in links:
        parts = urlsplit(link)
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or is_blocked_host(host):
            continue
        if any(marker in f"{host}{parts.path}".lower() for marker in markers):
            matching.append(link)
    own = [link for link in matching if _is_own_host(urlsplit(link).hostname or "", own_domains)]
    return (own or matching or [None])[0]


def _is_own_host(host: str, own_domains: set[str]) -> bool:
    host = host.lower().removeprefix("www.")
    return any(host == domain or host.endswith(f".{domain}") for domain in own_domains)
