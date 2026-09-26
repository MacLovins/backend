"""Ports of leadradar-ai (SPEC §1.4.3).

Implemented by core in `leadradar_core/adapters/`; fakes for tests live in `leadradar_ai.testing`.
A port changes only with agreement of P1 and P3 (ARCHITECTURE §4.7).
"""

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

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

__all__ = [
    "AnalysisStore",
    "CollectRequest",
    "Collector",
    "Embedder",
    "LLMCache",
    "ProgressSink",
    "UsageSink",
]


@runtime_checkable
class Collector(Protocol):
    async def resolve(self, company: CompanyProfile) -> CompanyProfile:
        """Fill domain, own_domains, careers/ATS and firmographics if missing."""
        ...

    async def collect(self, company: CompanyProfile, request: CollectRequest) -> list[AnalysisDocument]:
        """core: parser.collect → save with dedup → return the window's documents (new and earlier)."""
        ...


@runtime_checkable
class AnalysisStore(Protocol):
    async def documents_without_chunks(self, company_id: UUID) -> list[AnalysisDocument]: ...

    async def save_chunks(self, chunks: list[ChunkIn]) -> None: ...

    async def load_snippets(
        self, company_id: UUID, since: datetime, source_types: set[SourceType]
    ) -> list[Snippet]: ...

    async def get_fingerprint(self, company_id: UUID, service_id: UUID) -> str | None: ...

    async def save_extraction(
        self,
        run_id: UUID,
        company_id: UUID,
        service_id: UUID,
        fingerprint: str,
        signals: list[VerifiedSignal],
        rejected: list[RejectedEvidence],
    ) -> None:
        """Previous active signals of the service become superseded; fingerprint is stored atomically."""
        ...

    async def load_signals(self, company_id: UUID, service_id: UUID) -> list[StoredSignal]:
        """Active signals only (rejected_by_user and superseded are excluded)."""
        ...

    async def sync_derived(
        self, company_id: UUID, service_id: UUID, signals: list[StoredSignal]
    ) -> list[StoredSignal]:
        """Persist the currently derivable NIS2/DORA signals (deterministic ids): insert missing ones, retire
        stored derived ones not in the list. Returns them as stored — with the status a user may have set."""
        ...

    async def save_score(self, run_id: UUID | None, score: LeadScore) -> ScoreChange: ...


@runtime_checkable
class ProgressSink(Protocol):
    async def emit(self, event: ProgressEvent) -> None: ...


@runtime_checkable
class LLMCache(Protocol):
    async def get(self, key: str) -> dict | None: ...

    async def set(self, key: str, value: dict, meta: dict) -> None: ...


@runtime_checkable
class UsageSink(Protocol):
    async def record(self, call: LLMCallRecord) -> None: ...

    async def used_today(self, model: str) -> int:
        """Calls to `model` today that consume quota (status not cache_hit / rate_limited).

        The day is in Pacific time, like Gemini quotas.
        """
        ...


@runtime_checkable
class Embedder(Protocol):
    def embed_passages(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...
