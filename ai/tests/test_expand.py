import asyncio
from pathlib import Path

import pytest
from leadradar_ai import ICPConfig, QuotaExhausted
from leadradar_ai.config_assist import apply_expansion, expand_question, languages_for_icp
from leadradar_ai.config_assist.expand import (
    PROMPT_VERSION,
    ExpansionOutput,
    expansion_prompt,
    render_system,
    render_user,
)
from leadradar_ai.presets import load_preset
from leadradar_ai.retrieval.prefilter import question_query
from leadradar_ai.testing import BLOCKED, FakeLLM
from leadradar_ai.testing.factories import make_bundle, make_question

SNAPSHOTS = Path(__file__).parent / "snapshots"


def run(coro):
    return asyncio.run(coro)


def hiring_question(**overrides):
    data = {
        "key": "ia_hiring",
        "category": "hiring",
        "source_types": {"jobs"},
        "recency_days": 90,
        "text": "Is the company hiring RPA developers?",
        "keywords": {"en": ["RPA"]},
        "job_titles": ["RPA Developer"],
        "negative_terms": ["internship"],
    }
    return make_question(**(data | overrides))


def output(**overrides):
    data = {
        "keywords": [
            {"language": "en", "terms": ["RPA", "robotic process automation", "  UiPath  ", "uipath", ""]},
            {"language": "DE", "terms": ["Prozessautomatisierung"]},
            {"language": "ru", "terms": ["автоматизация"]},  # not requested
        ],
        "job_titles": ["rpa developer", "Automation Engineer"],
        "negative_terms": ["Internship", "Werkstudent"],
    }
    return data | overrides


def test_languages_for_icp():
    assert languages_for_icp(ICPConfig()) == ["en"]
    assert languages_for_icp(ICPConfig(countries=["DE", "AT"])) == ["en", "de"]
    assert languages_for_icp(ICPConfig(countries=["ch", "BE"])) == ["en", "de", "fr", "nl", "it"]
    assert languages_for_icp(load_preset("intelligent_automation").icp) == [
        "en",
        "de",
        "fr",
        "nl",
        "es",
        "it",
    ]
    assert len(languages_for_icp(load_preset("cybersecurity").icp, max_languages=3)) == 3


def test_request_uses_cheap_pool_and_minimal_thinking():
    llm = FakeLLM([output()])
    bundle = make_bundle(questions=[hiring_question()], icp=ICPConfig(countries=["DE"]))
    run(expand_question(llm, bundle, bundle.questions[0]))
    req = llm.calls[0]
    assert (req.purpose, req.prompt_version, req.pool, req.thinking) == (
        "expand_question",
        PROMPT_VERSION,
        "cheap",
        "minimal",
    )
    assert req.output_model is ExpansionOutput
    assert "<languages>en, de</languages>" in req.user
    assert "en: RPA" in req.user  # the current keywords are the seed


def test_result_is_cleaned_merged_with_seed_and_limited():
    bundle = make_bundle(questions=[hiring_question()], icp=ICPConfig(countries=["DE"]))
    many = [f"term {i}" for i in range(30)]
    llm = FakeLLM([output(keywords=[*output()["keywords"], {"language": "en", "terms": many}])])
    exp = run(expand_question(llm, bundle, bundle.questions[0]))
    assert exp.keywords["en"][:3] == ["RPA", "robotic process automation", "UiPath"]  # seed first, dedup
    assert len(exp.keywords["en"]) == 12
    assert exp.keywords["de"] == ["Prozessautomatisierung"]
    assert "ru" not in exp.keywords
    assert exp.job_titles == ["RPA Developer", "Automation Engineer"]
    assert exp.negative_terms == ["internship", "Werkstudent"]
    assert exp.from_llm and exp.model == "fake-model"


def test_job_titles_only_for_hiring_or_jobs_questions():
    q = make_question(key="ia_cost", source_types={"news", "report"})
    bundle = make_bundle(questions=[q])
    exp = run(expand_question(FakeLLM([output()]), bundle, q))
    assert exp.job_titles == []


def test_blocked_returns_seed_only():
    q = hiring_question()
    bundle = make_bundle(questions=[q])
    exp = run(expand_question(FakeLLM([BLOCKED]), bundle, q, languages=["en"]))
    assert (exp.keywords, exp.job_titles, exp.from_llm) == ({"en": ["RPA"]}, ["RPA Developer"], False)


def test_quota_errors_propagate_for_retry():
    q = hiring_question()
    with pytest.raises(QuotaExhausted):
        run(expand_question(FakeLLM([QuotaExhausted("cheap")]), make_bundle(questions=[q]), q))


def test_apply_expansion_feeds_the_prefilter_query():
    q = hiring_question()
    exp = run(expand_question(FakeLLM([output()]), make_bundle(questions=[q]), q, languages=["en", "de"]))
    updated = apply_expansion(q, exp)
    assert updated.version == q.version and updated.id == q.id
    query = question_query(updated)
    assert "Prozessautomatisierung" in query and "Automation Engineer" in query


def test_examples_are_valid_and_prompt_is_snapshotted():
    for ex in expansion_prompt().examples:
        out = ExpansionOutput.model_validate(ex["output"])
        assert {k.language for k in out.keywords} == set(ex["input"]["languages"])
        seed = {t.casefold() for terms in ex["input"]["seed"].values() for t in terms}
        assert not seed & {t.casefold() for k in out.keywords for t in k.terms}, (
            "examples must not repeat seeds"
        )
    ia = load_preset("intelligent_automation").to_bundle()
    text = (
        render_system() + "\n---\n" + render_user(ia, ia.questions[2], ["en", "de"], ia.questions[2].keywords)
    )
    path = SNAPSHOTS / "expand_question_v1.txt"
    if not path.exists():
        path.write_text(text + "\n", encoding="utf-8")
    assert text + "\n" == path.read_text(encoding="utf-8"), f"prompt changed: bump {PROMPT_VERSION}"
