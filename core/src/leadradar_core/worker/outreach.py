"""Worker task: outreach draft generation (CO-A1). The API enqueues an outreach_job; this task calls the LLM."""

from uuid import UUID

from structlog import get_logger

from leadradar_core.db.session import async_session_factory
from leadradar_core.modules.outreach.service import run_job
from leadradar_core.worker.broker import broker
from leadradar_core.worker.deps import worker_context

log = get_logger(__name__)


@broker.task(task_name="generate_outreach", retry_on_error=False)
async def generate_outreach(job_id: str) -> str:
    await worker_context.start()
    async with async_session_factory() as session:
        status = await run_job(session, UUID(job_id), worker_context.llm)
    log.info("outreach_generated", job_id=job_id, status=status)
    return status
