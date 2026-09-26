"""Evidence identity across analysis runs.

A re-analysis inserts new signal rows; the evidence key lets the user's verdict on "the same evidence"
(same question, same quote, same document URL) survive the new run. The SQL backfill in migration
2026-09-26_signal_evidence_key mirrors this normalisation.
"""

import hashlib
import re

_WS = re.compile(r"\s+")


def normalize_quote(quote: str | None) -> str:
    return _WS.sub(" ", (quote or "").strip().lower())


def normalize_url(url: str | None) -> str:
    return (url or "").split("#", 1)[0].strip().rstrip("/").lower()


def evidence_key(question_key: str, quote: str | None, url: str | None) -> str:
    raw = f"{question_key}\n{normalize_quote(quote)}\n{normalize_url(url)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
