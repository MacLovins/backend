"""AI-17 suggest_questions and AI-18 classify_industry: cheap model, structured output, code-side validation."""

import asyncio

import pytest
from leadradar_ai import QuotaExhausted, classify_industry, suggest_questions
from leadradar_ai.config_assist.classify import MAX_TEXT_CHARS, ClassificationOutput
from leadradar_ai.config_assist.suggest import PROMPT_VERSION, SuggestionOutput
from leadradar_ai.presets import load_preset
from leadradar_ai.testing import BLOCKED, FakeLLM
from leadradar_ai.testing.factories import make_bundle, make_company, make_question


def run(coro):
    return asyncio.run(coro)


def draft(key, **overrides):
    data = {
        "key": key,
        "label": key.replace("_", " ").title(),
        "text": f"Is the company doing {key}?",
        "category": "ai_automation",
        "polarity": "positive",
        "weight": "medium",
        "source_types": ["news", "website"],
        "recency_days": 365,
        "keywords": ["RPA", " RPA ", "process mining", ""],
    }
    return data | overrides


def suggestion_output():
    questions = [draft(f"sq_{i}") for i in range(8)]
    questions += [
        draft("sq_inhouse", polarity="negative", category="internal_capability", recency_days=5000),
        draft("sq_vendor", polarity="negative", category="tech_partners", source_types=["news", "tiktok"]),
        draft("sq_bad_category", category="astrology"),  # dropped: unknown category
        draft("sq_no_sources", source_types=["podcast"]),  # dropped: no known source
        draft("Existing Key"),  # dropped: the service already has it
        draft("sq_0"),  # dropped: duplicate
    ]
    rules = [
        {
            "name": "Too small",
            "kind": "firmographic",
            "field": "employees",
            "op": "lt",
            "value": "500",
            "action": "exclude",
        },
        {
            "name": "Strong in-house team",
            "kind": "signal",
            "question_key": "sq_inhouse",
            "min_strength": 0.6,
            "action": "cap",
            "cap_value": 40,
        },
        {
            "name": "Broken",
            "kind": "firmographic",
            "field": "mood",
            "op": "lt",
            "value": "x",
            "action": "flag",
        },
    ]
    return {"questions": questions, "rules": rules}


def test_suggest_questions_validates_drafts_in_code():
    service = make_bundle(questions=[make_question(key="existing_key")])
    llm = FakeLLM([suggestion_output()])
    result = run(suggest_questions(llm, service))

    (call,) = llm.calls_for("suggest_questions")
    assert call.pool == "cheap" and call.thinking == "minimal" and call.prompt_version == PROMPT_VERSION
    assert call.output_model is SuggestionOutput
    assert "existing_key" in call.user and "internal_capability" in call.user  # context: keys + categories

    keys = [s.question.key for s in result.questions]
    assert keys == [*(f"sq_{i}" for i in range(8)), "sq_inhouse", "sq_vendor"]
    assert 8 <= len(result.questions) <= 12
    assert sum(s.question.polarity == "negative" for s in result.questions) == 2
    by_key = {s.question.key: s.question for s in result.questions}
    assert by_key["sq_inhouse"].recency_days == 730  # clamped
    assert by_key["sq_vendor"].source_types == {"news"}
    assert by_key["sq_0"].keywords == {"en": ["RPA", "process mining"]} and by_key["sq_0"].version == 1
    assert result.questions[0].label == "Sq 0"

    assert [(r.name, r.kind, r.action) for r in result.rules] == [
        ("Too small", "firmographic", "exclude"),
        ("Strong in-house team", "signal", "cap"),
    ]
    assert result.rules[0].condition == {"field": "employees", "op": "lt", "value": 500}
    assert result.from_llm and result.model == "fake-model"


def test_suggest_questions_for_a_preset_service_and_blocked_answer():
    service = load_preset("cybersecurity").to_bundle()
    blocked = run(suggest_questions(FakeLLM([BLOCKED]), service))
    assert blocked.questions == [] and blocked.rules == [] and not blocked.from_llm
    with pytest.raises(QuotaExhausted):
        run(suggest_questions(FakeLLM([QuotaExhausted("cheap")]), service))


TAXONOMY = {"logistics": "Logistics & transport", "airlines": "Airlines", "software": "Software"}


def test_classify_industry_keeps_only_taxonomy_ids():
    llm = FakeLLM(
        [
            {
                "industry_ids": ["logistics", "space_travel", "logistics", "airlines"],
                "confidence": 0.9,
                "rationale": "r",
            }
        ]
    )
    company = make_company(industry_ids=[])
    text = "DHL is the leading global logistics company. " * 500
    result = run(classify_industry(llm, company, text, TAXONOMY, max_industries=2))
    assert result.industry_ids == ["logistics", "airlines"] and result.confidence == 0.9

    (call,) = llm.calls_for("classify_industry")
    assert call.pool == "cheap" and call.output_model is ClassificationOutput
    assert "airlines: Airlines" in call.user
    assert len(call.user) < MAX_TEXT_CHARS + 1_000  # the website text is truncated


def test_classify_industry_edge_cases():
    company = make_company(industry_ids=[])
    llm = FakeLLM([])
    empty = run(classify_industry(llm, company, "   ", TAXONOMY))
    assert empty.industry_ids == [] and not empty.from_llm and llm.calls == []  # no call without text

    blocked = run(classify_industry(FakeLLM([BLOCKED]), company, "We fly planes.", TAXONOMY))
    assert blocked.industry_ids == [] and not blocked.from_llm

    unknown = FakeLLM([{"industry_ids": ["space_travel"], "confidence": 0.8, "rationale": "r"}])
    result = run(classify_industry(unknown, company, "We fly rockets.", TAXONOMY))
    assert result.industry_ids == [] and result.confidence == 0.0 and result.from_llm
