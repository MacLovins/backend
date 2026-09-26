import os
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..http import HttpClient
from ..normalize import canonicalize_url, content_hash
from .base import SourceAdapter


class RSSHubAdapter(SourceAdapter):
    """Fetches company news, blog posts, and press releases via RSSHub feeds.

    Defaults to public RSSHub instance or custom RSSHUB_BASE_URL.
    """

    id = "rsshub"
    source_type = "news"
    requires_env = None
    rate_limit = RateLimit(requests=2, per_seconds=1, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        base_url = os.getenv("RSSHUB_BASE_URL", "https://rsshub.app").rstrip("/")
        name = company.name or company.domain

        # Query RSSHub search / google news or company custom feeds
        queries = [
            f"{name} AI automation",
            f"{name} cybersecurity digital transformation",
        ]

        seen_links: set[str] = set()
        for q in queries:
            feed_url = f"{base_url}/google/news/{urllib.parse.quote(q)}"
            try:
                response = await http.get(feed_url, check_robots=False, attempts=1)
                if response.status_code != 200:
                    continue
                root = ET.fromstring(response.content)
            except Exception:
                continue

            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if not title or link in seen_links:
                    continue
                seen_links.add(link)

                description = (item.findtext("description") or "").strip()
                pub_date_str = item.findtext("pubDate")
                published_at = datetime.now(UTC)
                if pub_date_str:
                    try:
                        published_at = parsedate_to_datetime(pub_date_str).astimezone(UTC)
                    except Exception:
                        pass

                yield Document(
                    source_type="news",
                    source_name=self.id,
                    url=link or f"{base_url}/news",
                    canonical_url=canonicalize_url(link) if link else "",
                    title=title,
                    text=f"{title}\n\n{description}" if description else title,
                    published_at=published_at,
                    fetched_at=datetime.now(UTC),
                    language="en",
                    content_hash=content_hash(f"{title}{link}"),
                    meta={"feed": feed_url, "query": q},
                )
