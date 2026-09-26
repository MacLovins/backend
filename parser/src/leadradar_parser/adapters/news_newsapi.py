import os
import urllib.parse
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..http import HttpClient
from ..normalize import canonicalize_url, content_hash
from .base import SourceAdapter


class NewsApiAdapter(SourceAdapter):
    """NewsAPI.org adapter for global news articles when NEWSAPI_KEY is configured."""

    id = "newsapi"
    source_type = "news"
    requires_env = "NEWSAPI_KEY"
    rate_limit = RateLimit(requests=1, per_seconds=1, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        api_key = os.getenv("NEWSAPI_KEY")
        if not api_key:
            return

        name = company.name or company.domain
        query = f'"{name}"'
        params = {
            "q": query,
            "apiKey": api_key,
            "sortBy": "publishedAt",
            "pageSize": 20,
            "language": "en",
        }
        from datetime import timedelta
        earliest_allowed = datetime.now(UTC) - timedelta(days=28)
        since_date = max(plan.since, earliest_allowed) if plan.since else earliest_allowed
        params["from"] = since_date.strftime("%Y-%m-%d")

        url = f"https://newsapi.org/v2/everything?{urllib.parse.urlencode(params)}"
        try:
            response = await http.get(
                url,
                check_robots=False,
                attempts=1,
                headers={"User-Agent": "LeadRadar/1.0"},
            )
            if response.status_code != 200:
                return
            data = response.json()
        except Exception:
            return

        articles = data.get("articles", [])
        for item in articles:
            title = (item.get("title") or "").strip()
            if not title or title == "[Removed]":
                continue

            content = item.get("content") or item.get("description") or title
            link = item.get("url") or ""
            source_info = item.get("source") or {}
            source_name = source_info.get("name") if isinstance(source_info, dict) else "NewsAPI"

            pub_str = item.get("publishedAt")
            published_at = datetime.now(UTC)
            if pub_str:
                try:
                    published_at = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                except Exception:
                    pass

            yield Document(
                source_type="news",
                source_name="newsapi",
                url=link,
                canonical_url=canonicalize_url(link),
                title=title,
                text=f"{title}\n\n{content}",
                published_at=published_at,
                fetched_at=datetime.now(UTC),
                language="en",
                content_hash=content_hash(f"{title}{content}"),
                meta={"publisher": source_name, "author": item.get("author")},
            )
