"""Taskiq broker: Redis list queue in dev/demo, in-memory in tests.

taskiq worker leadradar_core.worker.broker:broker --max-async-tasks 3
taskiq scheduler leadradar_core.worker.broker:scheduler   (periodic tasks from `schedule` labels, CO-22)
"""

from taskiq import AsyncBroker, InMemoryBroker, TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker

from leadradar_core.settings import settings

broker: AsyncBroker = (
    InMemoryBroker()
    if settings.ENV == "test"
    else ListQueueBroker(
        settings.REDIS_URL,
        queue_name="leadradar",
        # redis-py 8 defaults to socket_timeout=5 s; the worker waits on a blocking BRPOP, and the resulting
        # TimeoutError is not handled by taskiq-redis, so the worker process would restart every few seconds.
        socket_timeout=None,
        socket_keepalive=True,
    )
)


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _startup(state: TaskiqState) -> None:
    from leadradar_core.worker.deps import worker_context

    await worker_context.start()


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _shutdown(state: TaskiqState) -> None:
    from leadradar_core.worker.deps import worker_context

    await worker_context.stop()


# register tasks on the broker (the worker CLI imports only this module)
from leadradar_core.worker import outreach, tasks  # noqa: E402, F401

# periodic tasks (CO-22): `taskiq scheduler leadradar_core.worker.broker:scheduler` reads their schedule labels
scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])
from leadradar_core.worker import scheduled  # noqa: E402, F401
