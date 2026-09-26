"""Hand work to the worker (Taskiq over Redis). The API never runs analysis or LLM calls itself.

APP_EMBEDDED_WORKER=true (dev without a worker process) runs the tasks as background asyncio tasks inside the
API process instead. In tests (APP_ENV=test) nothing is enqueued: tests call the task functions directly.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any
from uuid import UUID

from leadradar_core.settings import settings

_background_tasks: set[asyncio.Task] = set()


async def _dispatch(task: Any, *args: Any) -> None:
    if settings.EMBEDDED_WORKER:
        coro: Coroutine[Any, Any, Any] = task(*args)  # a Taskiq task called directly runs the function
        background = asyncio.create_task(coro)
        _background_tasks.add(background)
        background.add_done_callback(_background_tasks.discard)
    else:
        await task.kiq(*args)


async def enqueue_analysis(run_id: UUID, company_ids: list[UUID], service_ids: list[UUID], mode: str) -> int:
    if settings.ENV == "test" or not company_ids:
        return 0
    from leadradar_core.worker.tasks import analyze_company

    for company_id in company_ids:
        await _dispatch(analyze_company, str(run_id), str(company_id), [str(s) for s in service_ids], mode)
    return len(company_ids)


async def enqueue_expand(question_id: UUID) -> bool:
    if settings.ENV == "test":
        return False
    from leadradar_core.worker.tasks import expand_question

    await _dispatch(expand_question, str(question_id))
    return True


async def enqueue_outreach(job_id: UUID) -> bool:
    if settings.ENV == "test":
        return False
    from leadradar_core.worker.outreach import generate_outreach

    await _dispatch(generate_outreach, str(job_id))
    return True
