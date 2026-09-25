"""Okapi BM25 over a small corpus (≤ a few thousand snippets per company) — no dependency needed."""

import math
from collections import Counter

from leadradar_ai.retrieval.text import tokenize


class BM25:
    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self._tf = [Counter(tokenize(d)) for d in documents]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(self._tf)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = set(tokenize(query))
        out = []
        for tf, length in zip(self._tf, self._len, strict=True):
            score = 0.0
            norm = self.k1 * (1 - self.b + self.b * length / self._avg) if self._avg else self.k1
            for t in terms:
                f = tf.get(t)
                if f:
                    score += self._idf[t] * f * (self.k1 + 1) / (f + norm)
            out.append(score)
        return out
