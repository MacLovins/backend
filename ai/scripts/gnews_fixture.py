"""Headline-only news fixture from Google News RSS — a stopgap while lr-parser cannot collect.

Why: company sites (dhl.com, lufthansagroup.com) are bot-protected, GDELT answers 429, and `lr-parser collect`
hangs when the company site is unreachable. Google News RSS gives titles only (links are Google redirects),
so every document is marked headline_only (reliability 0.6 in scoring).

    uv run python ai/scripts/gnews_fixture.py ai/tests/fixtures/dhl.jsonl \\
        '"DHL" (AI OR automation) when:365d' '"DHL Group" (strategy OR efficiency) when:365d'
"""

import sys
import time
import urllib.parse
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from leadradar_parser import Document
from leadradar_parser.normalize import content_hash
from lxml import etree

if len(sys.argv) < 3 or sys.argv[1].startswith("-"):
    sys.exit("usage: gnews_fixture.py OUT.jsonl QUERY [QUERY ...]")
out = Path(sys.argv[1])
queries = sys.argv[2:]
seen, docs = set(), []
with httpx.Client(headers={"User-Agent": "LeadRadarBot/0.1 (hackathon)"}, timeout=20) as client:
    for q in queries:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}
        )
        root = etree.fromstring(client.get(url).content)
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            source = item.findtext("source") or ""
            if title.endswith(" - " + source):
                title = title[: -len(" - " + source)]
            key = title.casefold()
            if not title or key in seen:
                continue
            seen.add(key)
            link = item.findtext("link")
            docs.append(
                Document(
                    source_type="news",
                    source_name="google_news",
                    url=link,
                    canonical_url=link,
                    title=title,
                    text=title,
                    published_at=parsedate_to_datetime(item.findtext("pubDate")).astimezone(UTC),
                    fetched_at=datetime.now(UTC),
                    language="en",
                    content_hash=content_hash(title),
                    meta={"publisher": source, "headline_only": True},
                )
            )
        time.sleep(2)
out.write_text("".join(d.model_dump_json() + "\n" for d in docs), encoding="utf-8")
print(f"{len(docs)} headlines from {len(queries)} queries -> {out}")
