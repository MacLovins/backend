"""V5 — syndication: one story reprinted by ten outlets is one piece of evidence, not ten.

Two signals of the same question are duplicates when their quotes (or the headlines they came from) are
near-identical after normalization. noisy-OR multiplies independent evidence, so only the most valuable copy
of each cluster may enter it; the copy count is kept as corroboration.
"""

from collections.abc import Callable, Sequence

from rapidfuzz import fuzz

from leadradar_ai.contracts import VerifiedSignal
from leadradar_ai.retrieval.text import fold

SIMILARITY = 80  # token_sort_ratio on folded quotes: reprints differ in a word or two, not in substance
MIN_TOKENS = 4  # shorter quotes ("AI strategy") are too generic to call two sources the same story


def _norm(text: str) -> str:
    return " ".join(fold(text).split())


def same_story(a: VerifiedSignal, b: VerifiedSignal) -> bool:
    if a.question_id != b.question_id:
        return False
    if a.document_id == b.document_id:
        return True
    qa, qb = _norm(a.quote), _norm(b.quote)
    if min(len(qa.split()), len(qb.split())) < MIN_TOKENS:
        return qa == qb
    return fuzz.token_sort_ratio(qa, qb) >= SIMILARITY


def collapse_syndicated[S: VerifiedSignal](
    signals: Sequence[S], value: Callable[[S], float]
) -> list[tuple[S, int]]:
    """Clusters of the same story → (the most valuable copy, cluster size), most valuable first."""
    clusters: list[list[S]] = []
    for s in sorted(signals, key=value, reverse=True):
        for cluster in clusters:
            if same_story(cluster[0], s):
                cluster.append(s)
                break
        else:
            clusters.append([s])
    return [(c[0], len(c)) for c in clusters]
