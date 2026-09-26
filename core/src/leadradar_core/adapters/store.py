"""SqlAnalysisStore — the AnalysisStore port of leadradar-ai over the core schema.

Every method runs in its own short transaction: the graph calls it from the worker, outside any request.
"""

from datetime import datetime
from uuid import UUID

import leadradar_ai as ai
from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from leadradar_core.adapters import mapping
from leadradar_core.modules.activity import events as domain_events
from leadradar_core.modules.intelligence.models import (
    Document,
    DocumentChunk,
    ExtractionState,
    RejectedEvidence,
    Signal,
)
from leadradar_core.modules.leads.models import LeadScore


class SqlAnalysisStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], org_id: UUID) -> None:
        self._sessions = session_factory
        self._org_id = org_id

    async def documents_without_chunks(self, company_id: UUID) -> list[ai.AnalysisDocument]:
        has_chunks = exists().where(DocumentChunk.document_id == Document.id)
        stmt = select(Document).where(Document.company_id == company_id, ~has_chunks)
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [mapping.analysis_document(r) for r in rows]

    async def save_chunks(self, chunks: list[ai.ChunkIn]) -> None:
        if not chunks:
            return
        async with self._sessions() as session, session.begin():
            session.add_all(
                DocumentChunk(
                    id=c.id,
                    org_id=self._org_id,
                    document_id=c.document_id,
                    company_id=c.company_id,
                    ord=c.ord,
                    text=c.text,
                    char_start=c.char_start,
                    char_end=c.char_end,
                    embedding=c.embedding or None,
                )
                for c in chunks
            )

    async def load_snippets(
        self, company_id: UUID, since: datetime, source_types: set[str]
    ) -> list[ai.Snippet]:
        stmt = (
            select(DocumentChunk, Document)
            .join(Document, DocumentChunk.document_id == Document.id)
            .where(
                DocumentChunk.company_id == company_id,
                Document.source_type.in_(source_types),
                func.coalesce(Document.published_at, Document.fetched_at) >= since,
            )
            .order_by(Document.id, DocumentChunk.ord)
        )
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).all()
        snippets = []
        for n, (chunk, doc) in enumerate(rows, start=1):
            source = mapping.analysis_document(doc)
            snippets.append(
                ai.Snippet(
                    id=f"S{n}",
                    chunk_id=chunk.id,
                    document_id=doc.id,
                    text=chunk.text,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    source_type=source.source_type,
                    source_name=doc.source_name,
                    url=doc.url,
                    title=doc.title,
                    published_at=doc.published_at,
                    fetched_at=source.fetched_at,
                    language=doc.language,
                    meta=source.meta,
                    embedding=[float(x) for x in chunk.embedding] if chunk.embedding is not None else None,
                )
            )
        return snippets

    async def get_fingerprint(self, company_id: UUID, service_id: UUID) -> str | None:
        stmt = select(ExtractionState.fingerprint).where(
            ExtractionState.company_id == company_id, ExtractionState.service_id == service_id
        )
        async with self._sessions() as session:
            return (await session.execute(stmt)).scalar_one_or_none()

    async def save_extraction(
        self,
        run_id: UUID,
        company_id: UUID,
        service_id: UUID,
        fingerprint: str,
        signals: list[ai.VerifiedSignal],
        rejected: list[ai.RejectedEvidence],
    ) -> None:
        """Previous active signals become superseded (history is kept); fingerprint saved in the same transaction."""
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(Signal)
                .where(
                    Signal.company_id == company_id,
                    Signal.service_id == service_id,
                    Signal.status == "active",
                )
                .values(status="superseded")
            )
            rows = [mapping.signal_row(s, company_id, service_id, self._org_id, run_id) for s in signals]
            session.add_all(rows)
            await domain_events.signals_detected(session, rows)
            session.add_all(
                RejectedEvidence(
                    org_id=self._org_id,
                    company_id=company_id,
                    service_id=service_id,
                    question_id=r.question_id,
                    run_id=run_id,
                    quote=r.quote,
                    reason=r.reason,
                )
                for r in rejected
            )
            await session.execute(
                insert(ExtractionState)
                .values(
                    org_id=self._org_id,
                    company_id=company_id,
                    service_id=service_id,
                    fingerprint=fingerprint,
                    prompt_version=ai.EXTRACTION_PROMPT_VERSION,
                    extracted_at=func.now(),
                )
                .on_conflict_do_update(
                    constraint="uq_extraction_state_company_service",
                    set_={
                        "fingerprint": fingerprint,
                        "prompt_version": ai.EXTRACTION_PROMPT_VERSION,
                        "extracted_at": func.now(),
                    },
                )
            )

    async def load_signals(self, company_id: UUID, service_id: UUID) -> list[ai.StoredSignal]:
        stmt = select(Signal).where(
            Signal.company_id == company_id, Signal.service_id == service_id, Signal.status == "active"
        )
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [mapping.stored_signal(r) for r in rows]

    async def save_score(self, run_id: UUID | None, score: ai.LeadScore) -> ai.ScoreChange:
        current = and_(
            LeadScore.company_id == score.company_id,
            LeadScore.service_id == score.service_id,
            LeadScore.is_current.is_(True),
        )
        async with self._sessions() as session, session.begin():
            previous = (await session.execute(select(LeadScore).where(current))).scalars().first()
            await session.execute(update(LeadScore).where(current).values(is_current=False))
            session.add(mapping.lead_score_row(score, self._org_id))
            await domain_events.lead_tier_changed(
                session, self._org_id, score, previous.tier if previous else None
            )
        return ai.ScoreChange(
            company_id=score.company_id,
            service_id=score.service_id,
            priority_before=float(previous.priority) if previous else None,
            priority_after=score.priority,
            tier_before=previous.tier
            if previous and previous.tier in ("hot", "warm", "cold", "disqualified")
            else None,
            tier_after=score.tier,
        )
