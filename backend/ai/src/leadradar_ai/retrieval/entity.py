"""Entity filter (SPEC §1.7.2 step 2): a snippet from a third-party site counts only if the company's name
or an alias appears in the title or within ±300 characters around it. Own domains, the company's ATS,
registries and incident records pass without the check.
"""

import re
from urllib.parse import urlsplit

from leadradar_ai.contracts import CompanyProfile, Snippet
from leadradar_ai.retrieval.text import fold

CONTEXT_CHARS = 300
_LEGAL_SUFFIXES = re.compile(
    r"\s+(group|holding|holdings|ag|se|sa|s\.a\.|nv|n\.v\.|plc|gmbh|inc|inc\.|ltd|ltd\.|llc|corp|corporation|"
    r"company|co\.|spa|s\.p\.a\.|oyj|asa|ab)$",
    re.IGNORECASE,
)
_TRUSTED_SOURCE_TYPES = {"registry", "incident", "derived", "manual"}
_AGGREGATOR_JOB_SOURCES = {"adzuna"}


def name_variants(company: CompanyProfile) -> list[str]:
    """Name, aliases and the name without legal suffixes ("Deutsche Lufthansa AG" → "Deutsche Lufthansa")."""
    variants = []
    for name in [company.name, *company.aliases]:
        for v in (name, _LEGAL_SUFFIXES.sub("", name.strip())):
            v = v.strip()
            if len(v) >= 2 and v not in variants:
                variants.append(v)
    return variants


def mention_pattern(company: CompanyProfile) -> re.Pattern[str]:
    alternatives = sorted({re.escape(fold(v)) for v in name_variants(company)}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alternatives) + r")(?![a-z0-9])")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def is_own_source(snippet: Snippet, company: CompanyProfile) -> bool:
    if snippet.source_type in _TRUSTED_SOURCE_TYPES:
        return True
    if snippet.source_type == "jobs" and snippet.source_name not in _AGGREGATOR_JOB_SOURCES:
        return True  # collected from the company's own ATS board
    host = _host(snippet.url)
    own = {d.lower().removeprefix("www.") for d in [company.domain, *company.own_domains]}
    return any(host == d or host.endswith("." + d) for d in own)


def passes_entity_filter(
    snippet: Snippet, context: str, company: CompanyProfile, pattern: re.Pattern[str] | None = None
) -> bool:
    """`context` = the snippet text plus up to CONTEXT_CHARS of its neighbours in the same document."""
    if is_own_source(snippet, company):
        return True
    pattern = pattern or mention_pattern(company)
    return bool(pattern.search(fold(snippet.title or "")) or pattern.search(fold(context)))
