import pytest
from leadradar_ai import load_preset, score_company
from leadradar_ai.scoring import derived_signals
from leadradar_ai.testing.factories import NOW, make_company

CYBER = load_preset("cybersecurity").to_bundle()
IA = load_preset("intelligent_automation").to_bundle()


def kinds(signals):
    return sorted(s.source_name.split()[0] for s in signals)


def test_eu_logistics_company_is_likely_in_nis2_scope():
    signals = derived_signals(make_company(), CYBER, NOW)
    assert kinds(signals) == ["NIS2"]
    s = signals[0]
    assert (s.question_key, s.source_type, s.flags, s.strength) == (
        "cy_compliance",
        "derived",
        {"derived"},
        "weak",
    )
    assert s.polarity == "positive" and s.reliability == 0.7 and s.event_date is None
    assert "Annex I" in s.summary and "590,000 employees" in s.quote
    assert s.url.startswith("https://eur-lex.europa.eu/")


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ({"industry_ids": ["banking"]}, ["DORA", "NIS2"]),
        ({"industry_ids": ["insurance"]}, ["DORA"]),
        ({"industry_ids": ["chemicals"]}, ["NIS2"]),  # Annex II
        ({"industry_ids": ["retail"]}, []),  # not a NIS2 sector
        ({"country_code": "GB"}, []),  # not in the EU
        ({"country_code": None}, []),
        ({"employees": 40, "revenue_eur": 5_000_000}, []),  # small enterprise
        ({"employees": 40, "revenue_eur": 20_000_000}, ["NIS2"]),  # revenue over the threshold
        ({"employees": None, "revenue_eur": None}, []),  # unknown size: no guess
        ({"industry_ids": []}, []),
    ],
)
def test_scope_rules(company, expected):
    assert kinds(derived_signals(make_company(**company), CYBER, NOW)) == expected


def test_services_without_a_compliance_question_get_nothing():
    assert derived_signals(make_company(industry_ids=["banking"]), IA, NOW) == []


def test_ids_are_stable_per_company_and_service():
    company = make_company()
    assert [s.id for s in derived_signals(company, CYBER, NOW)] == [
        s.id for s in derived_signals(company, CYBER, NOW)
    ]
    assert derived_signals(make_company(), CYBER, NOW)[0].id != derived_signals(company, CYBER, NOW)[0].id


def test_score_includes_derived_but_real_evidence_dominates():
    bank = make_company(
        name="Example Bank", domain="bank.example", industry_ids=["banking"], country_code="FR"
    )
    with_derived = score_company(bank, CYBER, [], NOW)
    without = score_company(bank, CYBER, [], NOW, include_derived=False)
    assert without.intent == 0.0
    compliance = next(c for c in with_derived.breakdown if c.key == "cy_compliance")
    # NIS2 weak 0.35·0.8·0.7 = 0.196, DORA moderate 0.65·0.8·0.7 = 0.364 → noisy-OR 0.489 × weight 3
    assert compliance.strength == 0.49 and compliance.points == 1.47
    assert with_derived.intent == pytest.approx(25.4, abs=0.1)  # 100 × (1 − exp(−1.4675 / 5))
    assert with_derived.tier in ("cold", "warm") and with_derived.tier != "hot"
    assert [r.source_name for r in with_derived.why_now if r.polarity == "positive"] == [
        "DORA scope (firmographics)"
    ]
    assert len(compliance.signal_ids) == 2
