from datetime import date, timedelta

import pytest
from leadradar_ai.extraction import Answer, Evidence, ServiceExtraction
from leadradar_ai.testing.factories import (
    NOW,
    make_bundle,
    make_company,
    make_profile,
    make_question,
    make_snippet,
)
from leadradar_ai.verification import find_quote, normalize, verify_extraction

PV = "extract_signals@v1"
TEXT = (
    "Under Strategy 2030 we are scaling AI across the Group. Since March 2026 agentic AI processes incoming "
    "customer RFQs in freight forwarding, cutting response times from two days to four hours. We also plan "
    "to cut 4,000 administrative roles by 2030."
)


# --- V1: quote matching -------------------------------------------------------------------------


def test_exact_quote_maps_to_original_offsets():
    m = find_quote("agentic AI processes incoming customer RFQs", TEXT)
    assert m is not None and not m.fuzzy
    assert TEXT[m.start : m.end] == "agentic AI processes incoming customer RFQs"


@pytest.mark.parametrize(
    "quote",
    [
        "AGENTIC ai processes   incoming\ncustomer RFQs",  # case and whitespace
        "“Since March 2026 agentic AI processes incoming customer RFQs”",  # smart quotes around
        "cutting response times from two days to four hours...",  # trailing ellipsis
    ],
)
def test_normalized_exact_matches(quote):
    m = find_quote(quote, TEXT)
    assert m is not None and not m.fuzzy


def test_dashes_quotes_and_ligatures_are_normalized():
    text = "The CIO said: „We will go ‘cloud‑first’ – no exceptions.“ Eﬃciency matters."
    assert (
        normalize(text) == "the cio said: \"we will go 'cloud-first' - no exceptions.\" efficiency matters."
    )
    m = find_quote("We will go 'cloud-first' - no exceptions", text)
    assert m is not None and not m.fuzzy
    assert text[m.start : m.end] == "We will go ‘cloud‑first’ – no exceptions"


def test_small_typo_is_a_fuzzy_match():
    m = find_quote("agentic AI proceses incomming customer RFQs", TEXT)
    assert m is not None and m.fuzzy and m.score >= 90
    assert "customer RFQs" in TEXT[m.start : m.end]


def test_changed_number_is_not_accepted_even_if_similar():
    assert find_quote("We also plan to cut 3,000 administrative roles by 2030", TEXT) is None
    assert find_quote("We also plan to cut 4,000 administrative roles by 2030", TEXT) is not None


@pytest.mark.parametrize(
    "quote",
    ["agentic AI", "We run a quantum computing lab in Berlin with 50 researchers.", ""],
)
def test_short_or_absent_quotes_are_rejected(quote):
    assert find_quote(quote, TEXT) is None


# --- verify_extraction --------------------------------------------------------------------------


def ev(quote="Since March 2026 agentic AI processes incoming customer RFQs", **overrides) -> Evidence:
    data = {
        "snippet_id": "S1",
        "quote": quote,
        "subject": "target_company",
        "event_date": date(2026, 3, 1),
        "strength": "strong",
        "summary": "Uses agentic AI for customer RFQs.",
    }
    return Evidence(**(data | overrides))


def extraction(bundle, answers: dict[int, tuple[str, list[Evidence], float]]) -> ServiceExtraction:
    return ServiceExtraction(
        answers={
            str(bundle.questions[i].id): Answer(
                question_id=str(bundle.questions[i].id), answer=a, confidence=c, evidence=e, rationale="r"
            )
            for i, (a, e, c) in answers.items()
        },
        model="gemini-test",
    )


@pytest.fixture
def setup():
    bundle = make_bundle(
        questions=[
            make_question(key="ia_ai_projects"),
            make_question(
                key="ia_inhouse", polarity="negative", category="internal_capability", recency_days=730
            ),
        ]
    )
    snippet = make_snippet(text=TEXT, char_start=500, char_end=500 + len(TEXT))
    return bundle, [snippet]


def verify(bundle, snippets, answers, company=None):
    return verify_extraction(extraction(bundle, answers), bundle, snippets, NOW, PV, company=company)


def test_valid_evidence_becomes_a_signal_with_document_offsets(setup):
    bundle, snippets = setup
    r = verify(bundle, snippets, {0: ("yes", [ev()], 0.9)})
    assert r.rejected == [] and len(r.signals) == 1
    s = r.signals[0]
    offset = TEXT.index("Since March")
    assert (s.quote_start, s.quote_end) == (500 + offset, 500 + offset + len(s.quote))
    assert s.quote == "Since March 2026 agentic AI processes incoming customer RFQs"
    assert (s.question_key, s.polarity, s.strength, s.confidence) == (
        "ia_ai_projects",
        "positive",
        "strong",
        0.9,
    )
    assert (s.document_id, s.chunk_id, s.url) == (
        snippets[0].document_id,
        snippets[0].chunk_id,
        snippets[0].url,
    )
    assert (s.reliability, s.model, s.prompt_version, s.flags) == (1.0, "gemini-test", PV, set())
    assert s.event_date == date(2026, 3, 1)
    assert r.final_answers[str(bundle.questions[0].id)] == "yes"


def test_negative_question_yields_negative_signal(setup):
    bundle, snippets = setup
    r = verify(
        bundle, snippets, {1: ("yes", [ev(quote="We also plan to cut 4,000 administrative roles")], 0.8)}
    )
    assert r.signals[0].polarity == "negative" and r.signals[0].question_key == "ia_inhouse"


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (ev(snippet_id="S9"), "quote_not_found"),
        (ev(quote="DHL opens an automation centre of excellence in Prague"), "quote_not_found"),
        (ev(subject="other_company"), "wrong_subject"),
        (ev(subject="industry_general"), "wrong_subject"),
        (ev(event_date=date(2024, 1, 1)), "stale"),
    ],
)
def test_rejections(setup, evidence, reason):
    bundle, snippets = setup
    r = verify(bundle, snippets, {0: ("yes", [evidence], 0.9)})
    assert r.signals == []
    assert [x.reason for x in r.rejected] == [reason]
    assert r.final_answers[str(bundle.questions[0].id)] == "unclear"  # V4


def test_low_confidence_and_fuzzy_penalty(setup):
    bundle, snippets = setup
    assert [x.reason for x in verify(bundle, snippets, {0: ("yes", [ev()], 0.4)}).rejected] == [
        "below_confidence"
    ]
    fuzzy = ev(quote="Since March 2026 agentic AI proceses incomming customer RFQs")
    r = verify(bundle, snippets, {0: ("yes", [fuzzy], 0.9)})
    assert r.signals[0].flags == {"fuzzy_quote"} and r.signals[0].confidence == pytest.approx(0.81)
    assert [x.reason for x in verify(bundle, snippets, {0: ("yes", [fuzzy], 0.55)}).rejected] == [
        "below_confidence"
    ]


def test_yes_without_evidence_becomes_unclear(setup):
    bundle, snippets = setup
    r = verify(bundle, snippets, {0: ("yes", [], 0.9)})
    assert r.signals == [] and r.final_answers[str(bundle.questions[0].id)] == "unclear"
    assert [x.reason for x in r.rejected] == ["no_evidence_for_yes"]


def test_evidence_of_no_and_unclear_is_ignored(setup):
    bundle, snippets = setup
    r = verify(bundle, snippets, {0: ("unclear", [ev()], 0.9), 1: ("no", [ev()], 0.9)})
    assert r.signals == [] and r.rejected == []
    assert list(r.final_answers.values()) == ["unclear", "no"]


def test_missing_answer_is_unclear(setup):
    bundle, snippets = setup
    r = verify(bundle, snippets, {})
    assert set(r.final_answers.values()) == {"unclear"}


def test_future_event_date_falls_back_to_published(setup):
    bundle, snippets = setup
    r = verify(bundle, snippets, {0: ("yes", [ev(event_date=date(2030, 12, 31))], 0.9)})
    assert r.signals[0].event_date == NOW.date()


def test_llm_date_cannot_be_later_than_the_article(setup):
    # an old article with an invented fresh date: capped by published_at, so it is stale, not fresh
    bundle, _ = setup
    old = [make_snippet(text=TEXT, published_at=NOW - timedelta(days=500))]
    r = verify(bundle, old, {0: ("yes", [ev(event_date=NOW.date())], 0.9)})
    assert r.signals == [] and [x.reason for x in r.rejected] == ["stale"]
    # an earlier event reported later keeps its own (older) date
    recent = [make_snippet(text=TEXT, published_at=NOW - timedelta(days=10))]
    r = verify(bundle, recent, {0: ("yes", [ev(event_date=date(2026, 3, 1))], 0.9)})
    assert r.signals[0].event_date == date(2026, 3, 1)


def test_third_party_quote_needs_the_company_named_near_it(setup):
    bundle, _ = setup
    company = make_company(name="Orange", domain="orange.com", aliases=[])
    fruit = "Citrus markets: orange prices rose 12% after the frost, and juice makers raised their forecasts."
    operator = "Orange said on Monday that agentic AI now handles 30% of customer care requests in France."
    snippets = [
        make_snippet(
            id="S1", text=fruit, source_type="news", url="https://news.example.com/a", title="Markets"
        ),
        make_snippet(
            id="S2", text=operator, source_type="news", url="https://news.example.com/b", title="Tech"
        ),
    ]
    answers = {
        0: (
            "yes",
            [
                ev(snippet_id="S1", quote="orange prices rose 12% after the frost"),
                ev(snippet_id="S2", quote="agentic AI now handles 30% of customer care requests"),
            ],
            0.9,
        )
    }
    r = verify(bundle, snippets, answers, company=company)
    assert [s.quote for s in r.signals] == ["agentic AI now handles 30% of customer care requests"]
    assert [x.reason for x in r.rejected] == ["wrong_subject"]
    # the company's own site needs no mention
    own = [make_snippet(id="S1", text=fruit, url="https://www.orange.com/en/news")]
    r = verify(
        bundle, own, {0: ("yes", [ev(quote="orange prices rose 12% after the frost")], 0.9)}, company=company
    )
    assert len(r.signals) == 1


def test_undated_snippet_uses_fetched_at_and_is_flagged(setup):
    bundle, _ = setup
    undated = [make_snippet(text=TEXT, published_at=None)]
    r = verify(bundle, undated, {0: ("yes", [ev(event_date=None)], 0.9)})
    assert r.signals[0].flags == {"undated"} and r.signals[0].event_date is None
    stale = [make_snippet(text=TEXT, published_at=None, fetched_at=NOW - timedelta(days=400))]
    assert [x.reason for x in verify(bundle, stale, {0: ("yes", [ev(event_date=None)], 0.9)}).rejected] == [
        "stale"
    ]


def test_quote_from_title_and_headline_only_documents(setup):
    bundle, _ = setup
    snippets = [
        make_snippet(
            text=TEXT,
            source_type="news",
            source_name="gdelt",
            title="Nordfracht appoints new Chief Automation Officer",
            meta={"headline_only": True},
        )
    ]
    r = verify(
        bundle, snippets, {0: ("yes", [ev(quote="Nordfracht appoints new Chief Automation Officer")], 0.9)}
    )
    s = r.signals[0]
    assert (s.quote_start, s.quote_end) == (None, None)
    assert s.flags == {"headline_only"} and s.reliability == 0.6


def test_one_document_is_one_story_and_the_strongest_quote_wins(setup):
    bundle, snippets = setup
    evidence = [
        ev(strength="weak", quote="Under Strategy 2030 we are scaling AI across the Group"),
        ev(),
        ev(),
    ]
    r = verify(bundle, snippets, {0: ("yes", evidence, 0.9)})
    assert [s.strength for s in r.signals] == ["strong"]
    assert "corroborated" in r.signals[0].flags


def test_reprints_collapse_and_cap_keeps_strongest_stories(setup):
    bundle, _ = setup
    bundle = bundle.model_copy(update={"scoring": make_profile(max_evidence_per_question=2)})
    reprint = "DHL Group rolls out agentic AI for customer RFQs in freight forwarding"
    other = "DHL Group opens a shared service centre for finance operations in Krakow"
    snippets = [
        make_snippet(id="S1", text=reprint, source_type="news"),
        make_snippet(id="S2", text=reprint + ".", source_type="news"),
        make_snippet(id="S3", text=other, source_type="news"),
    ]
    evidence = [
        ev(snippet_id="S1", quote=reprint),
        ev(snippet_id="S2", quote=reprint),
        ev(snippet_id="S3", quote=other, strength="weak"),
    ]
    r = verify(bundle, snippets, {0: ("yes", evidence, 0.9)})
    assert [s.strength for s in r.signals] == ["strong", "weak"]
    assert "corroborated" in r.signals[0].flags
