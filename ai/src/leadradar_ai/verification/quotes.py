"""V1 — find a quote in the snippet (SPEC §1.7.4), the way LangExtract aligns extractions to the source.

Both sides are normalized (NFKC, casefold, unified quotes/dashes/ellipses, collapsed whitespace) while keeping
a map back to original character offsets. Exact match first; otherwise rapidfuzz partial alignment ≥ 90
(flag fuzzy_quote), and a fuzzy match must contain every number of the quote.
"""

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

FUZZY_THRESHOLD = 90.0
MIN_QUOTE_CHARS = 12  # "AI", "RPA" alone prove nothing
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

_CHAR_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "`": "'",
        "´": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "«": '"',
        "»": '"',
        "‹": "'",
        "›": "'",
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        "\u00a0": " ",
        "\u202f": " ",
        "\u2009": " ",
    }
)


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normalized text and, for each of its characters, the index in `text` it came from."""
    out: list[str] = []
    index: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        piece = unicodedata.normalize("NFKC", ch).translate(_CHAR_MAP).casefold()
        if piece == "…":
            piece = "..."
        for c in piece:
            if c.isspace():
                if prev_space:
                    continue
                c = " "
                prev_space = True
            else:
                prev_space = False
            out.append(c)
            index.append(i)
    while out and out[-1] == " ":
        out.pop()
        index.pop()
    return "".join(out), index


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


@dataclass(frozen=True)
class QuoteMatch:
    start: int  # offsets in the searched text (original, not normalized)
    end: int
    fuzzy: bool
    score: float


def _strip_ellipsis(quote: str) -> str:
    return quote.strip().strip(".").strip() if quote.strip().endswith("...") else quote


def find_quote(quote: str, text: str) -> QuoteMatch | None:
    q = normalize(_strip_ellipsis(quote)).strip(" \"'")
    if len(q) < MIN_QUOTE_CHARS or not text:
        return None
    t, index = normalize_with_map(text)
    pos = t.find(q)
    if pos >= 0:
        return QuoteMatch(index[pos], index[pos + len(q) - 1] + 1, fuzzy=False, score=100.0)
    alignment = fuzz.partial_ratio_alignment(q, t, score_cutoff=FUZZY_THRESHOLD)
    if alignment is None or alignment.dest_end <= alignment.dest_start:
        return None
    matched = t[alignment.dest_start : alignment.dest_end]
    if set(_NUMBER.findall(q)) - set(_NUMBER.findall(matched)):
        return None  # "4,000 jobs" must not verify against "3,000 jobs"
    return QuoteMatch(
        index[alignment.dest_start], index[alignment.dest_end - 1] + 1, fuzzy=True, score=alignment.score
    )
