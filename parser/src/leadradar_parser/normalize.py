import hashlib
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fast_langdetect import detect

from .contracts import Document

TRACKING_PARAMS = {"gclid", "fbclid", "mc_cid", "mc_eid"}
MAX_PAGE_CHARS = 50_000
MAX_JOB_CHARS = 8_000
MAX_REPORT_CHARS = 40_000
MAX_TITLE_CHARS = 500
COOKIE_LINES = re.compile(
    r"^(accept (all )?cookies|cookie settings|manage cookies|privacy settings|alle cookies akzeptieren)$",
    re.IGNORECASE,
)


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port and parts.port not in {80, 443} else ""
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
        ]
    )
    path = parts.path.rstrip("/") or ""
    return urlunsplit((scheme, f"{host}{port}", path, query, ""))


def clean_text(text: str, *, max_chars: int = MAX_PAGE_CHARS) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        key = line.casefold()
        if not line or COOKIE_LINES.match(line) or key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines)[:max_chars].rstrip()


def content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip().casefold()
    return hashlib.sha256(normalized.encode()).hexdigest()


def detect_language(text: str) -> str | None:
    if len(text.strip()) < 40:
        return None
    try:
        result = detect(" ".join(text.split())[:400], low_memory=True)
        return str(result.get("lang")) if isinstance(result, dict) else None
    except (ValueError, RuntimeError):
        return None


def clean_title(title: str | None) -> str | None:
    if not title:
        return None
    cleaned = re.sub(r"\s+", " ", title).strip()
    return cleaned[:MAX_TITLE_CHARS].rstrip() or None


def utc_datetime(value: datetime | date | str | None) -> datetime | None:
    """Parse ISO 8601, RFC 2822 or date-only values; naive values are treated as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        parsed = _parse_datetime(value.strip())
        if parsed is None:
            return None
        value = parsed
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_datetime(value: str) -> datetime | None:
    cleaned = value.replace("Z", "+00:00")
    for candidate in (cleaned, cleaned.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None


def deduplicate(documents: Iterable[Document]) -> list[Document]:
    urls: set[str] = set()
    hashes: set[str] = set()
    unique: list[Document] = []
    for document in documents:
        if document.canonical_url in urls or document.content_hash in hashes:
            continue
        urls.add(document.canonical_url)
        hashes.add(document.content_hash)
        unique.append(document)
    return unique
