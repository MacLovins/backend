import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from leadradar_ai.retrieval import (
    PrefilterConfig,
    chunk_document,
    compute_fingerprint,
    load_window,
    prefilter,
)
from leadradar_ai.retrieval.bm25 import BM25
from leadradar_ai.retrieval.chunking import index_text
from leadradar_ai.retrieval.entity import mention_pattern, name_variants
from leadradar_ai.testing import FakeEmbedder, InMemoryStore
from leadradar_ai.testing.factories import NOW, make_bundle, make_company, make_document, make_question

PROMPT = "extract_signals@v1"
CONFIG = PrefilterConfig(min_cosine=0.5)  # FakeEmbedder cosines are much lower than e5's


def snippets_for(company, docs):
    """Index documents like the index node does and load them back as snippets."""
    store, embedder = InMemoryStore(), FakeEmbedder()
    store.add_documents(company.id, docs)
    chunks = [c for d in docs for c in chunk_document(d, company.id)]
    by_id = {d.id: d for d in docs}
    vectors = embedder.embed_passages(
        [index_text(c.text, by_id[c.document_id].title, by_id[c.document_id].source_type) for c in chunks]
    )
    chunks = [c.model_copy(update={"embedding": v}) for c, v in zip(chunks, vectors, strict=True)]
    asyncio.run(store.save_chunks(chunks))
    return asyncio.run(
        store.load_snippets(
            company.id,
            NOW - timedelta(days=3650),
            {"news", "website", "jobs", "report", "registry", "incident"},
        )
    )


@pytest.fixture
def dhl():
    company = make_company(own_domains=["dhl.com", "dhl.wd3.myworkdayjobs.com"])
    q_ai = make_question(
        key="ia_ai_projects",
        weight="high",
        keywords={"en": ["agentic AI", "RPA", "automation"], "de": ["Automatisierung", "KI"]},
    )
    q_hiring = make_question(
        key="ia_hiring",
        weight="high",
        category="hiring",
        source_types={"jobs"},
        recency_days=90,
        job_titles=["RPA developer", "automation engineer"],
        negative_terms=["internship"],
    )
    q_leaders = make_question(
        key="ia_leaders",
        weight="medium",
        category="leadership_change",
        source_types={"news"},
        text="Was a new COO or CIO appointed?",
    )
    docs = {
        "strategy": make_document(
            source_type="website",
            source_name="website",
            url="https://www.dhl.com/strategy-2030",
            title="Strategy 2030",
            text="Our Strategy 2030 puts agentic AI and automation at the core. "
            "We already use agentic AI to process customer RFQs and automate operational communication.",
        ),
        "news_dhl": make_document(
            url="https://news.example.com/a",
            title="DHL scales agentic AI",
            text="The logistics group expands automation with agentic AI across customer service.",
        ),
        "news_other": make_document(
            url="https://news.example.com/b",
            title="Kuehne+Nagel bets on automation",
            text="Kuehne+Nagel expands agentic AI and RPA automation across its operations.",
        ),
        "news_old": make_document(
            url="https://news.example.com/c",
            title="DHL pilots RPA",
            published=NOW - timedelta(days=400),
            text="DHL pilots RPA automation in finance.",
        ),
        "news_dividend": make_document(
            url="https://news.example.com/d",
            title="DHL raises dividend",
            text="DHL Group raises its dividend to 1.85 euro per share after a strong year.",
        ),
        "job_rpa": make_document(
            source_type="jobs",
            source_name="workday",
            url="https://dhl.wd3.myworkdayjobs.com/job/1",
            title="RPA Developer (m/f/d)",
            text="Build RPA automation with UiPath for finance processes.",
        ),
        "job_intern": make_document(
            source_type="jobs",
            source_name="workday",
            url="https://dhl.wd3.myworkdayjobs.com/job/2",
            title="Internship automation",
            text="Internship in RPA automation for students.",
        ),
    }
    bundle = make_bundle(questions=[q_ai, q_hiring, q_leaders])
    return company, bundle, docs


def selected_urls(result):
    return {s.url for s in result.snippets}


def test_dhl_selection(dhl):
    company, bundle, docs = dhl
    snippets = snippets_for(company, list(docs.values()))
    result = prefilter(company, bundle, snippets, FakeEmbedder(), NOW, PROMPT, CONFIG)
    q_ai, q_hiring, q_leaders = bundle.questions

    urls = selected_urls(result)
    assert docs["strategy"].url in urls  # own domain, no name mention needed
    assert docs["news_dhl"].url in urls  # third party, name in title
    assert docs["news_other"].url not in urls  # entity filter: another company
    assert docs["news_old"].url not in urls  # older than recency_days
    assert docs["job_intern"].url not in urls  # negative term
    assert docs["job_rpa"].url in urls

    by_id = {s.id: s for s in result.snippets}
    assert {by_id[i].source_type for i in result.candidates[str(q_hiring.id)]} == {"jobs"}
    assert all(by_id[i].source_type != "jobs" for i in result.candidates[str(q_ai.id)])
    assert result.no_candidates == [str(q_leaders.id)]  # nothing about leadership in news
    assert result.needs_llm
    assert result.stats["after_entity_filter"] < result.stats["loaded"]


def test_snippets_are_relabelled_and_slim(dhl):
    company, bundle, docs = dhl
    result = prefilter(
        company, bundle, snippets_for(company, list(docs.values())), FakeEmbedder(), NOW, PROMPT, CONFIG
    )
    assert [s.id for s in result.snippets] == [f"S{n}" for n in range(1, len(result.snippets) + 1)]
    assert all(s.embedding is None for s in result.snippets)
    ids = {s.id for s in result.snippets}
    assert all(set(c) <= ids for c in result.candidates.values())


def test_dividend_news_is_not_a_candidate_for_ai(dhl):
    company, bundle, docs = dhl
    result = prefilter(
        company, bundle, snippets_for(company, [docs["news_dividend"]]), FakeEmbedder(), NOW, PROMPT, CONFIG
    )
    assert str(bundle.questions[0].id) not in result.candidates
    assert not result.needs_llm


def test_at_most_two_snippets_per_document_and_topk():
    company = make_company()
    q = make_question(keywords={"en": ["automation"]})
    long_text = " ".join(
        f"DHL automation program step {i} improves automation of process {i}." for i in range(200)
    )
    docs = [
        make_document(url=f"https://news.example.com/{i}", title="DHL automation", text=long_text)
        for i in range(5)
    ]
    result = prefilter(
        company, make_bundle(questions=[q]), snippets_for(company, docs), FakeEmbedder(), NOW, PROMPT, CONFIG
    )
    chosen = result.candidates[str(q.id)]
    assert len(chosen) == CONFIG.topk_per_question
    by_id = {s.id: s for s in result.snippets}
    per_doc = {}
    for i in chosen:
        per_doc[by_id[i].document_id] = per_doc.get(by_id[i].document_id, 0) + 1
    assert max(per_doc.values()) <= 2


def test_budget_drops_lowest_but_keeps_protected():
    company = make_company()
    questions = [
        make_question(key=f"q{i}", weight="high" if i == 0 else "low", keywords={"en": [f"topic{i}"]})
        for i in range(4)
    ]
    docs = [
        make_document(
            url=f"https://dhl.com/{q.key}/{j}",
            source_type="website",
            title=None,
            text=f"DHL works on topic{i} automation initiative number {j}.",
        )
        for i, q in enumerate(questions)
        for j in range(5)
    ]
    config = PrefilterConfig(min_cosine=0.5, max_snippets=6)
    result = prefilter(
        company,
        make_bundle(questions=questions),
        snippets_for(company, docs),
        FakeEmbedder(),
        NOW,
        PROMPT,
        config,
    )
    assert len(result.snippets) == 6
    assert len(result.candidates[str(questions[0].id)]) >= 2  # high weight keeps two
    assert all(len(result.candidates[str(q.id)]) >= 1 for q in questions)
    assert result.stats["candidates_before_budget"] > 6


def test_fingerprint_changes_only_when_inputs_change(dhl):
    company, bundle, docs = dhl
    snippets = snippets_for(company, list(docs.values()))
    first = prefilter(company, bundle, snippets, FakeEmbedder(), NOW, PROMPT, CONFIG)
    again = prefilter(company, bundle, list(reversed(snippets)), FakeEmbedder(), NOW, PROMPT, CONFIG)
    assert first.fingerprint == again.fingerprint
    assert (
        prefilter(company, bundle, snippets, FakeEmbedder(), NOW, "extract_signals@v2", CONFIG).fingerprint
        != first.fingerprint
    )
    bumped = bundle.model_copy(
        update={"questions": [bundle.questions[0].model_copy(update={"version": 2}), *bundle.questions[1:]]}
    )
    assert (
        prefilter(company, bumped, snippets, FakeEmbedder(), NOW, PROMPT, CONFIG).fingerprint
        != first.fingerprint
    )
    new_doc = make_document(
        url="https://dhl.com/new", source_type="website", text="DHL launches agentic AI hub."
    )
    more = snippets + snippets_for(company, [new_doc])
    assert (
        prefilter(company, bundle, more, FakeEmbedder(), NOW, PROMPT, CONFIG).fingerprint != first.fingerprint
    )


def test_fingerprint_helper_is_order_independent():
    qs = [make_question(), make_question()]
    a, b = str(uuid4()), str(uuid4())
    assert compute_fingerprint(qs, [a, b], PROMPT) == compute_fingerprint(list(reversed(qs)), [b, a], PROMPT)


def test_empty_input():
    bundle = make_bundle()
    result = prefilter(make_company(), bundle, [], FakeEmbedder(), NOW, PROMPT, CONFIG)
    assert (result.snippets, result.candidates, result.needs_llm) == ([], {}, False)
    assert result.no_candidates == [str(bundle.questions[0].id)]


def test_load_window(dhl):
    _, bundle, _ = dhl
    since, sources = load_window(bundle, NOW)
    assert since == NOW - timedelta(days=365)
    assert sources == {"news", "website", "report", "jobs"}


# --- entity matching and BM25 -------------------------------------------------------------------


def test_name_variants_and_mention_pattern():
    company = make_company(name="Deutsche Lufthansa AG", aliases=["Lufthansa Group", "LH"])
    assert name_variants(company) == [
        "Deutsche Lufthansa AG",
        "Deutsche Lufthansa",
        "Lufthansa Group",
        "Lufthansa",
        "LH",
    ]
    p = mention_pattern(company)
    assert p.search("news: lufthansa cuts 4,000 admin jobs")
    assert not p.search("the lufthansas of this world") and not p.search("lhr airport")

    orange = mention_pattern(make_company(name="Orange SA", aliases=[]))
    assert orange.search("orange launches a soc") and not orange.search("the orangery restaurant")
    nestle = mention_pattern(make_company(name="Nestlé", aliases=[]))
    assert nestle.search("nestle opens a shared service center")


def test_bm25_ranks_matching_and_folds_diacritics():
    bm = BM25(["Automatisierung in München", "Dividende steigt", "RPA automation automation"])
    s = bm.scores("munchen automatisierung")
    assert s[0] > 0 and s[1] == 0
    s = bm.scores("automation")
    assert s[2] > 0 and s[0] == 0
    assert BM25([]).scores("x") == []
    # stopwords of the question do not make every snippet a match
    assert BM25(["The dividend rises for the company"]).scores("Is the company running RPA?") == [0.0]
