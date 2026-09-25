import os
from pathlib import Path

import pytest

from leadradar_ai.extraction import PROMPT_VERSION, ExtractionOutput, render_system, render_user
from leadradar_ai.extraction.extract import extraction_prompt
from leadradar_ai.testing.factories import make_bundle, make_company, make_question, make_snippet
from leadradar_ai.verification import find_quote

SNAPSHOTS = Path(__file__).parent / "snapshots"


def check_snapshot(name: str, text: str) -> None:
    """Compare with the stored snapshot; UPDATE_SNAPSHOTS=1 rewrites it (then review the diff and bump the
    prompt version if the prompt itself changed)."""
    path = SNAPSHOTS / name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not path.exists():
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8"), (
        f"prompt changed: bump {PROMPT_VERSION} and update {path}"
    )


def test_prompt_version():
    assert PROMPT_VERSION == "extract_signals@v1"
    assert extraction_prompt().id == PROMPT_VERSION


def test_examples_follow_the_schema_and_their_own_rules():
    for ex in extraction_prompt().examples:
        output = ExtractionOutput.model_validate(ex["output"])
        snippets = {s["id"]: s for s in ex["input"]["snippets"]}
        assert {a.question_id for a in output.answers} == {q["id"] for q in ex["input"]["questions"]}
        for a in output.answers:
            if a.answer == "yes":
                assert a.evidence, "yes needs evidence"
            for ev in a.evidence:
                match = find_quote(ev.quote, snippets[ev.snippet_id]["text"])
                assert match is not None and not match.fuzzy, f"example quote is not verbatim: {ev.quote}"


def test_examples_cover_the_traps():
    comments = " ".join(ex["comment"] for ex in extraction_prompt().examples).lower()
    assert "vendor" in comments and "homonym" in comments and "negative" in comments


def test_system_prompt_structure_and_snapshot():
    system = render_system()
    for tag in ("<role>", "<rules>", "<output_format>", "<examples>", "<example>", "</examples>"):
        assert tag in system
    assert system.count("<example>") == 3
    assert "temperature" not in system
    check_snapshot("extract_signals_v1_system.txt", system + "\n")


def test_user_prompt_order_and_snapshot():
    company = make_company(id="00000000-0000-0000-0000-000000000001")
    q1 = make_question(id="00000000-0000-0000-0000-00000000000a")
    q2 = make_question(
        id="00000000-0000-0000-0000-00000000000b",
        key="ia_inhouse",
        polarity="negative",
        category="internal_capability",
        text="Does the company have a large in-house automation team?",
    )
    bundle = make_bundle(questions=[q1, q2])
    snippets = [
        make_snippet(
            id="S1",
            document_id="00000000-0000-0000-0000-0000000000d1",
            chunk_id="00000000-0000-0000-0000-0000000000c1",
        ),
        make_snippet(
            id="S2",
            source_type="news",
            source_name="gdelt",
            title='CEO says "AI first"\nstrategy',
            text="DHL Group CEO said AI will change how the group works.",
            document_id="00000000-0000-0000-0000-0000000000d2",
            chunk_id="00000000-0000-0000-0000-0000000000c2",
            published_at=None,
        ),
    ]
    user = render_user(company, bundle, snippets, [("Q1", q1), ("Q2", q2)])
    # context first, then questions, the task at the very end
    assert user.index("<company>") < user.index("<snippets>") < user.index("<questions>")
    assert user.endswith("Return JSON only.")
    assert "title=\"CEO says 'AI first' strategy\"" in user  # attribute stays well-formed
    assert 'published="unknown"' in user
    assert '<question id="Q2" polarity="negative" category="internal_capability">' in user
    check_snapshot("extract_signals_v1_user.txt", user + "\n")


@pytest.mark.parametrize("field", ["aliases", "industry_ids"])
def test_user_prompt_handles_missing_company_data(field):
    company = make_company(**{field: []}, employees=None, country_code=None)
    user = render_user(company, make_bundle(), [], [])
    assert "employees: unknown" in user and "country: unknown" in user
