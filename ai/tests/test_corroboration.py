"""AI-15 / V5: reprints of one story are one event; independent sources raise its confidence."""

from datetime import date, timedelta

from leadradar_ai import score_company
from leadradar_ai.extraction import Answer, Evidence, ServiceExtraction
from leadradar_ai.scoring.corroboration import cluster_signals, same_event
from leadradar_ai.testing.factories import (
    NOW,
    make_bundle,
    make_company,
    make_profile,
    make_question,
    make_signal,
    make_snippet,
)
from leadradar_ai.verification import verify_extraction

HEADLINES = [
    "Lufthansa Group orders 20 Boeing 737 MAX 10s",
    "Lufthansa Group orders 20 Boeing 737 Max 10 jets",
    "Lufthansa Group To Grow Boeing 737 MAX Fleet By Securing 20 MAX 10 Orders",
]


def test_same_event_needs_similar_text_close_dates_and_no_contradicting_numbers():
    q = make_question()
    a = make_signal(q, quote=HEADLINES[0], url="https://a.example/1", event_date=NOW.date())
    b = make_signal(
        q, quote=HEADLINES[1], url="https://b.example/2", event_date=NOW.date() - timedelta(days=3)
    )
    assert same_event(a, b)
    assert not same_event(a, b.model_copy(update={"event_date": NOW.date() - timedelta(days=30)}))
    other_numbers = b.model_copy(
        update={"quote": "Lufthansa Group orders 10 Boeing 787 jets", "summary": "Orders 787s."}
    )
    assert not same_event(a, other_numbers)
    unrelated = make_signal(
        q, quote="Lufthansa Group launches Globe Cross logistics", summary="Launches Globe Cross."
    )
    assert not same_event(a, unrelated)
    assert not same_event(a, make_signal(make_question(), quote=HEADLINES[0]))  # other question


def test_cluster_confidence_counts_independent_sources():
    q = make_question()
    profile = make_profile()
    one_source = [make_signal(q, quote=HEADLINES[0], confidence=0.8) for _ in range(3)]
    (c,) = cluster_signals(one_source, profile, NOW)
    assert (c.sources, c.confidence, c.corroborated) == (1, 0.8, False)
    outlets = [
        make_signal(q, quote=h, confidence=0.6, url=f"https://o{i}.example/x")
        for i, h in enumerate(HEADLINES[:2])
    ]
    (c,) = cluster_signals(outlets, profile, NOW)
    assert c.sources == 2 and c.corroborated and abs(c.confidence - (1 - 0.4 * 0.4)) < 1e-9


def test_verify_flags_corroborated_and_caps_events_not_reprints():
    q = make_question(key="ia_fleet")
    bundle = make_bundle(questions=[q], scoring=make_profile(max_evidence_per_question=2))
    snippets = [
        make_snippet(id=f"S{i + 1}", text=h + ".", url=f"https://outlet{i}.example.com/a", source_type="news")
        for i, h in enumerate(HEADLINES)
    ]
    other = "Lufthansa Group launches Globe Cross to expand digital cross-border logistics"
    snippets.append(make_snippet(id="S4", text=other + ".", url="https://x.example/b", source_type="news"))
    lufthansa = make_company(name="Lufthansa Group", aliases=["Lufthansa"], domain="lufthansagroup.com")

    def evidence(sid, quote):
        return Evidence(
            snippet_id=sid,
            quote=quote,
            subject="target_company",
            event_date=date(2026, 9, 17),
            strength="moderate",
            summary="Orders jets." if sid != "S4" else "Launches Globe Cross.",
        )

    answer = Answer(
        question_id=str(q.id),
        answer="yes",
        confidence=0.8,
        evidence=[evidence("S1", HEADLINES[0]), evidence("S2", HEADLINES[1]), evidence("S4", other)],
        rationale="r",
    )
    ext = ServiceExtraction(answers={str(q.id): answer}, model="m")
    r = verify_extraction(ext, bundle, snippets, NOW, "extract_signals@v1", company=lufthansa)
    # cap = 2 events: both reprints survive (one event) together with the independent second event
    assert sorted(s.quote for s in r.signals) == sorted([HEADLINES[0], HEADLINES[1], other])
    flags = {s.quote: s.flags for s in r.signals}
    assert "corroborated" in flags[HEADLINES[0]] and "corroborated" in flags[HEADLINES[1]]
    assert "corroborated" not in flags[other]


def test_scoring_counts_a_reprinted_story_once():
    q = make_question()
    bundle = make_bundle(questions=[q])
    story = {"source_type": "news", "strength": "moderate", "confidence": 0.7, "event_date": NOW.date()}
    reprints = [
        make_signal(q, **story, quote=h, url=f"https://o{i}.example/a") for i, h in enumerate(HEADLINES)
    ]
    score = score_company(make_company(), bundle, reprints, NOW).breakdown[0]
    naive = 1 - (1 - 0.65 * 0.7 * 0.8) ** 3  # what three independent events would give
    assert score.strength < round(naive, 2)
    assert score.strength == round(0.65 * min(0.98, 1 - 0.3**3) * 0.8, 2)
    assert set(score.signal_ids) == {s.id for s in reprints}
