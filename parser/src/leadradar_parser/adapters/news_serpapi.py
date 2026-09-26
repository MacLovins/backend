from collections.abc import AsyncIterator

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..errors import ParserError
from ..http import HttpClient
from ..normalize import utc_datetime
from .common import make_document, raise_if_nothing_succeeded, search_name

API_URL = "https://serpapi.com/search.json"


class SerpApiAdapter:
    """SerpAPI Google News engine (search snippets); enabled only when SERPAPI_KEY is set.

    SerpAPI only accepts the key as a query parameter; the HTTP layer redacts it from error messages.
    """

    id = "serpapi"
    source_type = "news"
    requires_env = "SERPAPI_KEY"
    rate_limit = RateLimit(requests=1, per_seconds=1, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        api_key = http.settings.env(self.requires_env)
        if not api_key:
            return
        name = search_name(company)
        queries = [
            f'"{name}" (automation OR AI OR cybersecurity OR digital)',
            f"site:{company.domain} (news OR press)",
        ]
        failures: list[Exception] = []
        succeeded = emitted = 0
        seen: set[str] = set()
        for query in queries:
            try:
                response = await http.get(
                    API_URL,
                    check_robots=False,
                    attempts=1,
                    timeout=25.0,
                    params={"engine": "google_news", "q": query, "api_key": api_key},
                )
                data = response.json()
            except (ParserError, ValueError) as exc:
                failures.append(exc)
                continue
            succeeded += 1
            for item in data.get("news_results") or data.get("organic_results") or []:
                title = (item.get("title") or "").strip()
                link = item.get("link") or ""
                published_at = utc_datetime(item.get("iso_date"))
                if not title or not link or link in seen or (published_at and published_at < plan.since):
                    continue
                seen.add(link)
                snippet = item.get("snippet") or ""
                source = item.get("source")
                yield make_document(
                    source_type="news",
                    source_name=self.id,
                    url=link,
                    title=title,
                    text=f"{title}\n{snippet}".strip(),
                    published_at=published_at,
                    meta={
                        "publisher": source.get("name") if isinstance(source, dict) else source,
                        "headline_only": True,
                        "query": query,
                    },
                )
                emitted += 1
                if emitted >= plan.max_items_per_source:
                    return
        raise_if_nothing_succeeded(succeeded, failures)
