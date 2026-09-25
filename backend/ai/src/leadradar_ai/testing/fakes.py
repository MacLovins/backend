"""In-memory implementations of the ports for tests and the offline CLI.

FakeLLM arrives together with the LLMClient interface (AI-02).
"""

import hashlib
import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID, uuid4

from leadradar_ai.contracts import (
    AnalysisDocument,
    ChunkIn,
    CollectRequest,
    CompanyProfile,
    LeadScore,
    LLMCallRecord,
    ProgressEvent,
    RejectedEvidence,
    ScoreChange,
    Snippet,
    SourceType,
    StoredSignal,
    VerifiedSignal,
)


def _document_date(doc: AnalysisDocument) -> datetime:
    return doc.published_at or doc.fetched_at


class FakeCollector:
    """Returns the given documents that fit the request; records every call."""

    def __init__(self, documents: list[AnalysisDocument], resolved: CompanyProfile | None = None) -> None:
        self.documents = documents
        self.resolved = resolved
        self.resolve_calls: list[CompanyProfile] = []
        self.collect_calls: list[CollectRequest] = []

    async def resolve(self, company: CompanyProfile) -> CompanyProfile:
        self.resolve_calls.append(company)
        return self.resolved or company

    async def collect(self, company: CompanyProfile, request: CollectRequest) -> list[AnalysisDocument]:
        self.collect_calls.append(request)
        return [
            d
            for d in self.documents
            if d.source_type in request.source_types and _document_date(d) >= request.since
        ]


class InMemoryStore:
    """AnalysisStore over dicts. Seed documents with `add_documents`."""

    def __init__(self) -> None:
        self.documents: dict[UUID, list[AnalysisDocument]] = defaultdict(list)
        self.chunks: dict[UUID, list[ChunkIn]] = defaultdict(list)  # company_id → chunks
        self.fingerprints: dict[tuple[UUID, UUID], str] = {}
        self.signals: dict[tuple[UUID, UUID], list[StoredSignal]] = defaultdict(list)
        self.superseded: dict[tuple[UUID, UUID], list[StoredSignal]] = defaultdict(list)
        self.rejected: dict[tuple[UUID, UUID], list[RejectedEvidence]] = defaultdict(list)
        self.scores: dict[tuple[UUID, UUID], LeadScore] = {}
        self.score_history: list[LeadScore] = []
        self.extraction_runs: list[UUID] = []

    def add_documents(self, company_id: UUID, documents: list[AnalysisDocument]) -> None:
        self.documents[company_id].extend(documents)

    def _document(self, company_id: UUID, document_id: UUID) -> AnalysisDocument:
        return next(d for d in self.documents[company_id] if d.id == document_id)

    async def documents_without_chunks(self, company_id: UUID) -> list[AnalysisDocument]:
        chunked = {c.document_id for c in self.chunks[company_id]}
        return [d for d in self.documents[company_id] if d.id not in chunked]

    async def save_chunks(self, chunks: list[ChunkIn]) -> None:
        for chunk in chunks:
            self.chunks[chunk.company_id].append(chunk)

    async def load_snippets(
        self, company_id: UUID, since: datetime, source_types: set[SourceType]
    ) -> list[Snippet]:
        snippets = []
        for chunk in self.chunks[company_id]:
            doc = self._document(company_id, chunk.document_id)
            if doc.source_type not in source_types or _document_date(doc) < since:
                continue
            snippets.append(
                Snippet(
                    id=f"S{len(snippets) + 1}",
                    chunk_id=chunk.id,
                    document_id=doc.id,
                    text=chunk.text,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    source_type=doc.source_type,
                    source_name=doc.source_name,
                    url=doc.url,
                    title=doc.title,
                    published_at=doc.published_at,
                    fetched_at=doc.fetched_at,
                    language=doc.language,
                    meta=doc.meta,
                    embedding=chunk.embedding,
                )
            )
        return snippets

    async def get_fingerprint(self, company_id: UUID, service_id: UUID) -> str | None:
        return self.fingerprints.get((company_id, service_id))

    async def save_extraction(
        self,
        run_id: UUID,
        company_id: UUID,
        service_id: UUID,
        fingerprint: str,
        signals: list[VerifiedSignal],
        rejected: list[RejectedEvidence],
    ) -> None:
        key = (company_id, service_id)
        now = datetime.now(UTC)
        self.superseded[key].extend(self.signals[key])
        self.signals[key] = [
            StoredSignal(
                **s.model_dump(include=set(VerifiedSignal.model_fields)), id=uuid4(), detected_at=now
            )
            for s in signals
        ]
        self.rejected[key].extend(rejected)
        self.fingerprints[key] = fingerprint
        self.extraction_runs.append(run_id)

    async def load_signals(self, company_id: UUID, service_id: UUID) -> list[StoredSignal]:
        return [s for s in self.signals[(company_id, service_id)] if s.status == "active"]

    async def save_score(self, run_id: UUID | None, score: LeadScore) -> ScoreChange:
        key = (score.company_id, score.service_id)
        previous = self.scores.get(key)
        self.scores[key] = score
        self.score_history.append(score)
        return ScoreChange(
            company_id=score.company_id,
            service_id=score.service_id,
            priority_before=previous.priority if previous else None,
            priority_after=score.priority,
            tier_before=previous.tier if previous else None,
            tier_after=score.tier,
        )


class ListProgressSink:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    async def emit(self, event: ProgressEvent) -> None:
        self.events.append(event)

    def stages(self) -> list[tuple[str, str]]:
        return [(e.stage, e.status) for e in self.events]


class InMemoryLLMCache:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[dict, dict]] = {}

    async def get(self, key: str) -> dict | None:
        entry = self.entries.get(key)
        return entry[0] if entry else None

    async def set(self, key: str, value: dict, meta: dict) -> None:
        self.entries[key] = (value, meta)


class InMemoryUsageSink:
    """Counts real calls (cache hits excluded); `preset` simulates calls made earlier today."""

    def __init__(self, preset: dict[str, int] | None = None) -> None:
        self.calls: list[LLMCallRecord] = []
        self.preset = preset or {}

    async def record(self, call: LLMCallRecord) -> None:
        self.calls.append(call)

    async def used_today(self, model: str) -> int:
        made = sum(1 for c in self.calls if c.model == model and not c.cache_hit)
        return self.preset.get(model, 0) + made


_TOKEN = re.compile(r"\w+", re.UNICODE)


class FakeEmbedder:
    """Deterministic hashed bag-of-words vectors: shared words → higher cosine. No model download."""

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN.findall(text.casefold()):
            if token in ("query", "passage"):  # e5 prefixes carry no meaning
                continue
            h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)
