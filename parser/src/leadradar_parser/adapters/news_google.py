import asyncio
from collections.abc import AsyncIterator

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..errors import ParserError
from ..http import HttpClient
from ..normalize import utc_datetime
from .common import make_document, parse_feed, raise_if_nothing_succeeded, search_name

RSS_URL = "https://news.google.com/rss/search"
# Google News editions per plan language: (hl, gl, ceid)
EDITIONS = {
    "en": ("en-US", "US", "US:en"),
    "de": ("de", "DE", "DE:de"),
    "fr": ("fr", "FR", "FR:fr"),
    "it": ("it", "IT", "IT:it"),
    "es": ("es", "ES", "ES:es"),
    "nl": ("nl", "NL", "NL:nl"),
}
MAX_EDITIONS = 2
# Used when the plan has no news_topics (the ai presets normally pass them).
DEFAULT_TOPICS = (
    '(AI OR agentic OR automation OR robotics OR "digital transformation")',
    '(cybersecurity OR "data breach" OR ransomware OR NIS2 OR DORA)',
    '(strategy OR efficiency OR "cost reduction" OR restructuring OR CEO OR CIO)',
)


class GoogleNewsAdapter:
    """Google News RSS search: headlines + snippets, no key. Links are Google redirect URLs (headline only)."""

    id = "google_news"
    source_type = "news"
    requires_env = None
    rate_limit = RateLimit(requests=1, per_seconds=1, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        name = search_name(company)
        languages = [code for code in plan.languages if code in EDITIONS][:MAX_EDITIONS] or ["en"]
        seen: set[str] = set()
        failures: list[Exception] = []
        succeeded = emitted = 0
        for query in self._queries(name, plan):
            for language in languages:
                hl, gl, ceid = EDITIONS[language]
                try:
                    # News RSS is a feed endpoint; robots.txt handling is decided in SPEC §1.7.1 (see README).
                    response = await http.get(
                        RSS_URL,
                        check_robots=False,
                        attempts=1,
                        params={"q": query, "hl": hl, "gl": gl, "ceid": ceid},
                    )
                    items = parse_feed(response.content)
                except (ParserError, ValueError) as exc:
                    failures.append(exc)
                    continue
                succeeded += 1
                for item in items:
                    title = _strip_publisher(item.title, item.source)
                    published_at = utc_datetime(item.published)
                    key = title.casefold()
                    if not title or key in seen or (published_at and published_at < plan.since):
                        continue
                    seen.add(key)
                    description = item.description if item.description.casefold() != key else ""
                    yield make_document(
                        source_type="news",
                        source_name=self.id,
                        url=item.link or RSS_URL,
                        title=title,
                        text=f"{title}\n{description}".strip(),
                        published_at=published_at,
                        meta={
                            "publisher": item.source,
                            "headline_only": True,
                            "query": query,
                            "edition": ceid,
                        },
                    )
                    emitted += 1
                    if emitted >= plan.max_items_per_source:
                        return
                await asyncio.sleep(0.1)
        raise_if_nothing_succeeded(succeeded, failures)

    @staticmethod
    def _queries(name: str, plan: CollectPlan) -> list[str]:
        base = f'"{name}"'
        if plan.news_topics:
            topics = " OR ".join(f'"{topic}"' if " " in topic else topic for topic in plan.news_topics[:8])
            return [base, f"{base} ({topics})"]
        return [base, *(f"{base} {topics}" for topics in DEFAULT_TOPICS)]


def _strip_publisher(title: str, source: str | None) -> str:
    title = title.strip()
    if source and title.endswith(f" - {source}"):
        return title[: -len(f" - {source}")].strip()
    return title
