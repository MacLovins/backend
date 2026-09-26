"""Worker-wide dependencies built once at worker start (SPEC CO-11): embedding model, Gemini client,
LangGraph checkpointer, Redis for live progress. Per run, `analysis_deps()` binds them to the org."""

import asyncio

import leadradar_ai as ai
import leadradar_parser as parser
import redis.asyncio as aioredis
from structlog import get_logger

from leadradar_core.adapters.collector import ParserCollector
from leadradar_core.adapters.llm import SqlLLMCache, SqlUsageSink
from leadradar_core.adapters.progress import RunProgressSink
from leadradar_core.adapters.store import SqlAnalysisStore
from leadradar_core.db.session import async_session_factory
from leadradar_core.settings import settings

log = get_logger(__name__)

CHECKPOINT_SCHEMA = "langgraph"


def langgraph_conn_string() -> str:
    url = settings.LANGGRAPH_DB_URL or settings.DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}options=-csearch_path%3D{CHECKPOINT_SCHEMA}"


class WorkerContext:
    def __init__(self) -> None:
        self.embedder: ai.Embedder | None = None
        self.llm: ai.LLMClient | None = None
        self.redis: aioredis.Redis | None = None
        self.checkpointer = None
        self._checkpointer_cm = None
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        ai_settings = ai.AISettings()
        self.embedder = ai.FastEmbedder(ai_settings.embed_model, cache_dir=ai_settings.embed_cache_dir)
        self.embedder.warm_up()
        self.llm = ai.GeminiClient.from_settings(
            ai.LLMSettings(),
            usage=SqlUsageSink(async_session_factory, settings.DEFAULT_ORG_ID),
            cache=SqlLLMCache(async_session_factory, settings.DEFAULT_ORG_ID),
        )
        self.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await self._start_checkpointer()
        self.started = True
        log.info("worker_ready", checkpointer=type(self.checkpointer).__name__ if self.checkpointer else None)

    async def _start_checkpointer(self) -> None:
        try:
            import psycopg
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            conn = langgraph_conn_string()
            async with await psycopg.AsyncConnection.connect(conn, autocommit=True) as c:
                await c.execute(f"CREATE SCHEMA IF NOT EXISTS {CHECKPOINT_SCHEMA}")
            self._checkpointer_cm = AsyncPostgresSaver.from_conn_string(conn)
            self.checkpointer = await self._checkpointer_cm.__aenter__()
            for attempt in range(3):  # several worker processes may create the tables at the same time
                try:
                    await self.checkpointer.setup()
                    break
                except psycopg.errors.UniqueViolation:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1 + attempt)
        except Exception as e:
            log.warning("checkpointer_unavailable", error=str(e))
            self.checkpointer = None

    async def stop(self) -> None:
        if self._checkpointer_cm is not None:
            await self._checkpointer_cm.__aexit__(None, None, None)
        if self.redis is not None:
            await self.redis.aclose()
        self.started = False

    def analysis_deps(self, org_id) -> ai.AnalysisDeps:
        assert self.started and self.llm and self.embedder, "worker context is not started"
        return ai.AnalysisDeps(
            collector=ParserCollector(
                async_session_factory,
                org_id,
                settings=parser.ParserSettings(),
                resolve_timeout_s=settings.ANALYSIS_RESOLVE_TIMEOUT_S,
                collect_time_budget_s=settings.ANALYSIS_COLLECT_BUDGET_S,
            ),
            store=SqlAnalysisStore(async_session_factory, org_id),
            progress=RunProgressSink(async_session_factory, self.redis, org_id),
            llm=self.llm,
            embedder=self.embedder,
            checkpointer=self.checkpointer,
            max_input_tokens=ai.LLMSettings().max_input_tokens,
        )


worker_context = WorkerContext()
_api_llm: ai.LLMClient | None = None


def shared_llm() -> ai.LLMClient | None:
    """The Gemini client for LLM calls made from API requests (outreach): the worker's one when it runs in
    this process, else a lightweight one — without loading the embedding model. None without an API key."""
    global _api_llm
    if worker_context.llm is not None:
        return worker_context.llm
    if _api_llm is None:
        try:
            _api_llm = ai.GeminiClient.from_settings(
                ai.LLMSettings(),
                usage=SqlUsageSink(async_session_factory, settings.DEFAULT_ORG_ID),
                cache=SqlLLMCache(async_session_factory, settings.DEFAULT_ORG_ID),
            )
        except ValueError as e:  # no API key configured
            log.warning("llm_unavailable", error=str(e))
            return None
    return _api_llm
