"""LLMCache and UsageSink ports of leadradar-ai: the llm_cache and llm_call tables."""

from datetime import datetime, time
from uuid import UUID
from zoneinfo import ZoneInfo

import leadradar_ai as ai
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from leadradar_core.modules.meta.models import LLMCache, LLMCall

PACIFIC = ZoneInfo("America/Los_Angeles")  # Gemini daily quotas reset at midnight Pacific time
NOT_QUOTA = ("cache_hit", "rate_limited")


class SqlLLMCache:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], org_id: UUID) -> None:
        self._sessions = session_factory
        self._org_id = org_id

    async def get(self, key: str) -> dict | None:
        async with self._sessions() as session:
            return (
                await session.execute(select(LLMCache.response).where(LLMCache.key == key))
            ).scalar_one_or_none()

    async def set(self, key: str, value: dict, meta: dict) -> None:
        model = str(meta.get("model", ""))[:64]
        prompt_version = str(meta.get("prompt_version", ""))[:32]
        async with self._sessions() as session, session.begin():
            await session.execute(
                insert(LLMCache)
                .values(
                    key=key, org_id=self._org_id, response=value, model=model, prompt_version=prompt_version
                )
                .on_conflict_do_update(
                    index_elements=[LLMCache.key],
                    set_={
                        "response": value,
                        "model": model,
                        "prompt_version": prompt_version,
                        "updated_at": func.now(),
                    },
                )
            )


class SqlUsageSink:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], org_id: UUID) -> None:
        self._sessions = session_factory
        self._org_id = org_id

    async def record(self, call: ai.LLMCallRecord) -> None:
        async with self._sessions() as session, session.begin():
            session.add(
                LLMCall(
                    org_id=self._org_id,
                    run_id=None,  # llm_call.run_id references analysis_run; graph run ids are the same ids
                    purpose=call.purpose,
                    model=call.model[:64],
                    prompt_version=call.prompt_version[:32],
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    latency_ms=call.latency_ms,
                    cache_hit=call.cache_hit,
                    status=call.status,
                    error=call.error,
                )
            )

    async def used_today(self, model: str) -> int:
        start = datetime.combine(datetime.now(PACIFIC).date(), time.min, tzinfo=PACIFIC)
        stmt = select(func.count(LLMCall.id)).where(
            LLMCall.model == model, LLMCall.created_at >= start, LLMCall.status.not_in(NOT_QUOTA)
        )
        async with self._sessions() as session:
            return int((await session.execute(stmt)).scalar_one())
