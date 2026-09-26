"""Entity filter (SPEC §1.7.2 step 2): a snippet from a third-party site counts only if the company's name
or an alias appears in the title or within ±300 characters around it. Own domains, the company's ATS,
registries and incident records pass without the check.

A mention (find_mentions) is the name, an alias, the name without legal suffix or the company domain at word
boundaries, with homonym guards:
- capitalization: a capitalized name must be written capitalized ("Orange" the company, not "orange juice";
  "SAP", not "tree sap");
- other entities with the same word: "Orange County", "Orange Order", "East Orange" — the name followed by a
  place or institution word, or preceded by a compass/place prefix, names something else; so does any phrase
  listed in CompanyProfile.homonyms ("Orange Marketing");
- an acronym name must be written in capitals ("SAP", not "Sap").
The same check verifies the subject of a quote (verification V2).
"""

import re
from urllib.parse import urlsplit

from unidecode import unidecode

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


# The name followed by one of these words is another entity ("Orange County", "Orange Bowl")
HOMONYM_NEXT_WORDS = frozenset(
    """county counties city town township village borough district parish province state beach coast river
    rivers lake park street st avenue ave road boulevard line bowl order lodge church baptist school schools
    college university high public unified district's county's juice soda peel bakery barrel center centre
    arena stadium""".split()
)
# … or preceded by one of these ("East Orange", "West Orange-Stark", "Fort Orange")
HOMONYM_PREV_WORDS = frozenset("east west north south new port fort mount lake little".split())
_WORD_AFTER = re.compile(r"[ \t]+([A-Za-z][A-Za-z']*)")
_WORD_BEFORE = re.compile(r"([A-Za-z]+)[ \t]+$")


def _mention_variants(company: CompanyProfile) -> list[tuple[str, bool]]:
    """(variant, is_domain) — domains are matched case-insensitively and are never homonyms."""
    variants = [(v, False) for v in name_variants(company)]
    for d in [company.domain, *company.own_domains]:
        d = d.strip().lower().removeprefix("www.")
        if d and (d, True) not in variants:
            variants.append((d, True))
    return variants


def find_mentions(text: str, company: CompanyProfile) -> list[tuple[int, int]]:
    """Offsets of genuine mentions of the company in `text` (see the module docstring for the guards)."""
    if not text:
        return []
    names = {v for v, _ in _mention_variants(company)}
    folded_names = {fold(v) for v in names}
    excluded = [
        (m.start(), m.end())
        for h in company.homonyms
        if h.strip()
        for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(h.strip()) + r"(?![A-Za-z0-9])", text, re.I)
    ]
    found: list[tuple[int, int]] = []
    for variant, is_domain in _mention_variants(company):
        alternatives = {re.escape(variant), re.escape(unidecode(variant))}
        pattern = re.compile(
            r"(?<![A-Za-z0-9])(?:" + "|".join(sorted(alternatives)) + r")(?![A-Za-z0-9])", re.IGNORECASE
        )
        for m in pattern.finditer(text):
            if not is_domain:
                if variant[0].isupper() and not m.group(0)[0].isupper():
                    continue
                if len(variant) > 1 and variant.isupper() and not m.group(0).isupper():
                    continue
                after = _WORD_AFTER.match(text, m.end())
                if (
                    after
                    and after.group(1).casefold() in HOMONYM_NEXT_WORDS
                    and fold(f"{m.group(0)} {after.group(1)}") not in folded_names
                ):
                    continue
                before = _WORD_BEFORE.search(text, 0, m.start())
                if (
                    before
                    and before.group(1).casefold() in HOMONYM_PREV_WORDS
                    and before.group(1)[0].isupper()
                ):
                    continue
            if any(es <= m.start() and m.end() <= ee for es, ee in excluded):
                continue
            found.append((m.start(), m.end()))
    return sorted(set(found))


def mentioned_near(
    text: str, start: int, end: int, company: CompanyProfile, window: int = CONTEXT_CHARS
) -> bool:
    """A genuine mention within `window` characters of text[start:end]."""
    return any(ms < end + window and me > start - window for ms, me in find_mentions(text, company))


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
    """`context` = the snippet text plus up to CONTEXT_CHARS of its neighbours in the same document.

    `pattern` is a cheap pre-check (folded text); the decision is made by find_mentions."""
    if is_own_source(snippet, company):
        return True
    pattern = pattern or mention_pattern(company)
    title, folded_title, folded_context = snippet.title or "", fold(snippet.title or ""), fold(context)
    if not (pattern.search(folded_title) or pattern.search(folded_context)):
        return False
    return bool(find_mentions(title, company) or find_mentions(context, company))
