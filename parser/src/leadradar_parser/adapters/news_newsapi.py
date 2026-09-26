import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..http import HttpClient
from ..normalize import utc_datetime
from .common import make_document, search_name

API_URL = "https://newsapi.org/v2/everything"
# Developer plan: articles up to ~1 month old, 100 requests/day, development use only (SPEC §4).
MAX_AGE = timedelta(days=28)
TRUNCATION = re.compile(r"\s*\[\+\d+ chars\]\s*$")


class NewsApiAdapter:
    """NewsAPI.org /everything; enabled only when NEWSAPI_KEY is set."""

    id = "newsapi"
    source_type = "news"
    requires_env = "NEWSAPI_KEY"
    rate_limit = RateLimit(requests=1, per_seconds=1, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        api_key = http.settings.env(self.requires_env)
        if not api_key:
            return
        since = max(plan.since, datetime.now(UTC) - MAX_AGE)
        response = await http.get(
            API_URL,
            check_robots=False,
            attempts=1,
            # Key in a header, never in the URL: URLs end up in error messages and the HTTP cache.
            headers={"X-Api-Key": api_key},
            params={
                "q": f'"{search_name(company)}"',
                "sortBy": "publishedAt",
                "pageSize": min(plan.max_items_per_source, 100),
                "from": since.strftime("%Y-%m-%dT%H:%M:%S"),
            },
        )
        for item in response.json().get("articles") or []:
            title = (item.get("title") or "").strip()
            url = item.get("url") or ""
            if not title or title == "[Removed]" or not url:
                continue
            body = TRUNCATION.sub("", item.get("content") or item.get("description") or "")
            source = item.get("source")
            yield make_document(
                source_type="news",
                source_name=self.id,
                url=url,
                title=title,
                text=f"{title}\n{body}".strip(),
                published_at=utc_datetime(item.get("publishedAt")),
                meta={
                    "publisher": source.get("name") if isinstance(source, dict) else None,
                    "headline_only": not body,
                    "truncated": bool(body),
                },
            )
