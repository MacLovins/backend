from collections.abc import AsyncIterator
from urllib.parse import quote

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..errors import ParserError
from ..http import HttpClient
from ..normalize import utc_datetime
from .common import make_document, parse_feed, raise_if_nothing_succeeded, search_name
from .news_google import GoogleNewsAdapter


class RSSHubAdapter:
    """Keyword news feeds from a self-hosted RSSHub instance.

    Enabled only when RSSHUB_BASE_URL is set: the public rsshub.app answers automated clients with a
    Cloudflare challenge (HTTP 403), so it cannot be a default source. The route is RSSHUB_ROUTE
    (default `/bing/search/{query}`); queries are the company search name plus the plan's news topics.
    """

    id = "rsshub"
    source_type = "news"
    requires_env = "RSSHUB_BASE_URL"
    rate_limit = RateLimit(requests=1, per_seconds=1, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        base_url = (http.settings.env(self.requires_env) or "").rstrip("/")
        if not base_url:
            return
        route = "/" + http.settings.rsshub_route.lstrip("/")
        failures: list[Exception] = []
        succeeded = emitted = 0
        seen: set[str] = set()
        for query in self.queries(company, plan):
            feed_url = base_url + route.replace("{query}", quote(query, safe=""))
            try:
                items = parse_feed((await http.get(feed_url, check_robots=False, attempts=1)).content)
            except (ParserError, ValueError) as exc:
                failures.append(exc)
                continue
            succeeded += 1
            for item in items:
                published_at = utc_datetime(item.published)  # unknown date stays None, never "now"
                if not item.title or not item.link or item.link in seen:
                    continue
                if published_at and published_at < plan.since:
                    continue
                seen.add(item.link)
                yield make_document(  # language is detected from the title + snippet
                    source_type="news",
                    source_name=self.id,
                    url=item.link,
                    title=item.title,
                    text=f"{item.title}\n{item.description}".strip(),
                    published_at=published_at,
                    meta={"feed": feed_url, "query": query, "publisher": item.source, "headline_only": True},
                )
                emitted += 1
                if emitted >= plan.max_items_per_source:
                    return
        raise_if_nothing_succeeded(succeeded, failures)

    @staticmethod
    def queries(company: ResolvedCompany, plan: CollectPlan) -> list[str]:
        """`"<search name>"` and `"<search name>" (<news topics>)` — the same queries as Google News."""
        return GoogleNewsAdapter._queries(search_name(company), plan)
