import re
import zlib
from collections import deque
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from functools import lru_cache
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..errors import ParserError
from ..http import HttpClient
from ..normalize import canonicalize_url, utc_datetime
from ..taxonomy import load_data
from .common import extract_page, is_html, make_document

MAX_SITEMAPS = 5
MAX_SITEMAP_URLS = 5_000
MAX_SITEMAP_BYTES = 50_000_000
MAX_ARTICLES = 15
MIN_PAGE_CHARS = 300
LOCALE_SEGMENT = re.compile(r"^(?:[a-z]{2}|global)(?:[-_][a-z]{2})?$")
KIND_ORDER = ("news", "strategy", "ir", "about", "careers", "other")
ASSET_SUFFIXES = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".json", ".xml",
    ".zip", ".gz", ".mp3", ".mp4", ".mov", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)  # fmt: skip


@lru_cache
def page_patterns() -> dict[str, tuple[str, ...]]:
    data = load_data("url_patterns.yaml")
    if not isinstance(data, dict):
        raise ValueError("url_patterns.yaml must contain a mapping")
    return {str(kind): tuple(str(token).lower() for token in tokens) for kind, tokens in data.items()}


def page_kind(url: str) -> str:
    segments = _segments(url)
    for kind, tokens in page_patterns().items():
        for token in tokens:
            if len(token) <= 3 and token in segments:
                return kind
            if len(token) > 3 and any(token in segment for segment in segments):
                return kind
    return "other"


class WebsiteAdapter:
    id = "website"
    source_type = "website"
    requires_env = None
    rate_limit = RateLimit(requests=1, per_seconds=1, scope="host")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        roots = _site_roots(company)
        candidates = await self._candidate_urls(company, http, roots)
        newsroom = canonicalize_url(company.newsroom_url) if company.newsroom_url else None
        queue = deque(_select(candidates, plan, newsroom, company.domain))
        queued = set(queue)
        articles: deque[str] = deque()
        article_count = 0
        max_documents = min(plan.max_website_pages, plan.max_items_per_source)
        max_fetches = plan.max_website_pages * 2
        emitted = fetches = 0
        while (articles or queue) and emitted < max_documents and fetches < max_fetches:
            is_article = bool(articles)
            url = articles.popleft() if is_article else queue.popleft()
            lastmod = candidates.get(url)
            if is_article and lastmod is not None and lastmod < plan.since:
                continue
            fetches += 1
            try:
                response = await http.get(url)
            except ParserError:
                continue
            final_url = str(response.url)
            if not is_html(response) or not _is_own(final_url, roots):
                continue
            kind = "news" if is_article else page_kind(url)
            if not is_article and kind == "news" and _is_listing(url, newsroom):
                for link in _article_links(response.text, final_url, url, roots):
                    if article_count >= MAX_ARTICLES:
                        break
                    if link not in queued:
                        queued.add(link)
                        articles.append(link)
                        article_count += 1
            page = await extract_page(response.text, final_url)
            if page is None or len(page.text) < MIN_PAGE_CHARS:
                continue
            published_at = utc_datetime(page.date) or lastmod
            if is_article and published_at is not None and published_at < plan.since:
                continue
            yield make_document(
                source_type="website",
                source_name=self.id,
                url=final_url,
                title=page.title or _html_title(response.text),
                text=page.text,
                published_at=published_at,
                meta={"page_kind": kind, "depth": int(is_article)},
            )
            emitted += 1

    async def _candidate_urls(
        self, company: ResolvedCompany, http: HttpClient, roots: set[str]
    ) -> dict[str, datetime | None]:
        homepage = company.homepage_url
        candidates: dict[str, datetime | None] = {canonicalize_url(homepage): None}
        if company.newsroom_url and _is_own(company.newsroom_url, roots):
            candidates[canonicalize_url(company.newsroom_url)] = None

        # The company domain first: homepages often geo-redirect to a regional or product site.
        sitemaps: list[str] = []
        for origin in dict.fromkeys((f"https://{company.domain}/", homepage)):
            try:
                sitemaps += [url for url in await http.robots_sitemaps(origin) if _is_own(url, roots)]
            except ParserError:
                continue
        sitemaps = list(dict.fromkeys(sitemaps))
        if not sitemaps:
            sitemaps = [urljoin(homepage, "/sitemap.xml"), urljoin(homepage, "/sitemap_index.xml")]

        visited: set[str] = set()
        while sitemaps and len(visited) < MAX_SITEMAPS and len(candidates) < MAX_SITEMAP_URLS:
            sitemap_url = sitemaps.pop(0)
            if sitemap_url in visited:
                continue
            visited.add(sitemap_url)
            try:
                response = await http.get(sitemap_url)
                root = _parse_xml(response.content)
            except (ParserError, etree.XMLSyntaxError, zlib.error):
                continue
            for node in root.xpath("//*[local-name()='url']"):
                locations = node.xpath("./*[local-name()='loc']/text()")
                if not locations or not _is_candidate(str(locations[0]).strip(), roots):
                    continue
                modified = node.xpath("./*[local-name()='lastmod']/text()")
                candidates[canonicalize_url(str(locations[0]).strip())] = utc_datetime(
                    str(modified[0]) if modified else None
                )
                if len(candidates) >= MAX_SITEMAP_URLS:
                    break
            for location in root.xpath("//*[local-name()='sitemap']/*[local-name()='loc']/text()"):
                sitemaps.append(str(location).strip())

        try:
            homepage_response = await http.get(homepage)
            base = str(homepage_response.url)
            for href in html.fromstring(homepage_response.text).xpath("//a[@href]/@href"):
                absolute = urljoin(base, str(href))
                if _is_candidate(absolute, roots):
                    candidates.setdefault(canonicalize_url(absolute), None)
        except (ParserError, etree.ParserError, ValueError):
            pass
        return candidates


def _select(
    candidates: dict[str, datetime | None], plan: CollectPlan, newsroom: str | None, domain: str = ""
) -> list[str]:
    """Round-robin over page kinds so fresh news does not crowd out strategy, IR and about pages.

    Country copies of the same page (/de-en/about, /fr-en/about…) count once: the global/English copy on the
    company's primary domain wins.
    """
    groups: dict[str, list[str]] = {kind: [] for kind in KIND_ORDER}
    for url in _one_per_locale(candidates, domain):
        groups.setdefault(page_kind(url), []).append(url)

    def rank(url: str) -> tuple[int, int, float, int]:
        lastmod = candidates[url]
        fresh = int(lastmod is not None and lastmod >= plan.since)
        return (int(url == newsroom), fresh, lastmod.timestamp() if lastmod else 0.0, -len(_segments(url)))

    ordered = [sorted(groups[kind], key=rank, reverse=True) for kind in KIND_ORDER]
    selected: list[str] = []
    while len(selected) < plan.max_website_pages and any(ordered):
        for group in ordered:
            if group and len(selected) < plan.max_website_pages:
                selected.append(group.pop(0))
    return selected


def _one_per_locale(candidates: dict[str, datetime | None], domain: str) -> list[str]:
    def preference(url: str) -> tuple[int, int]:
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        locales = [segment for segment in _segments(url) if LOCALE_SEGMENT.match(segment)]
        english = all("en" in re.split(r"[-_]", locale) for locale in locales)
        return (
            0 if host == domain or host.endswith(f".{domain}") else 1,
            0 if not locales else 1 if english else 2,
        )

    chosen: dict[str, str] = {}
    for url in sorted(candidates, key=preference):
        parts = urlsplit(url)
        key = "/".join(segment for segment in _segments(url) if not LOCALE_SEGMENT.match(segment))
        chosen.setdefault(f"{parts.query}|{key}", url)
    keep = set(chosen.values())
    return [url for url in candidates if url in keep]


def _article_links(document: str, base: str, listing_url: str, roots: set[str]) -> Iterable[str]:
    listing_path = urlsplit(canonicalize_url(listing_url)).path
    listing_depth = len(_segments(listing_url))
    try:
        hrefs = html.fromstring(document).xpath("//a[@href]/@href")
    except (etree.ParserError, ValueError):
        return
    for href in hrefs:
        link = canonicalize_url(urljoin(base, str(href)))
        if not _is_candidate(link, roots) or "page=" in urlsplit(link).query:
            continue
        path = urlsplit(link).path
        below_listing = bool(listing_path) and path.startswith(f"{listing_path}/")
        if len(_segments(link)) > listing_depth and (below_listing or page_kind(link) == "news"):
            yield link


def _is_listing(url: str, newsroom: str | None) -> bool:
    return url == newsroom or len(_segments(url)) <= 2


def _site_roots(company: ResolvedCompany) -> set[str]:
    ats_host = company.ats.host.lower() if company.ats and company.ats.host else None
    roots = {company.domain, *company.own_domains}
    return {root.lower().removeprefix("www.") for root in roots if root and root.lower() != ats_host}


def _is_own(url: str, roots: set[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return any(host == root or host.endswith(f".{root}") for root in roots)


def _is_candidate(url: str, roots: set[str]) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme in {"http", "https"}
        and _is_own(url, roots)
        and not parts.path.lower().endswith(ASSET_SUFFIXES)
    )


def _segments(url: str) -> list[str]:
    return [segment for segment in urlsplit(url).path.lower().split("/") if segment]


def _parse_xml(content: bytes) -> etree._Element:
    if content[:2] == b"\x1f\x8b":  # gzipped sitemap served without Content-Encoding
        content = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(content, MAX_SITEMAP_BYTES)
    return etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False, no_network=True))


def _html_title(document: str) -> str | None:
    try:
        values = html.fromstring(document).xpath("//title/text()")
        return str(values[0]).strip() if values else None
    except (etree.ParserError, ValueError):
        return None
