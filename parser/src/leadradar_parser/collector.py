import asyncio
from time import monotonic

import httpx

from .adapters.base import SourceAdapter
from .adapters.registry import ADAPTERS
from .contracts import CollectPlan, CollectResult, Document, ResolvedCompany, SourceError
from .errors import (
    RobotsDenied,
    SourceBlocked,
    SourceDisabled,
    SourceRateLimited,
    SourceRequestFailed,
    SourceTimeout,
)
from .http import HttpClient, create_http_client
from .normalize import deduplicate


async def collect(
    company: ResolvedCompany,
    plan: CollectPlan,
    *,
    http: HttpClient | None = None,
) -> CollectResult:
    started = monotonic()
    if http is not None:
        return await _collect(company, plan, http, started)
    async with create_http_client() as owned_http:
        return await _collect(company, plan, owned_http, started)


async def _collect(
    company: ResolvedCompany, plan: CollectPlan, http: HttpClient, started: float
) -> CollectResult:
    errors: list[SourceError] = []
    stats: dict[str, int] = {}
    runnable: list[SourceAdapter] = []
    for adapter_id, adapter in ADAPTERS.items():
        if adapter_id not in http.settings.adapters or adapter.source_type not in plan.source_types:
            continue
        if adapter.requires_env and not http.settings.env(adapter.requires_env):
            errors.append(
                SourceError(adapter=adapter_id, kind="disabled", message=f"{adapter.requires_env} is not set")
            )
            stats[adapter_id] = 0
            continue
        runnable.append(adapter)

    # Each adapter streams into its own bucket, so documents yielded before a failure or the
    # time budget are kept.
    buckets: dict[str, list[Document]] = {adapter.id: [] for adapter in runnable}
    tasks = {
        asyncio.create_task(_drain(adapter, company, plan, http, buckets[adapter.id])): adapter.id
        for adapter in runnable
    }
    pending: set[asyncio.Task[None]] = set()
    try:
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=plan.time_budget_s)
    finally:
        leftovers = [task for task in tasks if not task.done()]
        for task in leftovers:
            task.cancel()
        if leftovers:
            await asyncio.gather(*leftovers, return_exceptions=True)

    documents: list[Document] = []
    for task, adapter_id in tasks.items():
        collected = buckets[adapter_id]
        documents.extend(collected)
        stats[adapter_id] = len(collected)
        if task in pending:
            errors.append(
                SourceError(
                    adapter=adapter_id,
                    kind="timeout",
                    message=f"Time budget of {plan.time_budget_s}s exceeded after {len(collected)} documents",
                )
            )
        elif (exc := task.exception()) is not None:
            errors.append(_source_error(adapter_id, exc))
    return CollectResult(
        documents=deduplicate(documents),
        errors=errors,
        stats=stats,
        duration_ms=int((monotonic() - started) * 1_000),
    )


async def _drain(
    adapter: SourceAdapter,
    company: ResolvedCompany,
    plan: CollectPlan,
    http: HttpClient,
    sink: list[Document],
) -> None:
    async for document in adapter.fetch(company, plan, http):
        sink.append(document)


def _source_error(adapter: str, exc: BaseException) -> SourceError:
    if isinstance(exc, SourceRateLimited):
        return SourceError(
            adapter=adapter, kind="rate_limited", message=str(exc), retry_after_s=exc.retry_after_s
        )
    if isinstance(exc, SourceDisabled):
        return SourceError(adapter=adapter, kind="disabled", message=str(exc))
    if isinstance(exc, SourceBlocked):
        return SourceError(adapter=adapter, kind="blocked", message=str(exc))
    if isinstance(exc, RobotsDenied):
        return SourceError(adapter=adapter, kind="robots", message=str(exc))
    if isinstance(exc, (SourceTimeout, TimeoutError, httpx.TimeoutException)):
        return SourceError(adapter=adapter, kind="timeout", message=str(exc) or type(exc).__name__)
    if isinstance(exc, (SourceRequestFailed, httpx.HTTPError)):
        return SourceError(adapter=adapter, kind="not_found", message=str(exc))
    return SourceError(adapter=adapter, kind="parse_error", message=f"{type(exc).__name__}: {exc}")
