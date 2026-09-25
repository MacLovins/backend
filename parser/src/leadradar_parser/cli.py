import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, get_args

import typer
from pydantic import ValidationError

from . import (
    CollectPlan,
    CollectResult,
    CompanyRef,
    DiscoveryQuery,
    ParserSettings,
    SourceType,
    collect,
    create_http_client,
    discover,
    list_adapters,
    resolve_company,
)
from .adapters.registry import ADAPTERS

app = typer.Typer(no_args_is_help=True, help="Collect and normalize public company data.")
SOURCE_TYPES = set(get_args(SourceType))


@app.command("adapters")
def adapters_command() -> None:
    """List source adapters and their runtime status (JSON lines)."""
    for adapter in list_adapters():
        typer.echo(adapter.model_dump_json())


@app.command("resolve")
def resolve_command(
    domain: Annotated[str, typer.Option(help="Company domain, without a path")],
    name: Annotated[str | None, typer.Option(help="Company name")] = None,
) -> None:
    """Resolve homepage, careers, ATS, domains and firmographics."""
    company = asyncio.run(resolve_company(CompanyRef(name=name or domain, domain=domain)))
    typer.echo(company.model_dump_json(indent=2))


@app.command("collect")
def collect_command(
    name: Annotated[str, typer.Option(help="Company name")],
    domain: Annotated[str, typer.Option(help="Company domain")],
    sources: Annotated[
        str, typer.Option(help="Comma-separated source types (news, website, jobs…) or adapter ids (gdelt…)")
    ] = "news,website,jobs,registry",
    since: Annotated[str, typer.Option(help="Relative age (30d, 12h) or ISO date")] = "30d",
    topics: Annotated[str, typer.Option(help="Comma-separated news topics")] = "",
    job_keywords: Annotated[str, typer.Option(help="Comma-separated job search keywords")] = "",
    time_budget: Annotated[int, typer.Option(help="Seconds for the whole collection")] = 90,
    out: Annotated[Path | None, typer.Option(help="Write JSONL to this path instead of stdout")] = None,
) -> None:
    """Resolve and collect company documents as JSONL; stats and errors go to stderr."""
    source_types, adapter_ids, explicit = _parse_sources(sources)
    try:
        plan = CollectPlan(
            source_types=source_types,
            since=_parse_since(since),
            news_topics=_csv(topics),
            job_keywords=_csv(job_keywords),
            time_budget_s=time_budget,
        )
    except ValidationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    settings = ParserSettings()
    if adapter_ids:
        # Explicit adapter ids narrow the run; explicitly named source types keep all their enabled adapters.
        by_type = [
            item for item in settings.adapters if item in ADAPTERS and ADAPTERS[item].source_type in explicit
        ]
        settings = settings.model_copy(update={"adapters": [*adapter_ids, *by_type]})

    async def run() -> CollectResult:
        async with create_http_client(settings) as http:
            company = await resolve_company(CompanyRef(name=name, domain=domain), http=http)
            return await collect(company, plan, http=http)

    result = asyncio.run(run())
    lines = [document.model_dump_json() for document in result.documents]
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    elif lines:
        typer.echo("\n".join(lines))
    summary = {
        "documents": len(result.documents),
        "stats": result.stats,
        "errors": [error.model_dump(mode="json") for error in result.errors],
        "duration_ms": result.duration_ms,
    }
    typer.echo(json.dumps(summary), err=True)


@app.command("discover")
def discover_command(
    countries: Annotated[str, typer.Option(help="Comma-separated ISO2 codes")],
    industries: Annotated[str, typer.Option(help="Comma-separated taxonomy ids")],
    min_employees: Annotated[int | None, typer.Option(help="Minimum employees; unknown size is kept")] = None,
    max_employees: Annotated[int | None, typer.Option(help="Maximum employees")] = None,
    exclude: Annotated[str, typer.Option(help="Comma-separated domains to skip")] = "",
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
) -> None:
    """Discover companies matching an ICP through Wikidata (JSON lines)."""
    query = DiscoveryQuery(
        countries=_csv(countries),
        industries=_csv(industries),
        employees_min=min_employees,
        employees_max=max_employees,
        exclude_domains=_csv(exclude),
        limit=limit,
    )
    for candidate in asyncio.run(discover(query)):
        typer.echo(candidate.model_dump_json())


def _parse_sources(value: str) -> tuple[set[str], list[str], set[str]]:
    """Split --sources into (all source types, explicit adapter ids, explicitly named source types)."""
    source_types: set[str] = set()
    adapter_ids: list[str] = []
    explicit: set[str] = set()
    for item in _csv(value):
        if item in SOURCE_TYPES:
            source_types.add(item)
            explicit.add(item)
        elif item in ADAPTERS:
            adapter_ids.append(item)
            source_types.add(ADAPTERS[item].source_type)
        else:
            known = ", ".join(sorted(SOURCE_TYPES | set(ADAPTERS)))
            raise typer.BadParameter(f"Unknown source {item!r}; use one of: {known}", param_hint="--sources")
    if not source_types:
        raise typer.BadParameter("At least one source is required", param_hint="--sources")
    return source_types, adapter_ids, explicit


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_since(value: str) -> datetime:
    raw = value.strip().lower()
    if raw[:-1].isdigit() and raw[-1:] in {"d", "h"}:
        unit = "days" if raw.endswith("d") else "hours"
        return datetime.now(UTC) - timedelta(**{unit: int(raw[:-1])})
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        message = "Use a relative age such as 30d / 12h or an ISO date"
        raise typer.BadParameter(message, param_hint="--since") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
