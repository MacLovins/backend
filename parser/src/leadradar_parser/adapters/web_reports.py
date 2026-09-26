"""PDF annual / strategy reports from the company's own site (SPEC PR-16, §1.7.5: relevant pages only, ≤ 40k)."""

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote, urljoin, urlsplit

from lxml import etree, html

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..errors import ParserError
from ..http import HttpClient
from ..normalize import MAX_REPORT_CHARS, canonicalize_url
from .common import is_html, make_document, raise_if_nothing_succeeded
from .web_site import LOCALE_SEGMENT as LOCALE
from .web_site import _is_own, _site_roots, page_kind, sitemap_entries

MAX_REPORT_PAGES = 6  # IR / report listing pages scanned for PDF links
MAX_PDF_PAGES = 600  # pages of one PDF that are searched for keywords
MAX_REPORT_AGE_YEARS = 2
# Weighted tokens in a PDF's URL or link text. Multilingual: EN, DE, FR, NL, IT, ES.
REPORT_TOKENS: dict[str, int] = {
    "annual report": 5, "annual-report": 5, "annualreport": 5, "annual_report": 5, "integrated report": 5,
    "geschaeftsbericht": 5, "geschäftsbericht": 5, "jahresbericht": 4, "konzernbericht": 4,
    "rapport annuel": 5, "rapport-annuel": 5, "document d'enregistrement universel": 5,
    "universal registration document": 5, "jaarverslag": 5, "relazione annuale": 5, "informe anual": 5,
    "strategy": 3, "strategie": 3, "stratégie": 3, "capital markets day": 3, "capital-markets-day": 3,
    "investor presentation": 2, "sustainability report": 2, "nachhaltigkeitsbericht": 2, "esg report": 2,
    "half-year": 1, "halbjahres": 1, "interim report": 1, "quarterly statement": 1, "quartalsmitteilung": 1,
    "10-k": 4, "20-f": 4, "report": 1, "bericht": 1, "rapport": 1,
}  # fmt: skip
EXCLUDED_TOKENS = (
    "agb", "terms", "privacy", "datenschutz", "imprint", "impressum", "cookie", "conditions", "gtc",
    "datasheet", "data-sheet", "manual", "menu", "price-list", "preisliste", "application-form", "flyer",
    "shareholdings", "anteilsbesitz",
)  # fmt: skip
REPORT_PAGE_TOKENS: dict[str, int] = {
    "annual-report": 3, "annual report": 3, "geschaeftsbericht": 3, "geschäftsbericht": 3, "reporting": 2,
    "reports": 2, "publications": 2, "publikationen": 2, "finanzberichte": 2, "rapport": 2, "financial": 1,
    "investor": 1, "investoren": 1, "strategy": 1, "strategie": 1, "results": 1, "downloads": 1,
}  # fmt: skip
# Kept pages must mention one of these (plus the plan's news topics); neighbours (± 1 page) are kept too.
DEFAULT_KEYWORDS = (
    "strategy", "strategic", "outlook", "guidance", "transformation", "digitalisation", "digitalization",
    "automation", "artificial intelligence", "AI", "cloud", "cyber", "cybersecurity", "information security",
    "IT", "investment", "capex", "cost reduction", "efficiency", "savings", "restructuring", "programme",
    "program", "risk", "Strategie", "Ausblick", "Prognose", "Digitalisierung", "Automatisierung",
    "Künstliche Intelligenz", "KI", "Informationssicherheit", "Investitionen", "Kostensenkung", "Effizienz",
    "Risiko", "stratégie", "perspectives", "numérique", "investissements", "efficacité", "cybersécurité",
)  # fmt: skip
YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
PDF_DATE = re.compile(r"^D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?")


@dataclass
class PdfLink:
    url: str
    text: str
    source: str  # the page (or sitemap) where the link was found
    score: int = 0
    year: int | None = None


class ReportsAdapter:
    """PDF reports linked from IR / annual-report pages and sitemaps of the company site.

    Downloads ≤ `reports_max_pdfs` PDFs of ≤ `reports_max_pdf_mb`, extracts text with PyMuPDF and keeps only
    pages that mention a configured keyword, ± 1 page, up to 40k characters.
    """

    id = "reports"
    source_type = "report"
    requires_env = None
    rate_limit = RateLimit(requests=1, per_seconds=1, scope="host")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        limit = min(http.settings.reports_max_pdfs, plan.max_items_per_source)
        if limit <= 0:
            return
        links = await find_report_links(company, http)
        keywords = keyword_pattern([*plan.news_topics, *DEFAULT_KEYWORDS])
        max_bytes = int(http.settings.reports_max_pdf_mb * 1024 * 1024)
        failures: list[Exception] = []
        attempted = parsed = emitted = 0
        for link in links:
            if emitted >= limit or attempted >= limit * 2:
                break
            attempted += 1
            try:
                download = await http.download(
                    link.url, max_bytes=max_bytes, timeout_s=http.settings.reports_download_timeout_s
                )
            except ParserError as exc:
                failures.append(exc)
                continue
            parsed += 1
            if not download.content.startswith(b"%PDF"):
                continue
            try:
                report = await asyncio.to_thread(extract_report, download.content, keywords)
            except (RuntimeError, ValueError) as exc:  # corrupt or encrypted PDF
                failures.append(ParserError(f"Unreadable PDF {link.url}: {exc}"))
                continue
            if report is None:
                continue
            published_at, date_source = report.published_at, "pdf_metadata"
            if published_at is None and link.year:
                published_at, date_source = datetime(link.year, 1, 1, tzinfo=UTC), "url_year"
            yield make_document(
                source_type="report",
                source_name=self.id,
                url=download.url,
                title=report.title or link.text or _filename(download.url),
                text=report.text,
                published_at=published_at,
                meta={
                    "page_kind": "report",
                    "pages": report.pages,
                    "page_count": report.page_count,
                    "source_page": link.source,
                    "date_source": date_source if published_at else None,
                    "truncated": report.truncated,
                },
                max_chars=MAX_REPORT_CHARS,
            )
            emitted += 1
        raise_if_nothing_succeeded(parsed, failures)


async def find_report_links(company: ResolvedCompany, http: HttpClient) -> list[PdfLink]:
    """Report-like PDFs of the company's own site, newest and most relevant first."""
    roots = _site_roots(company)
    pdfs: dict[str, PdfLink] = {}
    pages: dict[str, int] = {}

    def add_pdf(url: str, text: str, source: str) -> None:
        canonical = canonicalize_url(url)
        if canonical not in pdfs or (text and not pdfs[canonical].text):
            pdfs[canonical] = PdfLink(url=url, text=text, source=source)

    def add_page(url: str, text: str = "") -> None:
        canonical = canonicalize_url(url)
        if score := _report_page_score(canonical, text):
            pages[canonical] = max(score, pages.get(canonical, 0))

    async with aclosing(sitemap_entries(company, http, roots)) as entries:
        async for location, _ in entries:
            if _is_pdf(location):
                add_pdf(location, "", "sitemap")
            else:
                add_page(location)
    # Homepage first, then the best-scoring IR / report pages (depth 2: IR hub → "annual reports" page).
    # One language version per page: /en/investors and /de/investoren usually link the same PDFs.
    scanned: set[str] = set()
    url: str | None = company.homepage_url
    while url is not None and len(scanned) <= MAX_REPORT_PAGES:
        scanned.add(_locale_free(url))
        try:
            response = await http.get(url)
        except ParserError:
            response = None
        if response is not None and is_html(response):
            for href, text in _anchors(response.text, str(response.url)):
                if not _is_own(href, roots):
                    continue
                if _is_pdf(href):
                    add_pdf(href, text, str(response.url))
                else:
                    add_page(href, text)
        url = next((page for page, _ in _top(pages, len(pages)) if _locale_free(page) not in scanned), None)

    current_year = datetime.now(UTC).year
    ranked: list[PdfLink] = []
    for link in pdfs.values():
        haystack = f"{unquote(link.url)} {link.text}".casefold().replace("_", " ")
        if any(token in haystack for token in EXCLUDED_TOKENS):
            continue
        link.score = sum(weight for token, weight in REPORT_TOKENS.items() if token in haystack)
        years = [int(year) for year in YEAR.findall(haystack) if int(year) <= current_year + 1]
        link.year = max(years) if years else None
        if link.score <= 0 or (link.year is not None and link.year < current_year - MAX_REPORT_AGE_YEARS):
            continue
        ranked.append(link)
    # Relevance plus a recency bonus: last year's annual report outranks this year's interim statement.
    oldest = current_year - MAX_REPORT_AGE_YEARS
    ranked.sort(key=lambda link: link.score + 2 * max(0, (link.year or oldest) - oldest), reverse=True)
    # The same file is often linked from several hosts (www and a reporting hub): download it once.
    unique: dict[str, PdfLink] = {}
    for link in ranked:
        unique.setdefault(_filename(link.url).casefold(), link)
    return list(unique.values())


@dataclass(frozen=True)
class ExtractedReport:
    text: str
    title: str | None
    published_at: datetime | None
    pages: list[int]
    page_count: int
    truncated: bool


def extract_report(content: bytes, keywords: re.Pattern[str]) -> ExtractedReport | None:
    """Relevant pages (keyword hit ± 1) of a PDF; the best-scoring pages win when the 40k budget is tight."""
    import pymupdf  # heavy C extension: imported only when a PDF is actually processed

    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        if pdf.needs_pass:
            raise ValueError("encrypted PDF")
        metadata = pdf.metadata or {}
        page_count = pdf.page_count
        texts = [pdf[index].get_text("text") for index in range(min(page_count, MAX_PDF_PAGES))]
    hits = {
        index: len(set(match.casefold() for match in keywords.findall(text)))
        for index, text in enumerate(texts)
    }
    hits = {index: count for index, count in hits.items() if count}
    if not hits:
        return None
    selected: set[int] = set()
    budget = MAX_REPORT_CHARS
    truncated = False
    for index in sorted(hits, key=lambda item: (-hits[item], item)):
        group = [
            page for page in (index - 1, index, index + 1) if 0 <= page < len(texts) and page not in selected
        ]
        size = sum(len(texts[page]) for page in group)
        if size > budget:
            truncated = True
            if budget <= 0:
                break
            continue
        selected.update(group)
        budget -= size
    if not selected:  # a single oversized page: keep the best one, clean_text truncates it
        selected = {max(hits, key=hits.__getitem__)}
        truncated = True
    ordered = sorted(selected)
    text = "\n".join(f"[Page {page + 1}]\n{texts[page]}" for page in ordered)
    return ExtractedReport(
        text=text,
        title=(metadata.get("title") or "").strip() or None,
        published_at=pdf_date(metadata.get("creationDate") or metadata.get("modDate")),
        pages=[page + 1 for page in ordered],
        page_count=page_count,
        truncated=truncated or page_count > MAX_PDF_PAGES,
    )


def keyword_pattern(keywords: list[str] | tuple[str, ...]) -> re.Pattern[str]:
    """Whole-word match; short all-caps acronyms (AI, IT, KI) are case-sensitive to avoid "it"/"ai" noise."""
    parts = []
    for keyword in dict.fromkeys(keyword.strip() for keyword in keywords if keyword.strip()):
        escaped = re.escape(keyword)
        parts.append(escaped if keyword.isupper() and len(keyword) <= 4 else f"(?i:{escaped})")
    return re.compile(rf"(?<!\w)(?:{'|'.join(parts)})(?!\w)")


def pdf_date(value: str | None) -> datetime | None:
    """PDF date strings: D:YYYYMMDDHHmmSS+HH'mm' (only the date and time are used, as UTC)."""
    match = PDF_DATE.match(value or "")
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) if part else None for part in match.groups())
    try:
        return datetime(year or 1, month or 1, day or 1, hour or 0, minute or 0, second or 0, tzinfo=UTC)
    except ValueError:
        return None


def _report_page_score(url: str, text: str = "") -> int:
    parts = urlsplit(url)
    haystack = f"{parts.hostname or ''}{parts.path} {text}".casefold()
    score = sum(weight for token, weight in REPORT_PAGE_TOKENS.items() if token in haystack)
    return score + (1 if score and page_kind(url) in {"ir", "strategy"} else 0)


def _locale_free(url: str) -> str:
    """URL identity without language segments (/en/, /de-de/, /global-en/) and a trailing .html."""
    parts = urlsplit(canonicalize_url(url))
    segments = [segment for segment in parts.path.lower().split("/") if segment and not LOCALE.match(segment)]
    return f"{parts.hostname}/{'/'.join(segments)}".removesuffix(".html")


def _top(pages: dict[str, int], limit: int) -> list[tuple[str, int]]:
    return sorted(pages.items(), key=lambda item: (-item[1], len(item[0])))[:limit]


def _anchors(document: str, base: str) -> list[tuple[str, str]]:
    try:
        tree = html.fromstring(document)
    except (etree.ParserError, ValueError):
        return []
    anchors = []
    for anchor in tree.xpath("//a[@href]"):
        href = urljoin(base, str(anchor.get("href")))
        if urlsplit(href).scheme in {"http", "https"}:
            text = " ".join(" ".join(anchor.itertext()).split()) or str(anchor.get("title") or "")
            anchors.append((href, text))
    return anchors


def _is_pdf(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


def _filename(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])
