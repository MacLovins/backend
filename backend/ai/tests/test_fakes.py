import asyncio
import math
from datetime import timedelta
from uuid import uuid4

import pytest

from leadradar_ai import (
    AnalysisStore,
    ChunkIn,
    Collector,
    CollectRequest,
    Embedder,
    LLMCache,
    LLMCallRecord,
    ProgressEvent,
    ProgressSink,
    RejectedEvidence,
    UsageSink,
)
from leadradar_ai.testing import (
    FakeCollector,
    FakeEmbedder,
    InMemoryLLMCache,
    InMemoryStore,
    InMemoryUsageSink,
    ListProgressSink,
)
from leadradar_ai.testing.factories import NOW, make_company, make_document, make_question, make_signal


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    ("fake", "port"),
    [
        (FakeCollector([]), Collector),
        (InMemoryStore(), AnalysisStore),
        (ListProgressSink(), ProgressSink),
        (InMemoryLLMCache(), LLMCache),
        (InMemoryUsageSink(), UsageSink),
        (FakeEmbedder(), Embedder),
    ],
)
def test_fakes_implement_ports(fake, port):
    assert isinstance(fake, port)


def test_collector_filters_by_source_and_window():
    fresh = make_document(source_type="news")
    old = make_document(source_type="news", published=NOW - timedelta(days=400))
    job = make_document(source_type="jobs")
    collector = FakeCollector([fresh, old, job])
    docs = run(
        collector.collect(
            make_company(), CollectRequest(source_types={"news"}, since=NOW - timedelta(days=365))
        )
    )
    assert docs == [fresh]
    assert len(collector.collect_calls) == 1


def test_store_chunks_snippets_and_fingerprint():
    store, company = InMemoryStore(), make_company()
    doc = make_document(source_type="website", meta={"page_kind": "strategy"})
    store.add_documents(company.id, [doc])
    assert run(store.documents_without_chunks(company.id)) == [doc]

    chunk = ChunkIn(
        id=uuid4(),
        document_id=doc.id,
        company_id=company.id,
        ord=0,
        text=doc.text,
        char_start=0,
        char_end=len(doc.text),
        embedding=[0.1, 0.2],
    )
    run(store.save_chunks([chunk]))
    assert run(store.documents_without_chunks(company.id)) == []

    snippets = run(store.load_snippets(company.id, NOW - timedelta(days=30), {"website"}))
    assert [(s.id, s.chunk_id, s.meta) for s in snippets] == [("S1", chunk.id, {"page_kind": "strategy"})]
    assert run(store.load_snippets(company.id, NOW - timedelta(days=30), {"news"})) == []

    service_id = uuid4()
    assert run(store.get_fingerprint(company.id, service_id)) is None
    q = make_question()
    first = make_signal(q)
    run(store.save_extraction(uuid4(), company.id, service_id, "fp1", [first], []))
    run(
        store.save_extraction(
            uuid4(),
            company.id,
            service_id,
            "fp2",
            [make_signal(q, quote="other")],
            [RejectedEvidence(question_id=q.id, snippet_id="S3", quote="x", reason="quote_not_found")],
        )
    )
    assert run(store.get_fingerprint(company.id, service_id)) == "fp2"
    active = run(store.load_signals(company.id, service_id))
    assert [s.quote for s in active] == ["other"]
    assert [s.quote for s in store.superseded[(company.id, service_id)]] == [first.quote]


def test_usage_sink_counts_real_calls_only():
    sink = InMemoryUsageSink(preset={"m1": 5})
    ok = {"purpose": "extract_signals", "model": "m1", "prompt_version": "v1"}
    run(sink.record(LLMCallRecord(**ok, status="ok")))
    run(sink.record(LLMCallRecord(**ok, status="cache_hit", cache_hit=True)))
    assert run(sink.used_today("m1")) == 6
    assert run(sink.used_today("m2")) == 0


def test_cache_and_progress_sink():
    cache = InMemoryLLMCache()
    run(cache.set("k", {"answers": []}, {"model": "m1"}))
    assert run(cache.get("k")) == {"answers": []}
    assert run(cache.get("missing")) is None

    sink = ListProgressSink()
    run(
        sink.emit(
            ProgressEvent(
                run_id=uuid4(), company_id=uuid4(), stage="collecting", status="started", message=""
            )
        )
    )
    assert sink.stages() == [("collecting", "started")]


def test_fake_embedder_is_deterministic_and_semantic_enough():
    e = FakeEmbedder()
    q = e.embed_query("query: agentic AI automation")
    related, unrelated = e.embed_passages(["passage: DHL uses agentic AI automation", "quarterly dividend"])

    def cos(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert e.embed_query("query: agentic AI automation") == q
    assert math.isclose(cos(q, q), 1.0)
    assert cos(q, related) > cos(q, unrelated)
