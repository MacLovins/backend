"""In-memory implementations of the ports and of the LLM for tests and the offline CLI."""

import hashlib
import math
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

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
from leadradar_ai.llm.types import LLMRequest, LLMResult, Thinking, TransportError, TransportResponse


def _document_date(doc: AnalysisDocument) -> datetime:
    return doc.published_at or doc.fetched_at


class FakeCollector:
    """Returns the given documents that fit the request; records every call.

    With `store`, collected documents are also saved there (dedup by id), like core's ParserCollector.
    `fail_resolve` / `fail_collect` raise on every call.
    """

    def __init__(
        self,
        documents: list[AnalysisDocument],
        resolved: CompanyProfile | None = None,
        store: "InMemoryStore | None" = None,
        fail_resolve: Exception | None = None,
        fail_collect: Exception | None = None,
    ) -> None:
        self.documents = documents
        self.resolved = resolved
        self.store = store
        self.fail_resolve = fail_resolve
        self.fail_collect = fail_collect
        self.resolve_calls: list[CompanyProfile] = []
        self.collect_calls: list[CollectRequest] = []

    async def resolve(self, company: CompanyProfile) -> CompanyProfile:
        self.resolve_calls.append(company)
        if self.fail_resolve:
            raise self.fail_resolve
        return self.resolved or company

    async def collect(self, company: CompanyProfile, request: CollectRequest) -> list[AnalysisDocument]:
        self.collect_calls.append(request)
        if self.fail_collect:
            raise self.fail_collect
        docs = [
            d
            for d in self.documents
            if d.source_type in request.source_types and _document_date(d) >= request.since
        ]
        if self.store is not None:
            known = {d.id for d in self.store.documents[company.id]}
            self.store.add_documents(company.id, [d for d in docs if d.id not in known])
        return docs


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
    """Counts calls that consume quota (not cache hits, not 429s); `preset` = calls made earlier today."""

    def __init__(self, preset: dict[str, int] | None = None) -> None:
        self.calls: list[LLMCallRecord] = []
        self.preset = preset or {}

    async def record(self, call: LLMCallRecord) -> None:
        self.calls.append(call)

    async def used_today(self, model: str) -> int:
        made = sum(
            1 for c in self.calls if c.model == model and c.status not in ("cache_hit", "rate_limited")
        )
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


# --- LLM ----------------------------------------------------------------------------------------

BLOCKED = object()  # FakeLLM response: finish_reason SAFETY → LLMResult(output=None, blocked=True)

FakeResponse = BaseModel | dict | Exception | object
FakeHandler = Callable[[LLMRequest], FakeResponse]


class FakeLLM:
    """LLMClient for tests: answers from a handler or a queue; counts calls by purpose.

    A response may be a model or dict (validated into request.output_model), an exception to raise,
    or BLOCKED.
    """

    def __init__(
        self, responses: FakeHandler | list[FakeResponse] | None = None, model: str = "fake-model"
    ) -> None:
        self._handler = responses if callable(responses) else None
        self._queue = list(responses) if isinstance(responses, list) else []
        self.model = model
        self.calls: list[LLMRequest] = []

    def calls_for(self, purpose: str) -> list[LLMRequest]:
        return [c for c in self.calls if c.purpose == purpose]

    async def generate[T: BaseModel](self, request: LLMRequest[T]) -> LLMResult[T]:
        self.calls.append(request)
        if self._handler is not None:
            response = self._handler(request)
        elif self._queue:
            response = self._queue.pop(0)
        else:
            raise AssertionError(f"FakeLLM: no response queued for {request.purpose}")
        if isinstance(response, Exception):
            raise response
        if response is BLOCKED:
            return LLMResult(output=None, model=self.model, blocked=True)
        data = response.model_dump() if isinstance(response, BaseModel) else response
        return LLMResult(output=request.output_model.model_validate(data), model=self.model)


@dataclass
class TransportCall:
    model: str
    system: str
    contents: str
    thinking: Thinking


class FakeTransport:
    """Transport for GeminiClient tests: scripted per model; a str is the response text (finish STOP)."""

    def __init__(self, script: dict[str, list[str | TransportResponse | TransportError]]) -> None:
        self.script = {model: list(items) for model, items in script.items()}
        self.calls: list[TransportCall] = []

    async def generate(
        self, *, model: str, system: str, contents: str, output_model: type[BaseModel], thinking: Thinking
    ) -> TransportResponse:
        self.calls.append(TransportCall(model, system, contents, thinking))
        queue = self.script.get(model)
        if not queue:
            raise AssertionError(f"FakeTransport: unexpected call to {model}")
        item: Any = queue.pop(0)
        if isinstance(item, TransportError):
            raise item
        if isinstance(item, str):
            return TransportResponse(text=item, finish_reason="STOP", input_tokens=100, output_tokens=20)
        return item

    def models_called(self) -> list[str]:
        return [c.model for c in self.calls]
