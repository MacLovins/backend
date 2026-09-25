"""Graph nodes (SPEC §1.7.1). Side effects go only through the ports in AnalysisDeps.

Company level: resolve → collect → index. Their failures degrade instead of failing the run: an unresolved
company is analysed with the given profile, a failed collection falls back to documents stored earlier.
Service level: prefilter → extract → verify → score. A failure marks only that service as failed;
QuotaExhausted marks it paused (the other services finish) and run_analysis raises AnalysisPaused.
"""

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import timedelta
from functools import wraps
from typing import Any

import structlog
from langgraph.types import Overwrite

from leadradar_ai.contracts import (
    CollectRequest,
    ProgressEvent,
    RunStats,
    ServiceBundle,
    Stage,
    StageStatus,
    StepError,
)
from leadradar_ai.errors import LeadRadarAIError, QuotaExhausted
from leadradar_ai.extraction.extract import PROMPT_VERSION, extract_service
from leadradar_ai.pipeline.deps import AnalysisDeps
from leadradar_ai.pipeline.state import AnalysisState, ServiceOutcome, ServiceState
from leadradar_ai.retrieval.chunking import chunk_document, index_text
from leadradar_ai.retrieval.prefilter import load_window, prefilter
from leadradar_ai.scoring.engine import score_company
from leadradar_ai.verification.verify import verify_extraction

log = structlog.get_logger(__name__)

EMBED_BATCH = 64
MAX_NEWS_TOPICS = 20
MAX_JOB_KEYWORDS = 20


def build_collect_request(services: list[ServiceBundle], now, max_items: int) -> CollectRequest:
    """One collection for all services: union of sources, the longest window, news and job focus terms."""
    questions = [q for s in services for q in s.questions]
    news_topics = [
        kw
        for q in questions
        if q.polarity == "positive" and "news" in q.source_types
        for lang in sorted(q.keywords, key=lambda lang: lang != "en")  # English first
        for kw in q.keywords[lang]
    ]
    job_keywords = [t for q in questions if "jobs" in q.source_types for t in q.job_titles]
    return CollectRequest(
        source_types=set().union(*(q.source_types for q in questions)) if questions else set(),
        since=now - timedelta(days=max((q.recency_days for q in questions), default=365)),
        news_topics=list(dict.fromkeys(news_topics))[:MAX_NEWS_TOPICS],
        job_keywords=list(dict.fromkeys(job_keywords))[:MAX_JOB_KEYWORDS],
        max_items_per_source=max_items,
    )


class Nodes:
    def __init__(self, deps: AnalysisDeps) -> None:
        self.deps = deps

    async def emit(
        self,
        state: AnalysisState | ServiceState,
        stage: Stage,
        status: StageStatus,
        message: str = "",
        **data: Any,
    ) -> None:
        inp = state["input"]
        service = state.get("service")
        await self.deps.progress.emit(
            ProgressEvent(
                run_id=inp.run_id,
                company_id=inp.company.id,
                service_id=service.service_id if service else None,
                stage=stage,
                status=status,
                message=message,
                data=data,
            )
        )

    async def _with_retries[T](self, attempts: int, call: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(1, attempts + 1):
            try:
                return await call()
            except QuotaExhausted:
                raise
            except Exception:
                if attempt == attempts:
                    raise
                await asyncio.sleep(self.deps.retry_backoff_s * attempt)
        raise AssertionError("unreachable")

    # --- company level --------------------------------------------------------------------------

    async def resolve(self, state: AnalysisState) -> dict:
        """First node: also resets the accumulating channels, so a new run on the same thread starts clean."""
        started = time.perf_counter()
        company = state["input"].company
        errors: list[StepError] = []
        await self.emit(state, "resolving", "started")
        try:
            company = await self._with_retries(
                self.deps.resolve_attempts, lambda: self.deps.collector.resolve(company)
            )
            await self.emit(
                state, "resolving", "done", domain=company.domain, own_domains=company.own_domains
            )
        except Exception as e:
            log.warning("resolve_failed", company=company.domain, error=str(e))
            await self.emit(state, "resolving", "done", f"Could not resolve the company: {e}")
            errors.append(StepError(stage="resolving", error_type=type(e).__name__, message=str(e)))
        return {
            "company": company,
            "outcomes": Overwrite([]),
            "errors": Overwrite(errors),
            "durations": Overwrite({"resolving": _ms(started)}),
        }

    async def collect(self, state: AnalysisState) -> dict:
        started = time.perf_counter()
        inp, company = state["input"], state["company"]
        request = build_collect_request(inp.services, inp.now, self.deps.max_items_per_source)
        await self.emit(state, "collecting", "started", sources=sorted(request.source_types))
        try:
            docs = await self._with_retries(
                self.deps.collect_attempts, lambda: self.deps.collector.collect(company, request)
            )
        except Exception as e:
            log.warning("collect_failed", company=company.domain, error=str(e))
            await self.emit(state, "collecting", "done", f"Collection failed, using stored documents: {e}")
            return {
                "documents_by_source": {},
                "durations": {"collecting": _ms(started)},
                "errors": [StepError(stage="collecting", error_type=type(e).__name__, message=str(e))],
            }
        counts = dict(Counter(d.source_type for d in docs))
        await self.emit(state, "collecting", "done", _describe_counts(counts), **counts)
        return {"documents_by_source": counts, "durations": {"collecting": _ms(started)}}

    async def index(self, state: AnalysisState) -> dict:
        started = time.perf_counter()
        company = state["company"]
        await self.emit(state, "indexing", "started")
        docs = await self.deps.store.documents_without_chunks(company.id)
        by_id = {d.id: d for d in docs}
        chunks = [c for d in docs for c in chunk_document(d, company.id)]
        for i in range(0, len(chunks), EMBED_BATCH):
            batch = chunks[i : i + EMBED_BATCH]
            texts = [
                index_text(c.text, by_id[c.document_id].title, by_id[c.document_id].source_type)
                for c in batch
            ]
            vectors = await asyncio.to_thread(self.deps.embedder.embed_passages, texts)
            await self.deps.store.save_chunks(
                [c.model_copy(update={"embedding": v}) for c, v in zip(batch, vectors, strict=True)]
            )
        await self.emit(
            state,
            "indexing",
            "done",
            f"{len(docs)} new documents, {len(chunks)} snippets",
            documents=len(docs),
            snippets=len(chunks),
        )
        return {"snippets_indexed": len(chunks), "durations": {"indexing": _ms(started)}}

    # --- service level --------------------------------------------------------------------------

    @staticmethod
    def service_node(stage: Stage) -> Callable:
        def decorator(fn: Callable[["Nodes", ServiceState], Awaitable[dict]]) -> Callable:
            @wraps(fn)
            async def node(self: "Nodes", state: ServiceState) -> dict:
                started = time.perf_counter()
                await self.emit(state, stage, "started")
                try:
                    update = await fn(self, state)
                except QuotaExhausted as e:
                    # handled here, not raised: a raise would cancel the parallel services mid-flight
                    await self.emit(state, "paused", "paused", str(e), step=stage)
                    service_id = state["service"].service_id
                    return {
                        "failed": True,
                        "outcomes": [ServiceOutcome(service_id=service_id, status="paused")],
                    }
                except Exception as e:
                    if isinstance(e, LeadRadarAIError):  # expected domain error: one line, no traceback
                        log.warning(
                            "service_step_failed",
                            stage=stage,
                            service=state["service"].key,
                            error=f"{type(e).__name__}: {e}",
                        )
                    else:
                        log.exception("service_step_failed", stage=stage, service=state["service"].key)
                    await self.emit(state, "failed", "failed", f"{stage}: {e}", step=stage)
                    service_id = state["service"].service_id
                    return {
                        "failed": True,
                        "errors": [
                            StepError(
                                stage=stage,
                                service_id=service_id,
                                error_type=type(e).__name__,
                                message=str(e),
                            )
                        ],
                        "outcomes": [ServiceOutcome(service_id=service_id, status="failed")],
                    }
                await self.emit(state, stage, "done", update.pop("_message", ""), **update.pop("_data", {}))
                return {**update, "durations": {stage: _ms(started)}}

            return node

        return decorator

    @service_node("prefiltering")
    async def prefilter(self, state: ServiceState) -> dict:
        inp, company, bundle = state["input"], state["company"], state["service"]
        since, sources = load_window(bundle, inp.now)
        snippets = await self.deps.store.load_snippets(company.id, since, sources)
        pre = await asyncio.to_thread(
            prefilter,
            company,
            bundle,
            snippets,
            self.deps.embedder,
            inp.now,
            PROMPT_VERSION,
            self.deps.prefilter,
        )
        stored = await self.deps.store.get_fingerprint(company.id, bundle.service_id)
        skip = inp.mode == "incremental" and stored == pre.fingerprint
        message = "Nothing new to check" if skip else f"{len(pre.snippets)} snippets selected"
        return {"pre": pre, "skip_extraction": skip, "_message": message, "_data": pre.stats}

    @service_node("extracting")
    async def extract(self, state: ServiceState) -> dict:
        extraction = await extract_service(
            self.deps.llm,
            state["company"],
            state["service"],
            state["pre"],
            max_input_tokens=self.deps.max_input_tokens,
            run_id=state["input"].run_id,
        )
        return {
            "extraction": extraction,
            "_data": {"llm_calls": extraction.llm_calls, "blocked": extraction.blocked},
        }

    @service_node("verifying")
    async def verify(self, state: ServiceState) -> dict:
        inp, company, bundle, pre = state["input"], state["company"], state["service"], state["pre"]
        result = verify_extraction(state["extraction"], bundle, pre.snippets, inp.now, PROMPT_VERSION)
        await self.deps.store.save_extraction(
            inp.run_id, company.id, bundle.service_id, pre.fingerprint, result.signals, result.rejected
        )
        return {
            "signals_verified": len(result.signals),
            "evidence_rejected": len(result.rejected),
            "final_answers": dict(result.final_answers),
            "_message": f"{len(result.signals)} verified signals, {len(result.rejected)} rejected",
        }

    @service_node("scoring")
    async def score(self, state: ServiceState) -> dict:
        inp, company, bundle = state["input"], state["company"], state["service"]
        signals = await self.deps.store.load_signals(company.id, bundle.service_id)
        score = score_company(company, bundle, signals, inp.now)
        change = await self.deps.store.save_score(inp.run_id, score)
        extraction = state.get("extraction")
        outcome = ServiceOutcome(
            service_id=bundle.service_id,
            status="done",
            extraction_skipped=bool(state.get("skip_extraction")),
            score=score,
            change=change,
            llm_calls=extraction.llm_calls if extraction else 0,
            llm_cache_hits=extraction.cache_hits if extraction else 0,
            signals_verified=state.get("signals_verified", 0),
            evidence_rejected=state.get("evidence_rejected", 0),
            final_answers=state.get("final_answers", {}),
            auto_no=extraction.auto_no if extraction else [],
        )
        return {
            "outcomes": [outcome],
            "_message": f"Priority {score.priority} ({score.tier})",
            "_data": {"priority": score.priority, "tier": score.tier},
        }

    # --- finish ---------------------------------------------------------------------------------

    async def finalize(self, state: AnalysisState) -> dict:
        outcomes = state.get("outcomes", [])
        scores = [o.score for o in outcomes if o.score is not None]
        stats = RunStats(
            documents_by_source=state.get("documents_by_source", {}),
            snippets_indexed=state.get("snippets_indexed", 0),
            llm_calls=sum(o.llm_calls for o in outcomes),
            llm_cache_hits=sum(o.llm_cache_hits for o in outcomes),
            signals_verified=sum(o.signals_verified for o in outcomes),
            evidence_rejected=sum(o.evidence_rejected for o in outcomes),
            extraction_skipped_services=sum(o.extraction_skipped for o in outcomes),
            duration_ms_by_stage=state.get("durations", {}),
        )
        failed = [str(o.service_id) for o in outcomes if o.status == "failed"]
        paused = [str(o.service_id) for o in outcomes if o.status == "paused"]
        message = f"{len(scores)} services scored"
        message += f", {len(failed)} failed" if failed else ""
        message += f", {len(paused)} paused (LLM quota)" if paused else ""
        await self.emit(
            state,
            "paused" if paused else "done",
            "paused" if paused else "done",
            message,
            scores=[
                {"service_id": str(s.service_id), "priority": s.priority, "tier": s.tier} for s in scores
            ],
            failed_services=failed,
            paused_services=paused,
        )
        return {"scores": scores, "stats": stats}


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _describe_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items())) or "no new documents"
