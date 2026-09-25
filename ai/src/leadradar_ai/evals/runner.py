"""Eval runner: analyse every labelled company on its fixture (full mode, fresh in-memory store) and compare
the verified answers with the golden labels."""

from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel

from leadradar_ai.contracts import AnalysisInput, CompanyProfile
from leadradar_ai.errors import AnalysisPaused
from leadradar_ai.evals.golden import CompanyCase, GoldenLabel
from leadradar_ai.evals.metrics import Decision, Metrics, compute_metrics
from leadradar_ai.extraction.extract import PROMPT_VERSION
from leadradar_ai.llm.types import LLMClient
from leadradar_ai.local import load_parser_jsonl
from leadradar_ai.pipeline import AnalysisDeps, build_analysis_graph, run_analysis
from leadradar_ai.ports import Embedder, ProgressSink
from leadradar_ai.presets import load_preset
from leadradar_ai.retrieval.prefilter import PrefilterConfig
from leadradar_ai.testing import FakeCollector, InMemoryStore


class EvalResult(BaseModel):
    prompt_version: str
    mode: str
    now: datetime
    decisions: list[Decision]
    metrics: Metrics
    notes: list[str]


class _SilentProgress:
    async def emit(self, event) -> None:
        return None


def company_profile(case: CompanyCase) -> CompanyProfile:
    domain = case.domain.lower().removeprefix("www.")
    return CompanyProfile(
        id=uuid5(NAMESPACE_URL, f"leadradar:company:{domain}"),
        name=case.name,
        domain=domain,
        aliases=case.aliases,
        own_domains=list(dict.fromkeys([domain, *case.own_domains])),
        country_code=case.country,
        industry_ids=case.industries,
        employees=case.employees,
        tags=case.tags,
    )


async def run_eval(
    labels: list[GoldenLabel],
    companies: dict[str, CompanyCase],
    fixtures_dir: str | Path,
    llm: LLMClient,
    embedder: Embedder,
    now: datetime,
    *,
    mode: str = "cache-only",
    prefilter: PrefilterConfig | None = None,
    max_input_tokens: int = 30_000,
    progress: ProgressSink | None = None,
) -> EvalResult:
    fixtures_dir = Path(fixtures_dir)
    decisions: list[Decision] = []
    notes: list[str] = []
    verified_total = 0
    rejected_by_reason: Counter[str] = Counter()
    llm_calls = 0
    analysed = 0

    by_company: dict[str, list[GoldenLabel]] = {}
    for label in labels:
        by_company.setdefault(label.company_domain, []).append(label)

    for domain, company_labels in sorted(by_company.items()):
        case = companies.get(domain)
        bundles = {s: load_preset(s).to_bundle() for s in sorted({lbl.service for lbl in company_labels})}
        questions = {(s, q.key): q for s, b in bundles.items() for q in b.questions}
        unknown = [
            lbl.question_key for lbl in company_labels if (lbl.service, lbl.question_key) not in questions
        ]
        if unknown:
            raise ValueError(f"{domain}: unknown question keys {unknown}")

        outcomes = {}
        store = InMemoryStore()
        fixture = fixtures_dir / case.fixture if case else None
        if case is None:
            notes.append(f"{domain}: no entry in companies file — not evaluated")
        elif not fixture.exists():
            notes.append(f"{domain}: fixture {fixture} not found — not evaluated")
        else:
            company = company_profile(case)
            deps = AnalysisDeps(
                collector=FakeCollector(load_parser_jsonl(fixture), store=store),
                store=store,
                progress=progress or _SilentProgress(),
                llm=llm,
                embedder=embedder,
                prefilter=prefilter or PrefilterConfig(),
                max_input_tokens=max_input_tokens,
                retry_backoff_s=0,
            )
            inp = AnalysisInput(
                run_id=uuid5(NAMESPACE_URL, f"leadradar:eval:{domain}"),
                company=company,
                services=list(bundles.values()),
                mode="full",
                now=now,
            )
            try:
                output = await run_analysis(build_analysis_graph(deps), inp)
            except AnalysisPaused as e:
                output = e.output
                notes.append(f"{domain}: paused by LLM quota — some decisions not evaluated")
            for err in output.get("errors", []):
                notes.append(f"{domain}: [{err.stage}] {err.error_type}: {err.message}")
            outcomes = {o.service_id: o for o in output.get("outcomes", [])}
            llm_calls += sum(o.llm_calls for o in outcomes.values())
            analysed += 1
            for signals in store.signals.values():
                verified_total += len(signals)
            for rejected in store.rejected.values():
                rejected_by_reason.update(r.reason for r in rejected)

        for lbl in company_labels:
            bundle = bundles[lbl.service]
            q = questions[(lbl.service, lbl.question_key)]
            qid = str(q.id)
            outcome = outcomes.get(bundle.service_id)
            done = outcome is not None and outcome.status == "done"
            predicted = outcome.final_answers.get(qid, "not_evaluated") if done else "not_evaluated"
            key = (company_profile(case).id, bundle.service_id) if case else None
            decisions.append(
                Decision(
                    company_domain=domain,
                    service=lbl.service,
                    question_key=lbl.question_key,
                    category=q.category,
                    polarity=q.polarity,
                    expected=lbl.expected,
                    predicted=predicted,
                    evidence_hint=lbl.evidence_hint,
                    quotes=[s.quote for s in store.signals.get(key, []) if s.question_id == q.id],
                    rejected=dict(
                        Counter(r.reason for r in store.rejected.get(key, []) if r.question_id == q.id)
                    ),
                    auto_no=done and qid in outcome.auto_no,
                )
            )

    metrics = compute_metrics(decisions, verified_total, dict(rejected_by_reason), llm_calls, analysed)
    return EvalResult(
        prompt_version=PROMPT_VERSION, mode=mode, now=now, decisions=decisions, metrics=metrics, notes=notes
    )
