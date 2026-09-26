"""Enqueue worker tasks from the API. In tests (APP_ENV=test) nothing is enqueued: tasks are tested directly."""

from uuid import UUID

from leadradar_core.settings import settings


async def enqueue_analysis(run_id: UUID, company_ids: list[UUID], service_ids: list[UUID], mode: str) -> int:
    if settings.ENV == "test" or not company_ids:
        return 0
    from leadradar_core.worker.tasks import analyze_company

    for company_id in company_ids:
        await analyze_company.kiq(str(run_id), str(company_id), [str(s) for s in service_ids], mode)
    return len(company_ids)


async def enqueue_expand(question_id: UUID) -> bool:
    if settings.ENV == "test":
        return False
    from leadradar_core.worker.tasks import expand_question

    await expand_question.kiq(str(question_id))
    return True
