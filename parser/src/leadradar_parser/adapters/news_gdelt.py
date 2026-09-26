import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..errors import ParserError, SourceRateLimited
from ..http import HttpClient, shared_lock
from ..normalize import canonicalize_url
from .common import extract_page, is_html, make_document, search_name

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
MIN_INTERVAL_S = 5.0
GDELT_TIMEOUT_S = 30.0  # the DOC API regularly needs 10-20 s
# Consecutive 429s open the breaker for 60 s, then 120 s, then 15 min (SPEC §1.7.1).
BREAKER_PAUSES_S = (60, 120, 900)
MAX_ARTICLES = 20
# GDELT `sourcelang:` accepts language names, not ISO 639-1 codes.
GDELT_LANGUAGES = {
    "en": "english",
    "de": "german",
    "fr": "french",
    "es": "spanish",
    "it": "italian",
    "nl": "dutch",
    "pt": "portuguese",
    "pl": "polish",
    "ro": "romanian",
    "sv": "swedish",
    "da": "danish",
    "fi": "finnish",
    "no": "norwegian",
    "cs": "czech",
    "hu": "hungarian",
}
ISO_BY_GDELT_LANGUAGE = {name: code for code, name in GDELT_LANGUAGES.items()}

# Process-wide state: GDELT limits per IP, not per client.
_last_request_at = 0.0
_blocked_until = 0.0
_consecutive_429 = 0


class GdeltAdapter:
    id = "gdelt"
    source_type = "news"
    requires_env = None
    rate_limit = RateLimit(requests=1, per_seconds=5, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        articles: list[dict[str, object]] = []
        for query, timespan in self._queries(company, plan):
            response = await self._search(http, query, timespan)
            try:
                payload = response.json()
            except ValueError as exc:  # GDELT answers query errors with plain text and HTTP 200
                raise ParserError(f"GDELT rejected query {query!r}: {response.text[:200]}") from exc
            articles.extend(payload.get("articles") or [])
        articles.sort(key=lambda item: str(item.get("seendate") or ""), reverse=True)

        seen: set[str] = set()
        emitted = 0
        for item in articles:
            url = str(item.get("url") or "")
            canonical = canonicalize_url(url) if url else ""
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            published_at = _gdelt_datetime(item.get("seendate"))
            if published_at and published_at < plan.since:
                continue
            title = str(item.get("title") or "").strip() or None
            text = title or ""
            headline_only = True
            try:
                article = await http.get(url, attempts=1)
                page = await extract_page(article.text, url) if is_html(article) else None
                if page:
                    text = page.text
                    headline_only = False
            except (ParserError, httpx.HTTPError):
                pass
            if not text:
                continue
            yield make_document(
                source_type="news",
                source_name=self.id,
                url=url,
                title=title,
                text=text,
                published_at=published_at,
                language=_iso_language(item.get("language")),
                meta={
                    "publisher": str(item.get("domain") or urlsplit(url).hostname or ""),
                    "headline_only": headline_only,
                },
            )
            emitted += 1
            if emitted >= min(plan.max_items_per_source, MAX_ARTICLES):
                break

    @staticmethod
    def _queries(company: ResolvedCompany, plan: CollectPlan) -> list[tuple[str, str]]:
        name = search_name(company)
        languages = [
            f"sourcelang:{GDELT_LANGUAGES[code]}" for code in plan.languages if code in GDELT_LANGUAGES
        ]
        queries = [(f'"{name}" {_any_of(languages[:5])}'.strip(), "30d")]
        if plan.news_topics:
            topics = [f'"{topic}"' if " " in topic else topic for topic in plan.news_topics[:8]]
            queries.append((f'"{name}" {_any_of(topics)}', "3months"))
        return queries

    async def _search(self, http: HttpClient, query: str, timespan: str) -> httpx.Response:
        global _blocked_until, _consecutive_429, _last_request_at
        async with shared_lock("gdelt"):
            now = time.monotonic()
            if now < _blocked_until:
                raise SourceRateLimited("GDELT circuit breaker is open", int(_blocked_until - now) + 1)
            delay = MIN_INTERVAL_S - (now - _last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                response = await http.get(
                    GDELT_URL,
                    check_robots=False,
                    attempts=1,  # retries are handled by the breaker, not by hammering the API
                    timeout=GDELT_TIMEOUT_S,
                    params={
                        "query": query,
                        "mode": "ArtList",
                        "format": "json",
                        "maxrecords": 75,
                        "sort": "DateDesc",
                        "timespan": timespan,
                    },
                )
            except SourceRateLimited as exc:
                _last_request_at = time.monotonic()
                pause = BREAKER_PAUSES_S[min(_consecutive_429, len(BREAKER_PAUSES_S) - 1)]
                _consecutive_429 += 1
                _blocked_until = _last_request_at + max(pause, exc.retry_after_s or 0)
                raise SourceRateLimited(
                    f"GDELT rate limited; source paused for {pause}s", int(_blocked_until - _last_request_at)
                ) from exc
            except BaseException:
                _last_request_at = time.monotonic()
                raise
            _consecutive_429 = 0
            if not response.extensions.get("hishel_from_cache"):
                _last_request_at = time.monotonic()
            return response


def _any_of(terms: list[str]) -> str:
    # GDELT only accepts parentheses around OR'ed statements.
    if not terms:
        return ""
    return terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"


def _iso_language(value: object) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    return ISO_BY_GDELT_LANGUAGE.get(raw, raw if len(raw) == 2 else None)


def _gdelt_datetime(value: object) -> datetime | None:
    if not value:
        return None
    raw = str(value)
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
