"""lr-ai — run the AI engine without core: presets, analysis of parser fixtures, keyword expansion.

    lr-ai presets [intelligent_automation]
    lr-ai analyze --fixture tests/fixtures/dhl.jsonl --domain dhl.com --name "DHL Group" --live
    lr-ai expand --preset intelligent_automation --question ia_hiring --live

Without --live nothing goes to the network: answers come from the local LLM cache (.cache/lr-ai/llm),
and an uncached prompt fails that service with a hint. A repeated --live run costs 0 LLM calls.
"""

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import NAMESPACE_URL, uuid5

import typer

from leadradar_ai.config_assist import expand_question, languages_for_icp
from leadradar_ai.contracts import AnalysisInput, CompanyProfile, LeadScore, ProgressEvent, ServiceBundle
from leadradar_ai.errors import AnalysisPaused
from leadradar_ai.llm.gemini import GeminiClient
from leadradar_ai.llm.types import LLMClient
from leadradar_ai.local import CacheOnlyTransport, FileLLMCache, FileUsageSink, load_parser_jsonl
from leadradar_ai.pipeline import AnalysisDeps, build_analysis_graph, run_analysis
from leadradar_ai.ports import Embedder
from leadradar_ai.presets import SIGNAL_CATEGORIES, list_presets, load_preset
from leadradar_ai.retrieval import FastEmbedder, PrefilterConfig
from leadradar_ai.settings import AISettings, LLMSettings
from leadradar_ai.testing import FakeCollector, FakeEmbedder, InMemoryStore

app = typer.Typer(
    help="LeadRadar AI engine: presets, fixture analysis, keyword expansion.", no_args_is_help=True
)

DEFAULT_CACHE = Path(".cache/lr-ai")


# --- wiring (patched in tests) ------------------------------------------------------------------


def make_llm(live: bool, cache_dir: Path) -> tuple[LLMClient, FileUsageSink]:
    usage = FileUsageSink(cache_dir / "usage.jsonl")
    cache = FileLLMCache(cache_dir / "llm")
    settings = LLMSettings()
    if live:
        return GeminiClient.from_settings(settings, usage, cache), usage
    offline = settings.model_copy(update={"cache_enabled": True})
    return GeminiClient(offline, CacheOnlyTransport(), usage, cache), usage


def make_embedder(fake: bool) -> Embedder:
    if fake:
        return FakeEmbedder()
    s = AISettings()
    return FastEmbedder(s.embed_model, cache_dir=s.embed_cache_dir)


class EchoProgress:
    async def emit(self, event: ProgressEvent) -> None:
        if event.status in ("done", "failed", "paused") or event.stage in ("failed", "paused"):
            scope = f"[{event.service_id.hex[:6]}]" if event.service_id else ""
            typer.echo(f"  · {event.stage:<13}{scope:<9} {event.status:<7} {event.message}")


# --- presets ------------------------------------------------------------------------------------


@app.command()
def presets(key: Annotated[str | None, typer.Argument(help="Preset to show in detail")] = None) -> None:
    """List presets, or show the questions of one."""
    if key is None:
        for k in list_presets():
            p = load_preset(k)
            typer.echo(f"{k:<24} {p.name} — {len(p.questions)} questions, {len(p.rules)} rules")
        return
    p = load_preset(key)
    typer.echo(f"{p.name}\n{p.description}\nDecision makers: {', '.join(p.decision_makers)}\n")
    for q in p.questions:
        sign = "+" if q.polarity == "positive" else "−"
        typer.echo(
            f"{sign} {q.weight:<6} {q.key:<16} {q.label} ({SIGNAL_CATEGORIES[q.category]}; "
            f"{', '.join(sorted(q.source_types))}; {q.recency_days} d)"
        )
    for r in p.rules:
        typer.echo(f"  rule: {r.action:<7} {r.name}")


# --- analyze ------------------------------------------------------------------------------------


def _company(
    name: str | None,
    domain: str,
    aliases: list[str],
    country: str | None,
    industries: list[str],
    employees: int | None,
    own_domains: list[str],
) -> CompanyProfile:
    domain = domain.lower().removeprefix("www.")
    return CompanyProfile(
        id=uuid5(NAMESPACE_URL, f"leadradar:company:{domain}"),
        name=name or domain.split(".")[0].capitalize(),
        domain=domain,
        aliases=aliases,
        own_domains=list(dict.fromkeys([domain, *own_domains])),
        country_code=country.upper() if country else None,
        industry_ids=industries,
        employees=employees,
    )


def _print_service(bundle: ServiceBundle, score: LeadScore | None, store: InMemoryStore, company_id) -> None:
    typer.echo(f"\n━━ {bundle.name} " + "━" * max(0, 60 - len(bundle.name)))
    if score is None:
        typer.echo("  not scored (see errors above)")
        return
    typer.echo(
        f"  Priority {score.priority}  [{score.tier.upper()}]   Fit {score.fit} · Intent {score.intent} · "
        f"Risk {score.risk}"
    )
    for hit in score.rule_hits:
        typer.echo(f"  rule {hit['action']}: {hit['name']}")
    if score.why_now:
        typer.echo("  Why now:")
        for r in score.why_now:
            date = f" ({r.date})" if r.date else ""
            source = f" — {r.source_name}" if r.source_name else ""
            typer.echo(f"    [{r.polarity}] {r.text}{source}{date}")
    signals = store.signals.get((company_id, bundle.service_id), [])
    by_question = {}
    for s in signals:
        by_question.setdefault(s.question_id, []).append(s)
    typer.echo("  Breakdown:")
    for c in score.breakdown:
        if c.points == 0:
            continue
        sign = "+" if c.polarity == "positive" else "−"
        typer.echo(f"    {sign} {c.key:<16} strength {c.strength:.2f} × weight {c.weight:g} = {c.points:.2f}")
        for s in by_question.get(c.question_id, []):
            flags = f" [{', '.join(sorted(s.flags))}]" if s.flags else ""
            typer.echo(
                f"        “{s.quote[:160]}” — {s.source_name}, {s.event_date or 'undated'}, "
                f"{s.strength}, conf {s.confidence:.2f}{flags}"
            )
            typer.echo(f"          {s.url}")
    rejected = Counter(r.reason for r in store.rejected.get((company_id, bundle.service_id), []))
    if rejected:
        typer.echo(
            "  Rejected by the verifier: " + ", ".join(f"{k} {v}" for k, v in sorted(rejected.items()))
        )


@app.command()
def analyze(
    fixture: Annotated[list[Path], typer.Option(help="JSONL from `lr-parser collect --out` (repeatable)")],
    domain: Annotated[str, typer.Option(help="Company domain, e.g. dhl.com")],
    name: Annotated[str | None, typer.Option(help="Company name")] = None,
    alias: Annotated[list[str] | None, typer.Option(help="Alias (repeatable)")] = None,
    own_domain: Annotated[list[str] | None, typer.Option(help="Extra own domain, e.g. the ATS host")] = None,
    country: Annotated[str | None, typer.Option(help="ISO2 country code")] = None,
    industry: Annotated[list[str] | None, typer.Option(help="Industry id (repeatable)")] = None,
    employees: Annotated[int | None, typer.Option()] = None,
    service: Annotated[list[str] | None, typer.Option(help="Preset key (repeatable); default: all")] = None,
    live: Annotated[bool, typer.Option(help="Call Gemini for uncached prompts")] = False,
    mode: Annotated[str, typer.Option(help="full | incremental")] = "full",
    now: Annotated[str | None, typer.Option(help="Reference date, ISO (default: today)")] = None,
    fake_embeddings: Annotated[
        bool, typer.Option(help="Hash embeddings instead of e5 (no model download)")
    ] = False,
    cache_dir: Annotated[Path, typer.Option()] = DEFAULT_CACHE,
    json_out: Annotated[Path | None, typer.Option("--json", help="Also write the result as JSON")] = None,
) -> None:
    """Analyse a company from parser fixtures: signals with quotes and the explainable score."""
    documents = [d for path in fixture for d in load_parser_jsonl(path)]
    company = _company(name, domain, alias or [], country, industry or [], employees, own_domain or [])
    bundles = [load_preset(k).to_bundle() for k in (service or list_presets())]
    reference = datetime.fromisoformat(now).replace(tzinfo=UTC) if now else datetime.now(UTC)

    store = InMemoryStore()
    llm, _ = make_llm(live, cache_dir)
    ai = AISettings()
    deps = AnalysisDeps(
        collector=FakeCollector(documents, store=store),
        store=store,
        progress=EchoProgress(),
        llm=llm,
        embedder=make_embedder(fake_embeddings),
        prefilter=PrefilterConfig.from_settings(ai),
        max_input_tokens=LLMSettings().max_input_tokens,
        retry_backoff_s=0,
    )
    inp = AnalysisInput(
        run_id=uuid5(NAMESPACE_URL, f"leadradar:cli:{company.domain}:{reference.isoformat()}"),
        company=company,
        services=bundles,
        mode=mode,
        now=reference,
    )
    typer.echo(
        f"{company.name} ({company.domain}): {len(documents)} documents, "
        f"services: {', '.join(b.key for b in bundles)}, {'live' if live else 'cache-only'}"
    )

    exit_code = 0
    try:
        output = asyncio.run(run_analysis(build_analysis_graph(deps), inp))
    except AnalysisPaused as e:
        output, exit_code = e.output, 2
        typer.echo(f"\nPaused: {e}. Rerun later — finished services will not call the LLM again.")

    scores = {s.service_id: s for s in output.get("scores", [])}
    for b in bundles:
        _print_service(b, scores.get(b.service_id), store, company.id)
    for err in output.get("errors", []):
        typer.echo(f"\nerror [{err.stage}] {err.error_type}: {err.message}", err=True)
        exit_code = exit_code or 1
    stats = output.get("stats")
    if stats:
        typer.echo(
            f"\nLLM calls: {stats.llm_calls} (cache hits {stats.llm_cache_hits}) · "
            f"verified signals {stats.signals_verified} · rejected {stats.evidence_rejected} · "
            f"snippets {stats.snippets_indexed}"
        )

    if json_out:
        payload = {
            "company": company.model_dump(mode="json"),
            "scores": [s.model_dump(mode="json") for s in output.get("scores", [])],
            "signals": [s.model_dump(mode="json") for v in store.signals.values() for s in v],
            "rejected": [r.model_dump(mode="json") for v in store.rejected.values() for r in v],
            "errors": [e.model_dump(mode="json") for e in output.get("errors", [])],
            "stats": stats.model_dump(mode="json") if stats else None,
        }
        json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        typer.echo(f"JSON written to {json_out}")
    raise typer.Exit(exit_code)


# --- expand -------------------------------------------------------------------------------------


@app.command()
def expand(
    preset: Annotated[str, typer.Option(help="Preset key")],
    question: Annotated[str, typer.Option(help="Question key, e.g. ia_hiring")],
    language: Annotated[
        list[str] | None, typer.Option(help="Language (repeatable); default: from the ICP")
    ] = None,
    live: Annotated[bool, typer.Option(help="Call Gemini for uncached prompts")] = False,
    cache_dir: Annotated[Path, typer.Option()] = DEFAULT_CACHE,
) -> None:
    """Generate multilingual keywords, job titles and stop terms for a preset question."""
    bundle = load_preset(preset).to_bundle()
    q = next((q for q in bundle.questions if q.key == question), None)
    if q is None:
        raise typer.BadParameter(
            f"no question '{question}' in {preset}: {', '.join(x.key for x in bundle.questions)}"
        )
    llm, _ = make_llm(live, cache_dir)
    languages = language or languages_for_icp(bundle.icp)
    result = asyncio.run(expand_question(llm, bundle, q, languages=languages))
    typer.echo(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
