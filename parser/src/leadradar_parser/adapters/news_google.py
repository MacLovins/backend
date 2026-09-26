import asyncio
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..http import HttpClient
from ..normalize import canonicalize_url, content_hash
from .base import SourceAdapter


class GoogleNewsAdapter(SourceAdapter):
    """Fetches company news via Google News RSS and RSSHub feeds.

    Zero API key required, reliable, resilient to rate limits, supports multi-query search.
    """

    id = "google_news"
    source_type = "news"
    requires_env = None
    rate_limit = RateLimit(requests=2, per_seconds=1, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        seen_titles: set[str] = set()
        queries = self._build_queries(company, plan)

        for query in queries:
            params = urllib.parse.urlencode(
                {
                    "q": query,
                    "hl": "en-US",
                    "gl": "US",
                    "ceid": "US:en",
                }
            )
            url = f"https://news.google.com/rss/search?{params}"

            try:
                response = await http.get(url, check_robots=False, attempts=1)
                if response.status_code != 200:
                    continue
            except Exception:
                continue

            try:
                root = ET.fromstring(response.content)
            except Exception:
                continue

            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                source = (item.findtext("source") or "").strip()
                if title.endswith(f" - {source}"):
                    title = title[: -len(f" - {source}")].strip()

                key = title.casefold()
                if not title or key in seen_titles:
                    continue
                seen_titles.add(key)

                pub_date_str = item.findtext("pubDate")
                published_at = None
                if pub_date_str:
                    try:
                        published_at = parsedate_to_datetime(pub_date_str).astimezone(UTC)
                    except Exception:
                        pass

                if published_at and plan.since and published_at < plan.since:
                    continue

                link = (item.findtext("link") or "").strip()
                description = (item.findtext("description") or "").strip()

                doc = Document(
                    source_type="news",
                    source_name="google_news",
                    url=link or f"https://news.google.com/?q={urllib.parse.quote(title)}",
                    canonical_url=canonicalize_url(link) if link else "",
                    title=title,
                    text=f"{title}\n\n{description}" if description else title,
                    published_at=published_at or datetime.now(UTC),
                    fetched_at=datetime.now(UTC),
                    language="en",
                    content_hash=content_hash(title),
                    meta={
                        "publisher": source,
                        "headline_only": True,
                        "query": query,
                    },
                )
                yield doc

            # Gentle spacing between query batches
            await asyncio.sleep(0.1)

    def _build_queries(self, company: ResolvedCompany, plan: CollectPlan) -> list[str]:
        name = company.name or company.domain
        base_name = f'"{name}"'

        queries: list[str] = [
            f'{base_name} (AI OR "agentic" OR automation OR robotics OR IT)',
            f'{base_name} (cybersecurity OR "data breach" OR NIS2 OR DORA OR cloud)',
            f'{base_name} (strategy OR efficiency OR "cost reduction" OR restructuring OR digital)',
        ]

        if plan.news_topics:
            topics_clause = " OR ".join(f'"{t}"' for t in plan.news_topics[:3])
            queries.append(f"{base_name} ({topics_clause})")

        return queries
