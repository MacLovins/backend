from datetime import UTC, datetime

from leadradar_parser.adapters.common import make_document
from leadradar_parser.normalize import canonicalize_url, clean_text, content_hash, deduplicate


def test_canonical_url_removes_tracking_fragment_and_trailing_slash() -> None:
    url = "HTTPS://Example.COM/news/?utm_source=x&keep=yes&fbclid=secret#part"
    assert canonicalize_url(url) == "https://example.com/news?keep=yes"


def test_clean_text_removes_duplicates_and_cookie_lines() -> None:
    assert clean_text("Title\nAccept all cookies\nBody   text\nBody text") == "Title\nBody text"


def test_hash_is_case_and_whitespace_insensitive() -> None:
    assert content_hash("Hello  WORLD") == content_hash(" hello world ")


def test_deduplicate_uses_url_and_hash() -> None:
    first = make_document(source_type="news", source_name="test", url="https://a.test/1", text="First")
    same_text = make_document(source_type="news", source_name="test", url="https://a.test/2", text="first")
    same_url = make_document(source_type="news", source_name="test", url="https://a.test/1", text="Other")
    assert deduplicate([first, same_text, same_url]) == [first]
    assert first.fetched_at <= datetime.now(UTC)


def test_utc_datetime_parses_iso_rfc2822_and_dates() -> None:
    from leadradar_parser.normalize import utc_datetime

    assert utc_datetime("2026-09-20T10:00:00+02:00") == datetime(2026, 9, 20, 8, tzinfo=UTC)
    assert utc_datetime("Sun, 20 Sep 2026 10:00:00 +0200") == datetime(2026, 9, 20, 8, tzinfo=UTC)
    assert utc_datetime("2026-09-20") == datetime(2026, 9, 20, tzinfo=UTC)
    assert utc_datetime(datetime(2026, 9, 20)) == datetime(2026, 9, 20, tzinfo=UTC)
    assert utc_datetime("not a date") is None


def test_document_limits_and_utc() -> None:
    from datetime import timedelta, timezone

    from leadradar_parser import Document

    job = make_document(
        source_type="jobs",
        source_name="t",
        url="https://a.test/j",
        text="x " * 9_000,
        title="T" * 900,
        max_chars=8_000,
    )
    assert len(job.text) <= 8_000 and job.title is not None and len(job.title) == 500
    local = datetime(2026, 9, 20, 12, tzinfo=timezone(timedelta(hours=2)))
    document = Document.model_validate({**job.model_dump(), "published_at": local, "fetched_at": local})
    assert document.published_at == datetime(2026, 9, 20, 10, tzinfo=UTC)
    assert document.fetched_at.tzinfo is UTC
