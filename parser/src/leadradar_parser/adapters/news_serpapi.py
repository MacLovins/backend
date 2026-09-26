import os
import urllib.parse
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..http import HttpClient
from ..normalize import canonicalize_url, content_hash
from .base import SourceAdapter


class SerpApiAdapter(SourceAdapter):
    """SerpAPI adapter for search snippets, news, and anti-bot bypass.

    Uses SERPAPI_KEY when configured.
    """

    id = "serpapi"
    source_type = "news"
    requires_env = "SERPAPI_KEY"
    rate_limit = RateLimit(requests=2, per_seconds=1, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        api_key = os.getenv("SERPAPI_KEY")
        if not api_key:
            return

        name = company.name or company.domain
        queries = [
            f'"{name}" (automation OR AI OR cybersecurity OR digital)',
            f'site:{company.domain} (news OR press OR "about us")',
        ]

        for query in queries:
            params = {
                "engine": "google_news",
                "q": query,
                "api_key": api_key,
                "num": 15,
            }
            url = f"https://serpapi.com/search.json?{urllib.parse.urlencode(params)}"
            try:
                response = await http.get(url, check_robots=False, attempts=1)
                if response.status_code != 200:
                    continue
                data = response.json()
            except Exception:
                continue

            results = data.get("news_results", []) or data.get("organic_results", [])
            for item in results:
                title = (item.get("title") or "").strip()
                if not title:
                    continue

                snippet = item.get("snippet") or item.get("description") or ""
                link = item.get("link") or ""
                source_name = (
                    item.get("source", {}).get("name") if isinstance(item.get("source"), dict) else "SerpAPI"
                )

                yield Document(
                    source_type="news",
                    source_name="serpapi",
                    url=link,
                    canonical_url=canonicalize_url(link),
                    title=title,
                    text=f"{title}\n\n{snippet}" if snippet else title,
                    published_at=datetime.now(UTC),
                    fetched_at=datetime.now(UTC),
                    language="en",
                    content_hash=content_hash(f"{title}{snippet}"),
                    meta={"publisher": source_name, "query": query},
                )
