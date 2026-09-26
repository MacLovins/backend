import math
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from leadradar_ai import (
    Criterion,
    ICPConfig,
    RuleConfig,
    evaluate_rules,
    fit_score,
    lead_sort_key,
    score_company,
)
from leadradar_ai.scoring import combine_priority, noisy_or, saturate
from leadradar_ai.scoring.decay import decay_factor, evidence_value, reliability_of
from leadradar_ai.testing.factories import (
    NOW,
    make_bundle,
    make_company,
    make_profile,
    make_question,
    make_signal,
)

DAY = timedelta(days=1)


def days_ago(n: int):
    return (NOW - n * DAY).date()


def rule(kind: str, condition: dict, action: str = "exclude", cap_value: float | None = None) -> RuleConfig:
    return RuleConfig(
        id=uuid4(), name=f"{kind} rule", kind=kind, condition=condition, action=action, cap_value=cap_value
    )


# --- evidence value -------------------------------------------------------------------------------


def test_decay_halves_at_half_life_and_ignores_registry():
    profile, q = make_profile(), make_question()
    assert decay_factor(make_signal(q, source_type="jobs", event_date=days_ago(45)), profile, NOW) == 0.5
    assert decay_factor(make_signal(q, source_type="news", event_date=days_ago(240)), profile, NOW) == 0.25
    assert decay_factor(make_signal(q, source_type="registry", event_date=days_ago(999)), profile, NOW) == 1.0
    # future event date (a target year slipped through) is clamped to age 0
    assert decay_factor(make_signal(q, source_type="news", event_date=days_ago(-30)), profile, NOW) == 1.0
    # event_date wins over published_at; published_at is the fallback
    assert (
        decay_factor(
            make_signal(q, source_type="news", event_date=None, published_at=NOW - 120 * DAY), profile, NOW
        )
        == 0.5
    )


def test_undated_signals_decay_from_fetch_date_with_a_minimum_age():
    profile, q = make_profile(undated_age_days=90), make_question()
    undated = {"source_type": "news", "event_date": None, "published_at": None, "flags": {"undated"}}
    # fetched today: assumed 90 days old, not "today" (news half-life 120)
    fresh = make_signal(q, **undated, fetched_at=NOW)
    assert decay_factor(fresh, profile, NOW) == pytest.approx(0.5 ** (90 / 120))
    # fetched 240 days ago: the document age wins
    old = make_signal(q, **undated, fetched_at=NOW - 240 * DAY)
    assert decay_factor(old, profile, NOW) == 0.25
    # a stored signal without fetched_at falls back to detected_at
    stored = make_signal(q, **undated, detected_at=NOW - 360 * DAY)
    assert decay_factor(stored, profile, NOW) == 0.125
    # undated evidence never outweighs the same evidence dated today
    dated = make_signal(q, source_type="news", event_date=NOW.date())
    assert evidence_value(fresh, profile, NOW) < evidence_value(dated, profile, NOW)
    assert "undated" in fresh.flags  # the data-gap flag survives scoring


def test_reliability_comes_from_profile_and_headline_only_downgrades():
    q = make_question()
    assert reliability_of(make_signal(q, source_type="news"), make_profile()) == 0.8
    assert reliability_of(make_signal(q, source_type="news", flags={"headline_only"}), make_profile()) == 0.6
    custom = make_profile(reliability={"news": 0.5})
    assert reliability_of(make_signal(q, source_type="news"), custom) == 0.5
    assert reliability_of(make_signal(q, source_type="jobs", reliability=0.77), custom) == 0.77  # fallback


def test_evidence_value_formula():
    # moderate (0.65) × confidence 0.8 × news (0.8) × one half-life (0.5) = 0.208
    s = make_signal(
        make_question(), source_type="news", strength="moderate", confidence=0.8, event_date=days_ago(120)
    )
    assert evidence_value(s, make_profile(), NOW) == pytest.approx(0.208)


def test_aggregation_helpers():
    assert noisy_or([]) == 0.0
    assert noisy_or([0.5, 0.5]) == pytest.approx(0.75)
    assert noisy_or([1.0, 0.3]) == 1.0
    assert saturate(0, 3) == 0.0
    assert saturate(3, 3) == pytest.approx(100 * (1 - math.exp(-1)))


def test_dhl_breakdown_from_spec():
    # SPEC §1.7.5 example: points 2.49 + 1.22 + 1.32 positive, 0.96 negative, fit 92.
    # The spec prints priority 69.2, but the formula gives 69.149 → 69.1 (intent and risk match exactly).
    # (printed with the original τ_intent = 3; the default is now 5 — see test_weight_change_flips_a_tier)
    profile = make_profile(tau_intent=3.0)
    intent, risk = saturate(2.49 + 1.22 + 1.32, profile.tau_intent), saturate(0.96, profile.tau_risk)
    assert round(intent, 1) == 81.3
    assert round(risk, 1) == 38.1
    assert round(combine_priority(92.0, intent, risk, profile), 1) == 69.1


# --- score_company: golden scenario ---------------------------------------------------------------


@pytest.fixture
def ia():
    q_ai = make_question(key="ia_ai_projects", weight="high")
    q_dt = make_question(key="ia_dt", weight="medium", category="digital_transformation")
    q_hiring = make_question(
        key="ia_hiring", weight="high", category="hiring", source_types={"jobs"}, recency_days=90
    )
    q_inhouse = make_question(
        key="ia_inhouse", weight="medium", category="internal_capability", polarity="negative"
    )
    icp = ICPConfig(
        countries=["DE"],
        employees_min=1000,
        nice_to_have=[
            Criterion(kind="industry_in", values=["logistics"], weight=2),
            Criterion(kind="revenue_at_least", values=[10_000_000_000], weight=1),
        ],
    )
    bundle = make_bundle(questions=[q_ai, q_dt, q_hiring, q_inhouse], icp=icp)
    signals = [
        # v = 1.0 × 1.0 × 1.0 × 1.0 = 1.0 → s = 1 → 3 points
        make_signal(q_ai, summary="Uses agentic AI to process customer RFQs."),
        # v = 0.208 and 0.35 × 1 × 0.8 = 0.28 → s = 1 − 0.792 × 0.72 = 0.42976 → 0.8595 points
        make_signal(
            q_dt,
            source_type="news",
            strength="moderate",
            confidence=0.8,
            event_date=days_ago(120),
            summary="Strategy 2030 digitalises core processes.",
        ),
        make_signal(q_dt, source_type="news", strength="weak", summary="Talks about digital transformation."),
        # v = 1.0 × 0.9 × 0.9 × 0.5 = 0.405 → 1.215 points
        make_signal(
            q_hiring,
            source_type="jobs",
            confidence=0.9,
            event_date=days_ago(45),
            summary="Hiring automation and AI engineers.",
        ),
        # v = 0.65 × 1 × 1 × 0.5 = 0.325 → 0.65 negative points
        make_signal(
            q_inhouse,
            source_type="report",
            strength="moderate",
            event_date=days_ago(365),
            summary="Runs a large in-house automation CoE.",
        ),
    ]
    return bundle, signals


def test_golden_scenario(ia):
    bundle, signals = ia
    company = make_company(revenue_eur=None)
    score = score_company(company, bundle, signals, NOW)

    # fit: industry matched (2) + revenue unknown (0.5 × 1) out of 3 → share 0.8333 → 20 + 80 × 0.8333 = 86.7
    assert score.fit == 86.7
    # intent: 100 × (1 − exp(−(3 + 0.85952 + 1.215) / 5)) = 63.76
    assert score.intent == 63.8
    # risk: 100 × (1 − exp(−0.65 / 2)) = 27.75
    assert score.risk == 27.7
    # priority: 100 × 0.8667^0.4 × 0.6376^0.6 × (1 − 0.5 × 0.2775) = 62.09
    assert score.priority == 62.1
    assert score.tier == "warm"
    assert not score.disqualified and not score.outside_icp

    points = {c.key: (c.strength, c.points) for c in score.breakdown}
    assert points == {
        "ia_ai_projects": (1.0, 3.0),
        "ia_hiring": (0.41, 1.22),
        "ia_dt": (0.43, 0.86),
        "ia_inhouse": (0.32, 0.65),
    }
    assert [c.key for c in score.breakdown] == ["ia_ai_projects", "ia_hiring", "ia_dt", "ia_inhouse"]
    assert len(next(c for c in score.breakdown if c.key == "ia_dt").signal_ids) == 2

    why = [(r.polarity, r.text) for r in score.why_now]
    assert why == [
        ("positive", "Uses agentic AI to process customer RFQs."),
        ("positive", "Hiring automation and AI engineers."),
        # fresh weak (v = 0.28) beats the 120-day-old moderate one (v = 0.208)
        ("positive", "Talks about digital transformation."),
        ("negative", "Runs a large in-house automation CoE."),  # risk 27.7 ≥ 20
        ("data_gap", "Unknown: revenue_eur"),
    ]
    assert score.why_now[0].signal_id == signals[0].id
    assert score.why_now[0].date == NOW.date()
    assert score.data_gaps == ["revenue_eur"]


def test_no_signals_gives_zero_intent_and_cold(ia):
    bundle, _ = ia
    score = score_company(make_company(revenue_eur=20_000_000_000), bundle, [], NOW)
    assert (score.fit, score.intent, score.risk, score.priority, score.tier) == (100.0, 0.0, 0.0, 0.0, "cold")
    assert all(c.points == 0 and c.signal_ids == [] for c in score.breakdown)
    assert score.why_now == []


def test_signal_filters(ia):
    bundle, signals = ia
    q_ai = bundle.questions[0]
    company = make_company()
    base = score_company(company, bundle, signals, NOW)
    noise = [
        make_signal(q_ai, confidence=0.4),  # below min_confidence 0.5
        make_signal(q_ai, status="rejected_by_user"),
        make_signal(make_question(key="deleted_question")),  # question no longer in the bundle
    ]
    assert score_company(company, bundle, signals + noise, NOW) == base


def _event(q, n: int, **overrides):
    """n-th distinct event: its own quote, summary, URL and a date two months apart from the others."""
    data = {
        "source_type": "news",
        "strength": "weak",
        "quote": f"Milestone {n}: "
        + [
            "rolls out RPA bots",
            "opens an AI lab",
            "hires a CDO",
            "moves to S/4HANA",
            "launches process mining",
            "automates invoices",
        ][n % 6],
        "summary": f"Event number {n} about automation.",
        "url": f"https://news{n}.example.com/story",
        "event_date": days_ago(60 * n),
        "published_at": NOW - 60 * n * DAY,
    }
    return make_signal(q, **(data | overrides))


def test_distinct_events_count_at_most_three_times():
    q = make_question()
    bundle = make_bundle(questions=[q], scoring=make_profile(half_life_days={"news": None}))
    three = score_company(make_company(), bundle, [_event(q, n) for n in range(3)], NOW)
    six = score_company(make_company(), bundle, [_event(q, n) for n in range(6)], NOW)
    # weak news: v = 0.35 × 1.0 × 0.8 = 0.28 each
    assert six.breakdown[0].strength == three.breakdown[0].strength == round(1 - 0.72**3, 2)
    assert len(six.breakdown[0].signal_ids) == 3


def test_reprints_of_one_story_count_once():
    """AI-15: ten outlets reprinting one story are one event, not ten independent signals."""
    q = make_question()
    bundle = make_bundle(questions=[q])
    story = {"source_type": "news", "strength": "weak", "confidence": 0.8, "event_date": NOW.date()}
    headlines = [
        "Lufthansa Group orders 20 Boeing 737 MAX 10s",
        "Lufthansa Group orders 20 Boeing 737 Max 10 jets",
        "Lufthansa Group To Grow Boeing 737 MAX Fleet By Securing 20 MAX 10 Orders",
    ]
    same_source = [make_signal(q, **story, quote=headlines[0]) for _ in range(10)]  # one URL, collected 10×
    single = score_company(make_company(), bundle, same_source[:1], NOW).breakdown[0]
    many = score_company(make_company(), bundle, same_source, NOW).breakdown[0]
    assert many.strength == single.strength == round(0.35 * 0.8 * 0.8, 2)  # no boost from one source
    assert len(many.signal_ids) == 10  # every reprint stays visible as evidence

    # the same story from three independent outlets: counted once, confidence 1 − 0.2³ → capped at 0.98
    outlets = [
        make_signal(
            q, **story, quote=h, url=f"https://outlet{i}.example.com/a", summary="Orders 20 MAX 10 jets."
        )
        for i, h in enumerate(headlines)
    ]
    corroborated = score_company(make_company(), bundle, outlets, NOW).breakdown[0]
    assert corroborated.strength == round(0.35 * 0.98 * 0.8, 2)
    assert single.strength < corroborated.strength < round(1 - (1 - 0.35 * 0.8 * 0.8) ** 3, 2)

    # a different event (other numbers, two months later) is independent evidence
    other = make_signal(
        q,
        **(story | {"event_date": days_ago(60)}),
        quote="Lufthansa Group orders ten Airbus A350",
        url="https://x.example/b",
    )
    both = score_company(make_company(), bundle, [*outlets, other], NOW).breakdown[0]
    assert both.strength > corroborated.strength


# --- properties ---------------------------------------------------------------------------------


def _priority(bundle, signals):
    return score_company(make_company(), bundle, signals, NOW).priority


def test_more_positive_evidence_never_lowers_priority(ia):
    bundle, signals = ia
    q_ai = bundle.questions[0]
    rest = signals[1:]
    previous = -1.0
    for confidence in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        for strength in ("weak", "moderate", "strong"):
            p = _priority(
                bundle,
                [*rest, make_signal(q_ai, strength=strength, confidence=confidence, source_type="news")],
            )
            assert p >= _priority(bundle, rest)
        p = _priority(bundle, [*rest, make_signal(q_ai, confidence=confidence)])
        assert p >= previous
        previous = p


def test_more_negative_evidence_never_raises_priority(ia):
    bundle, signals = ia
    q_inhouse = bundle.questions[3]
    previous = math.inf
    for n in range(4):
        extra = [make_signal(q_inhouse, source_type="news", strength="strong") for _ in range(n)]
        p = _priority(bundle, signals + extra)
        assert p <= previous
        previous = p


def test_older_evidence_never_contributes_more():
    q = make_question()
    bundle = make_bundle(questions=[q])
    points = [
        score_company(
            make_company(), bundle, [make_signal(q, source_type="news", event_date=days_ago(age))], NOW
        )
        .breakdown[0]
        .points
        for age in (0, 30, 120, 365, 730)
    ]
    assert points == sorted(points, reverse=True)
    assert points[0] > points[-1]


def test_fit_zero_means_priority_zero(ia):
    bundle, signals = ia
    score = score_company(make_company(country_code="US"), bundle, signals, NOW)
    assert (score.fit, score.priority, score.tier) == (0.0, 0.0, "cold")
    assert score.intent > 0  # signals are still visible
    assert ("fit", "Outside ICP: Country in DE") in [(r.polarity, r.text) for r in score.why_now]
    # an explicit status, so the UI can tell "outside ICP" from "in ICP, no intent"
    assert score.outside_icp
    assert score.rule_hits == [
        {
            "rule_id": "icp:must_have",
            "name": "Outside ICP: Country in DE",
            "kind": "icp",
            "action": "flag",
            "cap_value": None,
        }
    ]


def test_no_nice_to_have_match_keeps_the_fit_floor(ia):
    """Passing every must-have but matching no nice-to-have is still inside the ICP: Fit = floor, not 0."""
    bundle, signals = ia
    company = make_company(industry_ids=["retail"], revenue_eur=1_000_000)
    score = score_company(company, bundle, signals, NOW)
    assert score.fit == 20.0 and not score.outside_icp and score.rule_hits == []
    assert score.priority > 0
    no_floor = bundle.model_copy(update={"scoring": make_profile(fit_floor=0)})
    assert score_company(company, no_floor, signals, NOW).fit == 0.0


def test_exclude_cap_and_flag_rules(ia):
    bundle, signals = ia
    excl = bundle.model_copy(
        update={"rules": [rule("firmographic", {"field": "employees", "op": "lt", "value": 1_000_000})]}
    )
    score = score_company(make_company(), excl, signals, NOW)
    assert (score.disqualified, score.tier, score.priority) == (True, "disqualified", 0.0)
    assert score.rule_hits[0]["action"] == "exclude"

    capped = bundle.model_copy(
        update={
            "rules": [
                rule(
                    "signal", {"question_key": "ia_inhouse", "min_strength": 0.3}, action="cap", cap_value=50
                )
            ]
        }
    )
    score = score_company(make_company(), capped, signals, NOW)
    assert (score.priority, score.tier) == (50.0, "warm")

    flagged = bundle.model_copy(update={"rules": [rule("list", {"domains": ["www.DHL.com"]}, action="flag")]})
    score = score_company(make_company(), flagged, signals, NOW)
    assert score.priority == 62.1 and score.tier == "warm"
    assert [h["action"] for h in score.rule_hits] == ["flag"]


def test_profile_change_rescales_without_new_signals(ia):
    bundle, signals = ia
    strict = bundle.model_copy(update={"scoring": make_profile(tiers={"hot": 70, "warm": 60})})
    assert score_company(make_company(), strict, signals, NOW).tier == "warm"
    low_hiring = bundle.model_copy(
        update={
            "questions": [
                q.model_copy(update={"weight": "low"}) if q.key == "ia_hiring" else q
                for q in bundle.questions
            ]
        }
    )
    assert _priority(low_hiring, signals) < _priority(bundle, signals)


def test_sort_key_order():
    def s(priority, intent, fit):
        return score_company(make_company(), make_bundle(), [], NOW).model_copy(
            update={"priority": priority, "intent": intent, "fit": fit}
        )

    rows = [(s(50, 10, 10), "b"), (s(50, 10, 10), "A"), (s(50, 20, 10), "z"), (s(70, 0, 0), "y")]
    assert [name for sc, name in sorted(rows, key=lambda r: lead_sort_key(*r))] == ["y", "z", "A", "b"]


def test_rescore_1000_companies_under_one_second():
    questions = [make_question(key=f"q{i}", polarity="negative" if i >= 9 else "positive") for i in range(11)]
    bundle = make_bundle(questions=questions, icp=ICPConfig(countries=["DE"], employees_min=1000))
    companies = [make_company() for _ in range(1000)]
    signals = [
        [make_signal(q, source_type="news", event_date=days_ago(i * 10)) for q in questions for i in range(3)]
        for _ in companies
    ]
    started = time.perf_counter()
    for company, company_signals in zip(companies, signals, strict=True):
        score_company(company, bundle, company_signals, NOW)
    assert time.perf_counter() - started < 1.0


# --- fit and rules ------------------------------------------------------------------------------


def test_fit_must_have_unknown_passes_with_gap():
    icp = ICPConfig(countries=["DE", "AT"], industries_any=["logistics"], employees_min=1000)
    r = fit_score(make_company(country_code=None, industry_ids=[], employees=None), icp)
    assert (r.fit, r.must_have_passed) == (100.0, True)
    assert r.data_gaps == ["country_code", "industry_ids", "employees"]
    assert {d["status"] for d in r.details} == {"unknown"}


@pytest.mark.parametrize(
    "company",
    [
        {"country_code": "FR"},
        {"industry_ids": ["software"]},
        {"employees": 999},
    ],
)
def test_fit_must_have_failure(company):
    icp = ICPConfig(countries=["de"], industries_any=["logistics"], employees_min=1000)
    r = fit_score(make_company(**company), icp)
    assert (r.fit, r.must_have_passed) == (0.0, False)


def test_fit_nice_to_have_weights():
    icp = ICPConfig(
        nice_to_have=[
            Criterion(kind="country_in", values=["DE"], weight=3),
            Criterion(kind="employees_between", values=[1000, 10_000], weight=1),
            Criterion(kind="tag_in", values=["priority"], weight=1),
        ]
    )
    # country matches (3), employees 590k out of range (0), no tag (0) → 60
    assert fit_score(make_company(), icp).fit == 60.0
    assert fit_score(make_company(tags=["priority"], employees=5000), icp).fit == 100.0
    assert fit_score(make_company(), ICPConfig()).fit == 100.0


def test_rules_do_not_fire_on_unknown_data():
    company = make_company(employees=None, industry_ids=[])
    rules = [
        rule("firmographic", {"field": "employees", "op": "lt", "value": 500}),
        rule("firmographic", {"field": "industry_ids", "op": "not_in", "value": ["logistics"]}),
    ]
    assert evaluate_rules(company, rules) == []


def test_vendor_flag_and_list_rules():
    vendor = rule(
        "firmographic",
        {"field": "industry_ids", "op": "intersects", "value": ["it_services", "software"]},
        action="flag",
    )
    clients = rule("list", {"domains": ["https://sap.com".removeprefix("https://")]})
    sap = make_company(name="SAP", domain="sap.com", industry_ids=["Software"])
    assert [h["kind"] for h in evaluate_rules(sap, [vendor, clients])] == ["firmographic", "list"]
    assert evaluate_rules(make_company(), [vendor, clients]) == []
    via_own = make_company(domain="dhl.de", own_domains=["dhl.com"])
    assert len(evaluate_rules(via_own, [rule("list", {"domains": ["DHL.com"]})])) == 1


def test_signal_rule_threshold():
    r = rule("signal", {"question_key": "ia_distress", "min_strength": 0.5})
    assert evaluate_rules(make_company(), [r], {"ia_distress": 0.5})
    assert not evaluate_rules(make_company(), [r], {"ia_distress": 0.49})
    assert not evaluate_rules(make_company(), [r], {})


# --- default tuning: weights must be visible in the tier (P0) ----------------------------------------


def _demo_signals(q):
    """DHL-like demo account on the IA preset: realistic mix of sources, strengths and ages."""

    def sig(key, n, **kw):
        return make_signal(
            q[key],
            quote=f"{key} evidence number {n} with its own wording",
            summary=f"{key} summary {n}",
            url=f"https://source{n}.example.com/{key}",
            **kw,
        )

    return [
        sig("ia_ai_projects", 1, source_type="website", confidence=0.9, event_date=days_ago(60)),
        sig(
            "ia_ai_projects",
            2,
            source_type="news",
            strength="moderate",
            confidence=0.8,
            event_date=days_ago(30),
        ),
        sig("ia_dt", 3, source_type="report", strength="moderate", confidence=0.8, event_date=days_ago(100)),
        sig("ia_hiring", 4, source_type="jobs", confidence=0.9, event_date=days_ago(10)),
        sig("ia_hiring", 5, source_type="jobs", strength="moderate", confidence=0.8, event_date=days_ago(20)),
        sig(
            "ia_inhouse",
            6,
            source_type="website",
            strength="moderate",
            confidence=0.8,
            event_date=days_ago(90),
        ),
    ]


def test_weight_change_flips_a_tier_on_realistic_data():
    """ia_hiring High → Low must change the tier of a typical demo account (it did not with τ_intent = 3)."""
    from leadradar_ai.presets import load_preset

    bundle = load_preset("intelligent_automation").to_bundle()
    q = {x.key: x for x in bundle.questions}
    signals = _demo_signals(q)
    company = make_company()  # DE, logistics, 590k employees → Fit 100

    def with_hiring(weight, profile=None):
        questions = [
            x.model_copy(update={"weight": weight}) if x.key == "ia_hiring" else x for x in bundle.questions
        ]
        return bundle.model_copy(update={"questions": questions, "scoring": profile or bundle.scoring})

    high = score_company(company, with_hiring("high"), signals, NOW)
    low = score_company(company, with_hiring("low"), signals, NOW)
    assert (high.fit, high.risk) == (100.0, 33.0)
    assert (high.priority, high.tier) == (66.6, "hot")
    assert (low.priority, low.tier) == (59.4, "warm")

    # the old default τ_intent = 3 saturated Intent: the same change moved no tier
    tau3 = bundle.scoring.model_copy(update={"tau_intent": 3.0})
    assert score_company(company, with_hiring("high", tau3), signals, NOW).tier == "hot"
    assert score_company(company, with_hiring("low", tau3), signals, NOW).tier == "hot"
