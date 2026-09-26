import asyncio
from uuid import UUID

from leadradar_core.settings import settings

_background_tasks: set[asyncio.Task] = set()


async def enqueue_analysis(run_id: UUID, company_ids: list[UUID], service_ids: list[UUID], mode: str) -> int:
    if settings.ENV == "test" or not company_ids:
        return 0
    from leadradar_core.worker.tasks import analyze_company

    for company_id in company_ids:
        if getattr(settings, "EMBEDDED_WORKER", True):
            task = asyncio.create_task(
                analyze_company(str(run_id), str(company_id), [str(s) for s in service_ids], mode)
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        else:
            await analyze_company.kiq(str(run_id), str(company_id), [str(s) for s in service_ids], mode)
    return len(company_ids)


async def enqueue_expand(question_id: UUID) -> bool:
    if settings.ENV == "test":
        return False
    from leadradar_core.worker.tasks import expand_question

    if getattr(settings, "EMBEDDED_WORKER", True):
        task = asyncio.create_task(expand_question(str(question_id)))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    else:
        await expand_question.kiq(str(question_id))
    return True
