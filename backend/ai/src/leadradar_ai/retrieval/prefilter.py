"""Prefilter (SPEC §1.7.2): the minimum set of high-signal snippets per service.

Per question: source + recency filter → entity filter → BM25 and cosine ranks → RRF(k=60) → top-k with
≤ 2 snippets per document. Per service: union ≤ 40 snippets and ≤ 25k tokens; the lowest-RRF snippets are
dropped first, but every question keeps its best snippet and high-weight questions keep two.
A question without candidates is answered "no" without the LLM. The fingerprint lets `extract` skip
the LLM when nothing changed.
"""

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from pydantic import BaseModel

from leadradar_ai.contracts import CompanyProfile, QuestionConfig, ServiceBundle, Snippet, SourceType
from leadradar_ai.llm.cache_key import estimate_tokens
from leadradar_ai.ports import Embedder
from leadradar_ai.retrieval.bm25 import BM25
from leadradar_ai.retrieval.chunking import index_text
from leadradar_ai.retrieval.entity import CONTEXT_CHARS, mention_pattern, passes_entity_filter
from leadradar_ai.retrieval.text import fold
from leadradar_ai.settings import AISettings


@dataclass(frozen=True)
class PrefilterConfig:
    topk_per_question: int = 5
    max_snippets: int = 40
    max_tokens: int = 25_000
    min_cosine: float = 0.78
    per_list: int = 20  # top-N of each ranked list that goes into RRF
    rrf_k: int = 60
    max_per_document: int = 2
    protected_high: int = 2  # high-weight questions keep this many snippets under budget pressure
    protected_other: int = 1

    @classmethod
    def from_settings(cls, s: AISettings) -> "PrefilterConfig":
        return cls(
            topk_per_question=s.topk_per_question,
            max_snippets=s.max_snippets_per_service,
            max_tokens=s.max_tokens_per_service,
            min_cosine=s.min_cosine,
        )


class PrefilterResult(BaseModel):
    snippets: list[Snippet]  # union for the prompt: ids relabelled S1..Sn, embeddings dropped
    candidates: dict[str, list[str]]  # str(question.id) → snippet ids, best first
    no_candidates: list[str]  # str(question.id) answered "no" without the LLM
    fingerprint: str
    stats: dict[str, int]

    @property
    def needs_llm(self) -> bool:
        return bool(self.snippets)


def load_window(bundle: ServiceBundle, now: datetime) -> tuple[datetime, set[SourceType]]:
    """Arguments for AnalysisStore.load_snippets: the longest window and all sources of the service."""
    days = max((q.recency_days for q in bundle.questions), default=0)
    sources: set[SourceType] = (
        set().union(*(q.source_types for q in bundle.questions)) if bundle.questions else set()
    )
    return now - timedelta(days=days), sources


def compute_fingerprint(questions: list[QuestionConfig], chunk_ids: list[str], prompt_version: str) -> str:
    q_part = ",".join(sorted(f"{q.id}@{q.version}" for q in questions))
    payload = f"{q_part}|{','.join(sorted(chunk_ids))}|{prompt_version}"
    return hashlib.sha256(payload.encode()).hexdigest()


def question_query(q: QuestionConfig) -> str:
    terms = [kw for words in q.keywords.values() for kw in words] + q.job_titles
    return " ".join([q.text, *dict.fromkeys(terms)])


def _entity_contexts(snippets: list[Snippet]) -> list[str]:
    """Snippet text plus the neighbouring text of the same document within ±CONTEXT_CHARS."""
    by_doc: dict = defaultdict(list)
    for i, s in enumerate(snippets):
        by_doc[s.document_id].append(i)
    contexts = [""] * len(snippets)
    for idxs in by_doc.values():
        for i in idxs:
            s = snippets[i]
            near = [
                snippets[j].text
                for j in idxs
                if snippets[j].char_end >= s.char_start - CONTEXT_CHARS
                and snippets[j].char_start <= s.char_end + CONTEXT_CHARS
            ]
            contexts[i] = "\n".join(near)
    return contexts


def _date(s: Snippet) -> datetime:
    return s.published_at or s.fetched_at


def _negative_pattern(q: QuestionConfig) -> re.Pattern[str] | None:
    terms = [re.escape(fold(t)) for t in q.negative_terms if t.strip()]
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(terms) + r")(?![a-z0-9])") if terms else None


def prefilter(
    company: CompanyProfile,
    bundle: ServiceBundle,
    snippets: list[Snippet],
    embedder: Embedder,
    now: datetime,
    prompt_version: str,
    config: PrefilterConfig | None = None,
) -> PrefilterResult:
    config = config or PrefilterConfig()
    pattern = mention_pattern(company)
    contexts = _entity_contexts(snippets)
    passing = [
        s for s, ctx in zip(snippets, contexts, strict=True) if passes_entity_filter(s, ctx, company, pattern)
    ]
    texts = [index_text(s.text, s.title, s.source_type) for s in passing]
    bm25 = BM25(texts)
    has_vec = [s.embedding is not None and len(s.embedding) > 0 for s in passing]
    matrix = np.array([s.embedding for s, ok in zip(passing, has_vec, strict=True) if ok], dtype=np.float32)
    vec_row = {i: r for r, i in enumerate(i for i, ok in enumerate(has_vec) if ok)}
    if len(matrix):
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True).clip(min=1e-12)

    candidates: dict[str, list[int]] = {}
    best_rrf: dict[int, float] = defaultdict(float)
    for q in bundle.questions:
        since = now - timedelta(days=q.recency_days)
        negative = _negative_pattern(q)
        pool = [
            i
            for i, s in enumerate(passing)
            if s.source_type in q.source_types
            and _date(s) >= since
            and not (negative and negative.search(fold(texts[i])))
        ]
        if not pool:
            continue
        query = question_query(q)
        all_bm25 = bm25.scores(query)
        bm = {i: all_bm25[i] for i in pool}
        cos: dict[int, float] = {}
        if len(matrix):
            qv = np.asarray(embedder.embed_query(query), dtype=np.float32)
            qv /= max(float(np.linalg.norm(qv)), 1e-12)
            sims = matrix @ qv
            cos = {i: float(sims[vec_row[i]]) for i in pool if i in vec_row}

        bm_rank = sorted((i for i in pool if bm[i] > 0), key=lambda i: -bm[i])[: config.per_list]
        vec_rank = sorted(cos, key=lambda i: -cos[i])[: config.per_list]
        rrf: dict[int, float] = defaultdict(float)
        for ranked in (bm_rank, vec_rank):
            for rank, i in enumerate(ranked, start=1):
                rrf[i] += 1.0 / (config.rrf_k + rank)
        kept = [i for i in rrf if bm[i] > 0 or cos.get(i, 0.0) >= config.min_cosine]
        kept.sort(key=lambda i: (-rrf[i], i))

        chosen: list[int] = []
        per_doc: dict = defaultdict(int)
        for i in kept:
            doc = passing[i].document_id
            if per_doc[doc] >= config.max_per_document:
                continue
            per_doc[doc] += 1
            chosen.append(i)
            best_rrf[i] = max(best_rrf[i], rrf[i])
            if len(chosen) == config.topk_per_question:
                break
        if chosen:
            candidates[str(q.id)] = chosen

    before_budget = len({i for c in candidates.values() for i in c})
    candidates = _apply_budget(candidates, best_rrf, passing, bundle, config)

    union = sorted(
        {i for c in candidates.values() for i in c},
        key=lambda i: (passing[i].source_type, str(passing[i].document_id), passing[i].char_start),
    )
    label = {i: f"S{n}" for n, i in enumerate(union, start=1)}
    prompt_snippets = [passing[i].model_copy(update={"id": label[i], "embedding": None}) for i in union]
    return PrefilterResult(
        snippets=prompt_snippets,
        candidates={qid: [label[i] for i in idxs] for qid, idxs in candidates.items()},
        no_candidates=[str(q.id) for q in bundle.questions if str(q.id) not in candidates],
        fingerprint=compute_fingerprint(
            bundle.questions, [str(passing[i].chunk_id) for i in union], prompt_version
        ),
        stats={
            "loaded": len(snippets),
            "after_entity_filter": len(passing),
            "candidates_before_budget": before_budget,
            "selected": len(union),
            "estimated_tokens": estimate_tokens(*(s.text for s in prompt_snippets)),
        },
    )


def _apply_budget(
    candidates: dict[str, list[int]],
    best_rrf: dict[int, float],
    passing: list[Snippet],
    bundle: ServiceBundle,
    config: PrefilterConfig,
) -> dict[str, list[int]]:
    union = {i for c in candidates.values() for i in c}

    def over(selected: set[int]) -> bool:
        tokens = estimate_tokens(*(passing[i].text for i in selected))
        return len(selected) > config.max_snippets or tokens > config.max_tokens

    if not over(union):
        return candidates
    weights = {str(q.id): q.weight for q in bundle.questions}
    protected = {
        i
        for qid, idxs in candidates.items()
        for i in idxs[: config.protected_high if weights[qid] == "high" else config.protected_other]
    }
    for i in sorted(union - protected, key=lambda i: (best_rrf[i], -i)):
        if not over(union):
            break
        union.discard(i)
    return {qid: [i for i in idxs if i in union] for qid, idxs in candidates.items()}
