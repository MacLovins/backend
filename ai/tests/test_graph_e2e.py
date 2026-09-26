import asyncio
import re
from datetime import timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from leadradar_ai import AnalysisInput, LLMBadRequest, QuotaExhausted
from leadradar_ai.errors import AnalysisPaused
from leadradar_ai.pipeline import AnalysisDeps, build_analysis_graph, build_collect_request, run_analysis
from leadradar_ai.retrieval import PrefilterConfig
from leadradar_ai.testing import FakeCollector, FakeEmbedder, FakeLLM, InMemoryStore, ListProgressSink
from leadradar_ai.testing.factories import NOW, make_bundle, make_company, make_document, make_question

SNIPPET = re.compile(r'<snippet id="(S\d+)"[^>]*>(.*?)</snippet>', re.DOTALL)
QUESTION = re.compile(r'<question id="(Q\d+)"[^>]*>(.*?)</question>', re.DOTALL)
QUESTION_WORDS = {"ai": "agentic AI", "hiring": "RPA", "incident": "ransomware"}


def smart_answers(request):
    """Answers "yes" with a verbatim quote when a shown snippet contains the question's topic word."""
    user = request.user.split("</examples>")[-1]
    snippets = SNIPPET.findall(user)
    answers = []
    for qid, text in QUESTION.findall(user):
        topic = next((w for k, w in QUESTION_WORDS.items() if f"[{k}]" in text), None)
        hit = next(((sid, body) for sid, body in snippets if topic and topic in body), None)
        if hit:
            sid, body = hit
            start = body.index(topic)
            quote = body[max(0, start - 20) : start + 40].strip()
            answers.append(
                {
                    "question_id": qid,
                    "answer": "yes",
                    "confidence": 0.9,
                    "rationale": "found",
                    "evidence": [
                        {
                            "snippet_id": sid,
                            "quote": quote,
                            "subject": "target_company",
                            "event_date": None,
                            "strength": "strong",
                            "summary": f"Mentions {topic}.",
                        }
                    ],
                }
            )
        else:
            answers.append(
                {
                    "question_id": qid,
                    "answer": "unclear",
                    "confidence": 0.5,
                    "rationale": "none",
                    "evidence": [],
                }
            )
    return {"answers": answers}


def docs():
    return [
        make_document(
            source_type="website",
            source_name="website",
            url="https://www.dhl.com/strategy",
            title="Strategy 2030",
            text="Under Strategy 2030 DHL uses agentic AI to process customer RFQs in forwarding.",
        ),
        make_document(
            source_type="jobs",
            source_name="workday",
            url="https://dhl.wd3.myworkdayjobs.com/j/1",
            title="RPA Developer",
            text="We are hiring an RPA developer to automate finance processes.",
        ),
        make_document(
            url="https://news.example.com/x",
            title="DHL hit by ransomware",
            text="DHL Group confirmed a ransomware attack on a regional parcel hub in 2026.",
        ),
    ]


def services():
    ia = make_bundle(
        key="intelligent_automation",
        questions=[
            make_question(
                key="ia_ai_projects",
                text="[ai] Is the company running agentic AI initiatives?",
                keywords={"en": ["agentic AI"]},
            ),
            make_question(
                key="ia_hiring",
                text="[hiring] Is the company hiring RPA developers?",
                category="hiring",
                source_types={"jobs"},
                recency_days=90,
                job_titles=["RPA developer"],
            ),
        ],
    )
    cyber = make_bundle(
        key="cybersecurity",
        name="Cybersecurity",
        questions=[
            make_question(
                key="cy_incident",
                text="[incident] Has the company suffered a ransomware attack?",
                category="incident",
                source_types={"news"},
                recency_days=540,
                keywords={"en": ["ransomware"]},
            ),
        ],
    )
    return [ia, cyber]


class World:
    def __init__(self, llm=None, collector_kwargs=None, checkpointer=None):
        self.company = make_company(own_domains=["dhl.com", "dhl.wd3.myworkdayjobs.com"])
        self.store = InMemoryStore()
        self.collector = FakeCollector(docs(), store=self.store, **(collector_kwargs or {}))
        self.progress = ListProgressSink()
        self.llm = llm or FakeLLM(smart_answers)
        self.services = services()
        self.deps = AnalysisDeps(
            collector=self.collector,
            store=self.store,
            progress=self.progress,
            llm=self.llm,
            embedder=FakeEmbedder(),
            checkpointer=checkpointer,
            prefilter=PrefilterConfig(min_cosine=0.5),
            retry_backoff_s=0,
        )
        self.graph = build_analysis_graph(self.deps)

    def input(self, run_id=None, mode="incremental"):
        return AnalysisInput(
            run_id=run_id or uuid4(), company=self.company, services=self.services, mode=mode, now=NOW
        )

    def run(self, inp):
        return asyncio.run(run_analysis(self.graph, inp))


def test_full_run_produces_scores_signals_and_progress():
    w = World()
    out = w.run(w.input())

    assert {s.service_id for s in out["scores"]} == {s.service_id for s in w.services}
    assert out["errors"] == []
    stats = out["stats"]
    assert stats.llm_calls == 2  # one call per service
    assert stats.documents_by_source == {"website": 1, "jobs": 1, "news": 1}
    assert stats.signals_verified == 3 and stats.evidence_rejected == 0
    assert set(stats.duration_ms_by_stage) >= {
        "resolving",
        "collecting",
        "indexing",
        "prefiltering",
        "extracting",
        "verifying",
        "scoring",
    }

    ia, cyber = w.services
    ia_signals = asyncio.run(w.store.load_signals(w.company.id, ia.service_id))
    assert {s.question_key for s in ia_signals} == {"ia_ai_projects", "ia_hiring"}
    assert all(s.quote_start is not None for s in ia_signals)
    ia_score = next(s for s in out["scores"] if s.service_id == ia.service_id)
    assert ia_score.intent > 0 and ia_score.why_now

    stages = w.progress.stages()
    assert stages[:6] == [
        ("resolving", "started"),
        ("resolving", "done"),
        ("collecting", "started"),
        ("collecting", "done"),
        ("indexing", "started"),
        ("indexing", "done"),
    ]
    assert stages[-1] == ("done", "done")
    per_service = [(e.stage, e.status) for e in w.progress.events if e.service_id == cyber.service_id]
    assert per_service == [
        ("prefiltering", "started"),
        ("prefiltering", "done"),
        ("extracting", "started"),
        ("extracting", "done"),
        ("verifying", "started"),
        ("verifying", "done"),
        ("scoring", "started"),
        ("scoring", "done"),
    ]


def test_second_incremental_run_skips_the_llm():
    w = World()
    w.run(w.input())
    calls = len(w.llm.calls)
    out = w.run(w.input())
    assert len(w.llm.calls) == calls
    assert out["stats"].llm_calls == 0 and out["stats"].extraction_skipped_services == 2
    assert len(out["scores"]) == 2 and all(s.intent > 0 for s in out["scores"])
    assert out["stats"].snippets_indexed == 0  # nothing new to index


def test_full_mode_extracts_again():
    w = World()
    w.run(w.input())
    calls = len(w.llm.calls)
    w.run(w.input(mode="full"))
    assert len(w.llm.calls) == calls + 2


def test_quota_exhausted_pauses_one_service_and_rerun_continues():
    state = {"quota": True}

    def llm_handler(request):
        if "ransomware" in request.user.split("</examples>")[-1] and state["quota"]:
            return QuotaExhausted("main")
        return smart_answers(request)

    w = World(llm=FakeLLM(llm_handler), checkpointer=InMemorySaver())
    inp = w.input()
    with pytest.raises(AnalysisPaused) as exc:
        w.run(inp)
    ia, cyber = w.services
    assert isinstance(exc.value, QuotaExhausted)  # core keeps catching QuotaExhausted
    assert exc.value.paused_service_ids == [str(cyber.service_id)]
    assert [s.service_id for s in exc.value.output["scores"]] == [ia.service_id]  # the other service finished
    assert (w.company.id, ia.service_id) in w.store.scores
    assert (w.company.id, cyber.service_id) not in w.store.scores
    assert w.progress.events[-1].stage == "paused"

    state["quota"] = False
    llm_calls = len(w.llm.calls)
    out = w.run(inp)
    assert {s.service_id for s in out["scores"]} == {ia.service_id, cyber.service_id}
    assert out["stats"].extraction_skipped_services == 1  # IA: fingerprint unchanged, no LLM
    assert len(w.llm.calls) == llm_calls + 1 and out["stats"].llm_calls == 1


class Crash(RuntimeError):
    """Not retried by the index RetryPolicy and not caught: the graph run dies like a killed worker."""


def test_crash_resumes_from_checkpoint_without_repeating_finished_steps():
    w = World(checkpointer=InMemorySaver())
    inp = w.input()
    original = w.store.documents_without_chunks
    calls = {"n": 0}

    async def dying_index(company_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Crash("worker killed")
        return await original(company_id)

    w.store.documents_without_chunks = dying_index
    with pytest.raises(Crash):
        w.run(inp)
    resolves, collects = len(w.collector.resolve_calls), len(w.collector.collect_calls)
    out = w.run(inp)
    assert len(out["scores"]) == 2
    assert (len(w.collector.resolve_calls), len(w.collector.collect_calls)) == (resolves, collects)


def test_error_inside_a_service_step_is_contained():
    w = World()
    original = w.store.save_score

    async def broken_save(run_id, score):
        if score.service_id == w.services[1].service_id:
            raise Crash("db down")
        return await original(run_id, score)

    w.store.save_score = broken_save
    out = w.run(w.input())
    assert [(e.stage, e.error_type) for e in out["errors"]] == [("scoring", "Crash")]
    assert len(out["scores"]) == 1


def test_failure_in_one_service_does_not_break_the_other():
    def llm_handler(request):
        if "ransomware" in request.user.split("</examples>")[-1]:
            return LLMBadRequest("400 INVALID_ARGUMENT")
        return smart_answers(request)

    w = World(llm=FakeLLM(llm_handler))
    out = w.run(w.input())
    ia, cyber = w.services
    assert [s.service_id for s in out["scores"]] == [ia.service_id]
    assert [(e.stage, e.service_id, e.error_type) for e in out["errors"]] == [
        ("extracting", cyber.service_id, "LLMBadRequest")
    ]
    assert any(o.status == "failed" and o.service_id == cyber.service_id for o in out["outcomes"])
    assert ("failed", "failed") in w.progress.stages()


def test_collect_and_resolve_failures_degrade_to_stored_data():
    w = World(
        collector_kwargs={
            "fail_collect": ConnectionError("GDELT down"),
            "fail_resolve": TimeoutError("site timeout"),
        }
    )
    w.store.add_documents(w.company.id, docs())  # collected by an earlier run
    out = w.run(w.input())
    assert len(out["scores"]) == 2
    assert [e.stage for e in out["errors"]] == ["resolving", "collecting"]
    assert len(w.collector.resolve_calls) == 3 and len(w.collector.collect_calls) == 2  # retried
    assert out["stats"].signals_verified == 3


def test_checkpoint_state_holds_no_document_texts():
    w = World(checkpointer=InMemorySaver())
    inp = w.input()
    w.run(inp)
    snapshot = asyncio.run(
        w.graph.aget_state({"configurable": {"thread_id": f"{inp.run_id}:{inp.company.id}"}})
    )
    assert "agentic AI to process customer RFQs" not in repr(snapshot.values)


def test_build_collect_request():
    request = build_collect_request(services(), NOW, max_items=30)
    assert request.source_types == {"news", "website", "report", "jobs"}
    assert request.since == NOW - timedelta(days=540)
    assert request.news_topics == ["agentic AI", "ransomware"]
    assert request.job_keywords == ["RPA developer"]
    assert request.max_items_per_source == 30


def test_derived_signals_are_persisted_and_breakdown_ids_resolve():
    """AI-16: NIS2/DORA signals go through AnalysisStore.save_extraction like extracted ones."""
    w = World()
    compliance = make_question(
        key="cy_compliance",
        text="[compliance] Is the company preparing for NIS2 or DORA?",
        category="compliance",
        source_types={"news"},
        recency_days=540,
    )
    cyber = w.services[1]
    w.services[1] = cyber.model_copy(update={"questions": [*cyber.questions, compliance]})
    w.company = w.company.model_copy(update={"industry_ids": ["logistics", "banking"]})  # DE: NIS2 + DORA
    out = w.run(w.input())

    stored = asyncio.run(w.store.load_signals(w.company.id, cyber.service_id))
    derived = [s for s in stored if s.source_type == "derived"]
    assert {s.source_name for s in derived} == {"NIS2 scope (firmographics)", "DORA scope (firmographics)"}
    assert all(s.flags == {"derived"} and s.question_key == "cy_compliance" for s in derived)

    score = next(s for s in out["scores"] if s.service_id == cyber.service_id)
    ids = {i for c in score.breakdown for i in c.signal_ids}
    assert {s.id for s in derived} <= ids <= {s.id for s in stored}  # no id points nowhere

    # a rerun without anything new skips extraction and keeps the same persisted ids
    again = w.run(w.input())
    rescored = next(s for s in again["scores"] if s.service_id == cyber.service_id)
    assert {i for c in rescored.breakdown for i in c.signal_ids} == ids

    # a firmographic change that changes the derived signals is not skipped
    w.company = w.company.model_copy(update={"industry_ids": ["logistics"]})
    w.run(w.input())
    stored = asyncio.run(w.store.load_signals(w.company.id, cyber.service_id))
    assert {s.source_name for s in stored if s.source_type == "derived"} == {"NIS2 scope (firmographics)"}
