import re

import pytest
from pydantic import ValidationError

from leadradar_ai import score_company
from leadradar_ai.presets import SIGNAL_CATEGORIES, Preset, list_presets, load_preset
from leadradar_ai.testing.factories import NOW, make_company, make_question, make_signal

# parser/SPEC.md §1.7.7 — ids the presets may reference
TAXONOMY = {
    "logistics",
    "airlines",
    "rail",
    "postal_courier",
    "automotive",
    "manufacturing",
    "chemicals",
    "pharma",
    "medical_devices",
    "healthcare",
    "banking",
    "insurance",
    "financial_markets",
    "telecom",
    "energy_utilities",
    "oil_gas",
    "water",
    "retail",
    "consumer_goods",
    "food_beverage",
    "public_sector",
    "digital_infrastructure",
    "it_services",
    "software",
    "media",
    "construction",
    "real_estate",
}
IA_KEYS = [
    "ia_cost",
    "ia_ai_projects",
    "ia_hiring",
    "ia_dt",
    "ia_ssc",
    "ia_leaders",
    "ia_erp",
    "ia_stack",
    "ia_inhouse",
    "ia_partner",
    "ia_distress",
]
CY_KEYS = [
    "cy_incident",
    "cy_compliance",
    "cy_hiring",
    "cy_leaders",
    "cy_surface",
    "cy_budget",
    "cy_stack",
    "cy_mssp",
    "cy_inhouse",
]


def test_presets_match_architecture():
    assert list_presets() == ["cybersecurity", "intelligent_automation"]
    ia, cy = load_preset("intelligent_automation"), load_preset("cybersecurity")
    assert [q.key for q in ia.questions] == IA_KEYS
    assert [q.key for q in cy.questions] == CY_KEYS
    assert {q.key for q in ia.questions if q.polarity == "negative"} == {
        "ia_inhouse",
        "ia_partner",
        "ia_distress",
    }
    assert {q.key for q in cy.questions if q.polarity == "negative"} == {"cy_mssp", "cy_inhouse"}
    assert (ia.icp.employees_min, cy.icp.employees_min) == (1000, 250)
    assert "Head of Process Excellence" in ia.decision_makers and "CISO" in cy.decision_makers


def test_ia_covers_every_annex_signal_type():
    # Annex A2: cost/efficiency, digital transformation, AI/RPA projects, hiring, new executives, shared services,
    # technologies in use, existing technology partners
    categories = {q.category for q in load_preset("intelligent_automation").questions}
    assert categories >= {
        "cost_efficiency",
        "digital_transformation",
        "ai_automation",
        "hiring",
        "leadership_change",
        "shared_services",
        "tech_stack",
        "tech_partners",
    }


@pytest.mark.parametrize("key", ["intelligent_automation", "cybersecurity"])
def test_preset_data_is_consistent(key):
    p = load_preset(key)
    referenced = {v for c in p.icp.nice_to_have if c.kind == "industry_in" for v in c.values}
    referenced |= {
        v for r in p.rules if r.condition.get("field") == "industry_ids" for v in r.condition["value"]
    }
    assert referenced <= TAXONOMY
    assert all(re.fullmatch(r"[A-Z]{2}", c) for c in p.icp.countries) and "NO" in p.icp.countries
    for q in p.questions:
        assert q.category in SIGNAL_CATEGORIES
        assert q.keywords_seed.get("en"), f"{q.key}: English keywords seed the query"
        if q.category == "hiring":
            assert q.source_types == {"jobs"} and q.job_titles and q.recency_days <= 90


@pytest.mark.parametrize("key", ["intelligent_automation", "cybersecurity"])
def test_to_bundle_has_stable_unique_ids(key):
    p = load_preset(key)
    a, b = p.to_bundle(), p.to_bundle()
    assert a == b
    ids = [q.id for q in a.questions] + [r.id for r in a.rules] + [a.service_id, a.scoring.id]
    assert len(ids) == len(set(ids))
    assert a.questions[0].keywords == p.questions[0].keywords_seed


def test_bundles_of_different_presets_do_not_share_ids():
    ia, cy = load_preset("intelligent_automation").to_bundle(), load_preset("cybersecurity").to_bundle()
    assert ia.service_id != cy.service_id
    assert not {q.id for q in ia.questions} & {q.id for q in cy.questions}


def test_ia_rules_on_typical_companies():
    bundle = load_preset("intelligent_automation").to_bundle()
    ai_q = next(q for q in bundle.questions if q.key == "ia_ai_projects")
    signals = [make_signal(ai_q)]

    dhl = score_company(make_company(), bundle, signals, NOW)
    assert dhl.fit == 100.0 and not dhl.disqualified and dhl.rule_hits == []

    sap = score_company(
        make_company(name="SAP", domain="sap.com", industry_ids=["software"], employees=100_000),
        bundle,
        signals,
        NOW,
    )
    assert [h["name"] for h in sap.rule_hits] == ["IT or software vendor"] and not sap.disqualified

    small = score_company(make_company(employees=300), bundle, signals, NOW)
    assert small.tier == "disqualified"

    us = score_company(make_company(country_code="US"), bundle, signals, NOW)
    assert us.fit == 0.0 and us.priority == 0.0


def test_cyber_vendor_is_excluded():
    bundle = load_preset("cybersecurity").to_bundle()
    vendor = score_company(make_company(tags=["cybersecurity_vendor"]), bundle, [], NOW)
    assert vendor.disqualified


def test_unknown_preset():
    with pytest.raises(KeyError, match="available"):
        load_preset("nope")


def _preset_dict(**overrides):
    q = make_question()
    data = {
        "key": "x",
        "name": "X",
        "description": "d",
        "value_proposition": "v",
        "decision_makers": [],
        "icp": {},
        "rules": [],
        "questions": [
            {
                "key": "q1",
                "label": "Q",
                "text": q.text,
                "category": "ai_automation",
                "polarity": "positive",
                "weight": "high",
                "source_types": ["news"],
                "recency_days": 30,
            }
        ],
    }
    return data | overrides


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "rules": [
                {
                    "name": "r",
                    "kind": "signal",
                    "condition": {"question_key": "missing", "min_strength": 0.5},
                    "action": "flag",
                }
            ]
        },
        {
            "rules": [
                {
                    "name": "r",
                    "kind": "firmographic",
                    "condition": {"field": "employees", "op": "lt"},
                    "action": "exclude",
                }
            ]
        },
        {"scoring": {"tiers": {"hot": 10, "warm": 50}}},
        {"questions": [_preset_dict()["questions"][0] | {"category": "astrology"}]},
        {"questions": [_preset_dict()["questions"][0]] * 2},
    ],
)
def test_invalid_presets_are_rejected(overrides):
    with pytest.raises(ValidationError):
        Preset.model_validate(_preset_dict(**overrides))
