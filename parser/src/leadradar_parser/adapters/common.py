import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import trafilatura
from lxml import etree, html

from ..contracts import Document, ResolvedCompany, SourceType
from ..errors import ParserError
from ..normalize import (
    MAX_PAGE_CHARS,
    canonicalize_url,
    clean_text,
    clean_title,
    content_hash,
    detect_language,
    utc_datetime,
)


def make_document(
    *,
    source_type: SourceType,
    source_name: str,
    url: str,
    text: str,
    title: str | None = None,
    published_at: datetime | str | None = None,
    language: str | None = None,
    meta: dict[str, Any] | None = None,
    max_chars: int = MAX_PAGE_CHARS,
) -> Document:
    normalized = clean_text(text, max_chars=max_chars)
    return Document(
        source_type=source_type,
        source_name=source_name,
        url=url,
        canonical_url=canonicalize_url(url),
        title=clean_title(title),
        text=normalized,
        published_at=utc_datetime(published_at),
        fetched_at=datetime.now(UTC),
        language=language or detect_language(normalized),
        content_hash=content_hash(normalized),
        meta=meta or {},
    )


@dataclass(frozen=True)
class ExtractedPage:
    text: str
    title: str | None
    date: str | None


def is_html(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    return not content_type or "html" in content_type or "xml" in content_type


async def extract_page(document: str, url: str | None = None) -> ExtractedPage | None:
    """Main text + metadata via trafilatura, off the event loop (it is CPU-bound)."""
    return await asyncio.to_thread(_extract_page, document, url)


def _extract_page(document: str, url: str | None) -> ExtractedPage | None:
    raw = trafilatura.extract(
        document,
        url=url,
        output_format="json",
        with_metadata=True,
        favor_precision=True,
        include_comments=False,
    )
    if not raw:
        return None
    data = json.loads(raw)
    text = str(data.get("text") or "").strip()
    if not text:
        return None
    return ExtractedPage(text=text, title=data.get("title") or None, date=data.get("date") or None)


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    description: str
    published: str | None
    source: str | None


def search_name(company: ResolvedCompany) -> str:
    """Name for news queries: legal name first; short names like "DHL" get a longer alias (homonyms)."""
    legal_name = company.firmographics.legal_name if company.firmographics else None
    name = (legal_name or company.name or company.domain).strip()
    if len(name) <= 4:
        longer = next((alias for alias in company.aliases if len(alias.strip()) > len(name)), None)
        name = longer.strip() if longer else name
    return name.replace('"', "")


def parse_feed(content: bytes) -> list[FeedItem]:
    """RSS 2.0 items; entity expansion and network access are disabled (untrusted XML)."""
    try:
        root = etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as exc:
        raise ParserError(f"Invalid RSS feed: {exc}") from exc
    items = []
    for node in root.iter("item"):
        items.append(
            FeedItem(
                title=(node.findtext("title") or "").strip(),
                link=(node.findtext("link") or "").strip(),
                description=plain_text(node.findtext("description") or ""),
                published=(node.findtext("pubDate") or "").strip() or None,
                source=(node.findtext("source") or "").strip() or None,
            )
        )
    return items


def plain_text(value: str) -> str:
    value = value.strip()
    if not value or "<" not in value:
        return value
    try:
        return " ".join(html.fromstring(value).text_content().split())
    except (etree.ParserError, ValueError):
        return value


def raise_if_nothing_succeeded(succeeded: int, failures: list[Exception]) -> None:
    """Adapters with several queries tolerate partial failures, but a total failure must reach `errors`."""
    if not succeeded and failures:
        raise failures[0]
