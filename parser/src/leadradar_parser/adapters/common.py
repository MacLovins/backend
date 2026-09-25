import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import trafilatura

from ..contracts import Document, SourceType
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
