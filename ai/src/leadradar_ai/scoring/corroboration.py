"""V5 — multi-source confirmation (SPEC §1.7.4, AI-15). Pure code, no LLM, no embeddings.

Reprints of one news story are one event, not independent evidence. Signals of the same question are
clustered when their quotes or summaries are near-duplicates (normalized token similarity) and their dates
are within ±14 days (an undated signal matches on text alone). Numbers must not contradict: the numbers of
one text must all appear in the other ("4,000 jobs" is not "300 jobs").

A cluster counts once in noisy-OR. Its value uses the best member (strength, source, date) with the
cluster confidence: independent sources (different URLs) boost it as 1 − Π(1 − cᵢ), capped at 0.98, never
below the best member's own confidence. A cluster confirmed by ≥ 2 independent sources is "corroborated".
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from rapidfuzz import fuzz
from unidecode import unidecode

from leadradar_ai.contracts import ScoringProfile, VerifiedSignal
from leadradar_ai.scoring.decay import evidence_date, evidence_value

SIMILARITY_THRESHOLD = 82.0  # token_sort_ratio of normalized quote or summary
SUBSET_THRESHOLD = 95.0  # token_set_ratio: one text contains the other (headline vs lead sentence)
SUBSET_MIN_TOKENS = 5  # … only for texts long enough that containment is not a coincidence
DATE_WINDOW_DAYS = 14
MAX_CLUSTER_CONFIDENCE = 0.98

_NON_WORD = re.compile(r"[^a-z0-9]+")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _norm(text: str) -> str:
    return _NON_WORD.sub(" ", unidecode(text).casefold()).strip()


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").replace(".", "") for n in _NUMBER.findall(text)}


def _similar(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if fuzz.token_sort_ratio(na, nb) >= SIMILARITY_THRESHOLD:
        return True
    short = min(len(na.split()), len(nb.split()))
    return short >= SUBSET_MIN_TOKENS and fuzz.token_set_ratio(na, nb) >= SUBSET_THRESHOLD


def source_key(signal: VerifiedSignal) -> str:
    """Identity of a source: the URL without query and fragment (one article collected twice is one source)."""
    parts = urlsplit(signal.url.strip().lower())
    if parts.netloc:
        return f"{parts.netloc.removeprefix('www.')}{parts.path.rstrip('/')}"
    return str(signal.document_id)


def same_event(a: VerifiedSignal, b: VerifiedSignal) -> bool:
    if a.question_id != b.question_id:
        return False
    if source_key(a) == source_key(b) and a.document_id == b.document_id and a.quote == b.quote:
        return True
    da, db = evidence_date(a), evidence_date(b)
    if da is not None and db is not None and abs((da - db).days) > DATE_WINDOW_DAYS:
        return False
    na, nb = _numbers(a.quote) | _numbers(a.summary), _numbers(b.quote) | _numbers(b.summary)
    if na and nb and not (na <= nb or nb <= na):  # every number of one text must appear in the other
        return False
    return _similar(a.quote, b.quote) or _similar(a.summary, b.summary)


@dataclass
class Cluster[S: VerifiedSignal]:
    members: list[S]  # best first
    confidence: float  # combined over independent sources
    sources: int  # independent sources

    @property
    def lead(self) -> S:
        return self.members[0]

    @property
    def corroborated(self) -> bool:
        return self.sources >= 2


def cluster_confidence(members: Sequence[VerifiedSignal]) -> tuple[float, int]:
    best_per_source: dict[str, float] = {}
    for s in members:
        key = source_key(s)
        best_per_source[key] = max(best_per_source.get(key, 0.0), s.confidence)
    top = max(best_per_source.values())
    if len(best_per_source) < 2:
        return top, len(best_per_source)
    product = 1.0
    for c in best_per_source.values():
        product *= 1.0 - c
    return max(top, min(MAX_CLUSTER_CONFIDENCE, 1.0 - product)), len(best_per_source)


def cluster_signals[S: VerifiedSignal](
    signals: Sequence[S], profile: ScoringProfile, now: datetime
) -> list[Cluster[S]]:
    """Clusters of one event (single linkage), sorted by cluster value, best first."""
    parent = list(range(len(signals)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(signals)):
        for j in range(i + 1, len(signals)):
            if find(i) != find(j) and same_event(signals[i], signals[j]):
                parent[find(j)] = find(i)

    groups: dict[int, list[S]] = {}
    for i, s in enumerate(signals):
        groups.setdefault(find(i), []).append(s)
    clusters = []
    for members in groups.values():
        members.sort(key=lambda s: evidence_value(s, profile, now), reverse=True)
        confidence, sources = cluster_confidence(members)
        clusters.append(Cluster(members=members, confidence=confidence, sources=sources))
    clusters.sort(key=lambda c: cluster_value(c, profile, now), reverse=True)
    return clusters


def cluster_value(cluster: Cluster, profile: ScoringProfile, now: datetime) -> float:
    return evidence_value(cluster.lead, profile, now, confidence=cluster.confidence)
