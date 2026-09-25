import asyncio

import pytest

from leadradar_ai import QuotaExhausted
from leadradar_ai.extraction import PROMPT_VERSION, ExtractionOutput, extract_service, render_system
from leadradar_ai.retrieval import PrefilterResult
from leadradar_ai.testing import BLOCKED, FakeLLM
from leadradar_ai.testing.factories import make_bundle, make_company, make_question, make_snippet


def run(coro):
    return asyncio.run(coro)


def prefiltered(bundle, candidates: dict[int, list[str]], text_len: int = 80) -> PrefilterResult:
    """candidates: question index → snippet ids."""
    ids = sorted({sid for sids in candidates.values() for sid in sids}, key=lambda s: int(s[1:]))
    return PrefilterResult(
        snippets=[
            make_snippet(id=sid, text=f"{sid} " + "agentic AI automation " * (text_len // 22)) for sid in ids
        ],
        candidates={str(bundle.questions[i].id): sids for i, sids in candidates.items()},
        no_candidates=[str(q.id) for i, q in enumerate(bundle.questions) if i not in candidates],
        fingerprint="fp",
        stats={},
    )


def answer(pid, value="yes", evidence=None, confidence=0.9):
    return {
        "question_id": pid,
        "answer": value,
        "confidence": confidence,
        "evidence": evidence or [],
        "rationale": "r",
    }


def three_questions():
    return make_bundle(questions=[make_question(key="a"), make_question(key="b"), make_question(key="c")])


def test_no_candidates_means_no_llm_call():
    bundle = three_questions()
    llm = FakeLLM([])
    out = run(extract_service(llm, make_company(), bundle, prefiltered(bundle, {})))
    assert llm.calls == [] and out.llm_calls == 0
    assert {a.answer for a in out.answers.values()} == {"no"}
    assert out.auto_no == [str(q.id) for q in bundle.questions]


def test_one_call_per_service_with_short_ids_mapped_back():
    bundle = three_questions()
    pre = prefiltered(bundle, {0: ["S1", "S2"], 2: ["S2"]})
    llm = FakeLLM([{"answers": [answer("Q1"), answer("Q2", "unclear")]}])
    out = run(extract_service(llm, make_company(), bundle, pre))

    assert len(llm.calls) == 1 and out.llm_calls == 1 and out.model == "fake-model"
    req = llm.calls[0]
    assert (req.purpose, req.prompt_version, req.pool, req.output_model) == (
        "extract_signals",
        PROMPT_VERSION,
        "main",
        ExtractionOutput,
    )
    assert req.system == render_system()
    assert '<question id="Q1"' in req.user and '<question id="Q2"' in req.user and '"Q3"' not in req.user
    assert 'id="S1"' in req.user and 'id="S2"' in req.user

    q_a, q_b, q_c = (str(q.id) for q in bundle.questions)
    assert out.answers[q_a].answer == "yes" and out.answers[q_a].question_id == q_a
    assert out.answers[q_b].answer == "no" and out.auto_no == [q_b]  # no candidates → no without LLM
    assert out.answers[q_c].answer == "unclear"  # the model's Q2 is our third question


def test_missing_and_duplicate_answers():
    bundle = three_questions()
    pre = prefiltered(bundle, {0: ["S1"], 1: ["S1"]})
    llm = FakeLLM([{"answers": [answer("Q1", "no"), answer("Q1", "yes")]}])
    out = run(extract_service(llm, make_company(), bundle, pre))
    q_a, q_b, _ = (str(q.id) for q in bundle.questions)
    assert out.answers[q_a].answer == "no"  # the first answer wins
    assert out.answers[q_b].answer == "unclear" and out.answers[q_b].confidence == 0


def test_blocked_call_makes_its_answers_unclear():
    bundle = three_questions()
    out = run(extract_service(FakeLLM([BLOCKED]), make_company(), bundle, prefiltered(bundle, {0: ["S1"]})))
    assert out.blocked
    assert out.answers[str(bundle.questions[0].id)].answer == "unclear"
    assert out.answers[str(bundle.questions[1].id)].answer == "no"


def test_over_budget_splits_into_batches_with_own_snippets():
    bundle = three_questions()
    pre = prefiltered(bundle, {0: ["S1"], 1: ["S2"], 2: ["S3"]}, text_len=3000)
    llm = FakeLLM(lambda r: {"answers": [answer(f"Q{i}") for i in range(1, r.user.count("<question ") + 1)]})
    system_tokens = len(render_system()) // 4
    out = run(extract_service(llm, make_company(), bundle, pre, max_input_tokens=system_tokens + 1400))
    assert len(llm.calls) >= 2
    for call in llm.calls:
        asked = call.user.count("<question ")
        shown = call.user.count("<snippet ")
        assert shown == asked  # each question brings only its own snippet
    assert all(a.answer == "yes" for a in out.answers.values())


def test_quota_errors_propagate():
    bundle = three_questions()
    llm = FakeLLM([QuotaExhausted("main")])
    with pytest.raises(QuotaExhausted):  # must reach the graph: stage "paused"
        run(extract_service(llm, make_company(), bundle, prefiltered(bundle, {0: ["S1"]})))
