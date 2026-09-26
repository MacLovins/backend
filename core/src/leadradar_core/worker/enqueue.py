"""Enqueue worker tasks from the API. In tests (APP_ENV=test) nothing is enqueued: tasks are tested directly.

EMBEDDED_WORKER runs the tasks inside the API process (no separate worker needed for a demo). A semaphore
bounds concurrent analyses: every analysis loads documents, embeds and calls the LLM, and 45 companies at once
would exhaust the free Gemini quota and stall the API's event loop.
"""

import asyncio
from collections.abc import Awaitable
from uuid import UUID

from structlog import get_logger

from leadradar_core.settings import settings

log = get_logger(__name__)

_background_tasks: set[asyncio.Task] = set()
_semaphore: asyncio.Semaphore | None = None


def _limit() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, settings.EMBEDDED_MAX_CONCURRENCY))
    return _semaphore


async def _bounded(job: Awaitable) -> None:
    async with _limit():
        try:
            await job
        except Exception:
            log.exception("embedded_task_failed")


def _spawn(job: Awaitable) -> None:
    task = asyncio.create_task(_bounded(job))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def enqueue_analysis(run_id: UUID, company_ids: list[UUID], service_ids: list[UUID], mode: str) -> int:
    if settings.ENV == "test" or not company_ids:
        return 0
    from leadradar_core.worker.tasks import analyze_company

    services = [str(s) for s in service_ids]
    for company_id in company_ids:
        if settings.EMBEDDED_WORKER:
            _spawn(analyze_company(str(run_id), str(company_id), services, mode))
        else:
            await analyze_company.kiq(str(run_id), str(company_id), services, mode)
    return len(company_ids)


async def enqueue_expand(question_id: UUID) -> bool:
    if settings.ENV == "test":
        return False
    from leadradar_core.worker.tasks import expand_question

    if settings.EMBEDDED_WORKER:
        _spawn(expand_question(str(question_id)))
    else:
        await expand_question.kiq(str(question_id))
    return True
