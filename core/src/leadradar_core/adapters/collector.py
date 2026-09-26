"""ParserCollector — the Collector port of leadradar-ai over leadradar-parser.

collect: parser.collect → documents saved with dedup (company_id, content_hash) → all documents of the window
(new and collected earlier) returned as ai.AnalysisDocument.

Both parser calls run under a hard timeout: resolve can hang when the company site is unreachable
(bot protection), and the collection time budget does not cover resolve. A timeout degrades to the stored
profile / the documents collected earlier instead of blocking the worker.
"""

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import leadradar_ai as ai
import leadradar_parser as parser
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog import get_logger

from leadradar_core.adapters import mapping
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.intelligence.models import Document

log = get_logger(__name__)

PARSER_SOURCE_TYPES = {"news", "website", "jobs", "report", "registry", "incident"}


async def with_deadline[T](coro: Coroutine[Any, Any, T], timeout_s: float) -> T:
    """Like asyncio.wait_for, but does not wait for the cancelled task to finish: a parser call stuck in
    cleanup must not block the worker. The abandoned task is cancelled and left to the event loop."""
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    if not done:
        task.cancel()
        raise TimeoutError(f"no result after {timeout_s}s")
    return task.result()


class ParserCollector:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        org_id: UUID,
        *,
        settings: parser.ParserSettings | None = None,
        resolve_timeout_s: float = 60,
        collect_time_budget_s: int = 90,
        collect_grace_s: float = 30,
    ) -> None:
        self._sessions = session_factory
        self._org_id = org_id
        self._settings = settings
        self._resolve_timeout_s = resolve_timeout_s
        self._budget_s = collect_time_budget_s
        self._grace_s = collect_grace_s

    async def resolve(self, company: ai.CompanyProfile) -> ai.CompanyProfile:
        async with self._sessions() as session:
            row = await session.get(Company, company.id)
        if row is None:
            return company
        try:
            async with parser.create_http_client(self._settings) as http:
                resolved = await with_deadline(
                    parser.resolve_company(mapping.company_ref(row), http=http), self._resolve_timeout_s
                )
        except TimeoutError:
            log.warning("resolve_timeout", domain=row.domain, timeout_s=self._resolve_timeout_s)
            return mapping.company_profile(row)
        async with self._sessions() as session, session.begin():
            row = await session.get(Company, company.id)
            mapping.apply_resolved(row, resolved)
            return mapping.company_profile(row)

    async def collect(
        self, company: ai.CompanyProfile, request: ai.CollectRequest
    ) -> list[ai.AnalysisDocument]:
        source_types = request.source_types & PARSER_SOURCE_TYPES
        if source_types:
            await self._collect_new(company, request, source_types)
        stmt = select(Document).where(
            Document.company_id == company.id,
            Document.source_type.in_(request.source_types),
            func.coalesce(Document.published_at, Document.fetched_at) >= request.since,
        )
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [mapping.analysis_document(r) for r in rows]

    async def _collect_new(
        self, company: ai.CompanyProfile, request: ai.CollectRequest, source_types: set[str]
    ) -> None:
        async with self._sessions() as session:
            row = await session.get(Company, company.id)
        if row is None:
            return
        resolved = parser.ResolvedCompany(
            **mapping.company_ref(row).model_dump(),
            homepage_url=row.homepage_url or f"https://{row.domain}",
            own_domains=list(dict.fromkeys([row.domain, *(row.own_domains or [])])),
            resolved_at=row.resolved_at or datetime.now(UTC),
        )
        plan = parser.CollectPlan(
            source_types=source_types,
            since=request.since,
            news_topics=request.news_topics,
            job_keywords=request.job_keywords,
            max_items_per_source=request.max_items_per_source,
            time_budget_s=self._budget_s,
        )
        try:
            async with parser.create_http_client(self._settings) as http:
                result = await with_deadline(
                    parser.collect(resolved, plan, http=http), self._budget_s + self._grace_s
                )
        except TimeoutError:
            log.warning("collect_timeout", domain=row.domain, timeout_s=self._budget_s + self._grace_s)
            return
        for err in result.errors:
            log.info(
                "source_error", domain=row.domain, adapter=err.adapter, kind=err.kind, message=err.message
            )
        if not result.documents:
            return
        rows = [mapping.document_row(company.id, self._org_id, d) for d in result.documents]
        async with self._sessions() as session, session.begin():
            await session.execute(
                insert(Document)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_document_company_content_hash")
            )
