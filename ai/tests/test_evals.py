import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from leadradar_ai import cli
from leadradar_ai.contracts import Snippet
from leadradar_ai.evals import (
    CompanyCase,
    Decision,
    GoldenLabel,
    GoldenOracleLLM,
    compute_metrics,
    default_golden_dir,
    load_companies,
    load_labels,
    render_markdown,
    run_eval,
    write_report,
)
from leadradar_ai.evals.runner import company_profile
from leadradar_ai.extraction import Answer, Evidence, ServiceExtraction
from leadradar_ai.local import load_parser_jsonl
from leadradar_ai.presets import load_preset
from leadradar_ai.retrieval import PrefilterConfig
from leadradar_ai.testing import FakeEmbedder, FakeLLM
from leadradar_ai.verification import find_quote, verify_extraction
from typer.testing import CliRunner

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
SNIPPET = re.compile(r'<snippet id="(S\d+)"[^>]*>(.*?)</snippet>', re.DOTALL)
QUESTION = re.compile(r'<question id="(Q\d+)"[^>]*>(.*?)</question>', re.DOTALL)


def doc(url, text, title="Strategy 2030"):
    return {
        "source_type": "website",
        "source_name": "website",
        "url": url,
        "canonical_url": url,
        "title": title,
        "text": text,
        "published_at": (NOW - timedelta(days=20)).isoformat(),
        "fetched_at": NOW.isoformat(),
        "language": "en",
        "content_hash": url,
        "meta": {},
    }


@pytest.fixture
def fixtures(tmp_path):
    d = tmp_path / "fixtures"
    d.mkdir()
    lines = [
        doc(
            "https://www.dhl.com/strategy",
            "With Strategy 2030 DHL deploys agentic AI and robotic process "
            "automation to process customer RFQs in its forwarding business.",
        ),
        doc(
            "https://www.dhl.com/finance",
            "DHL keeps investing in growth and expects cost and efficiency "
            "programs to remain stable this year.",
            title="Finance",
        ),
    ]
    (d / "dhl.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return d


def llm_handler(request):
    """ia_ai_projects: correct yes · ia_cost: wrong yes (FP) · anything else: unclear,
    plus one invented quote for ia_dt (hallucination)."""
    user = request.user.split("</examples>")[-1]
    snippets = dict(SNIPPET.findall(user))
    answers = []
    for qid, text in QUESTION.findall(user):
        ev = []
        answer = "unclear"
        sid_ai = next((s for s, b in snippets.items() if "agentic AI" in b), None)
        sid_cost = next((s for s, b in snippets.items() if "efficiency" in b), None)
        if "agentic AI" in text and sid_ai:
            answer, ev = "yes", [(sid_ai, "DHL deploys agentic AI and robotic process automation")]
        elif "cost reduction" in text and sid_cost:
            answer, ev = "yes", [(sid_cost, "expects cost and efficiency programs to remain stable")]
        elif "digital transformation" in text and sid_ai:
            answer, ev = "yes", [(sid_ai, "DHL launched a 2 billion euro transformation fund")]
        answers.append(
            {
                "question_id": qid,
                "answer": answer,
                "confidence": 0.9,
                "rationale": "r",
                "evidence": [
                    {
                        "snippet_id": s,
                        "quote": q,
                        "subject": "target_company",
                        "event_date": None,
                        "strength": "strong",
                        "summary": "s",
                    }
                    for s, q in ev
                ],
            }
        )
    return {"answers": answers}


def labels():
    ia = "intelligent_automation"
    return [
        GoldenLabel(company_domain="dhl.com", service=ia, question_key="ia_ai_projects", expected="yes"),
        GoldenLabel(company_domain="dhl.com", service=ia, question_key="ia_cost", expected="no"),
        GoldenLabel(
            company_domain="dhl.com",
            service=ia,
            question_key="ia_hiring",
            expected="yes",
            evidence_hint="RPA developer jobs",
        ),
        GoldenLabel(company_domain="dhl.com", service=ia, question_key="ia_leaders", expected="no"),
        GoldenLabel(company_domain="lufthansagroup.com", service=ia, question_key="ia_cost", expected="yes"),
    ]


def companies():
    return {
        "dhl.com": CompanyCase(
            domain="dhl.com",
            name="DHL Group",
            fixture="dhl.jsonl",
            country="DE",
            industries=["logistics"],
            employees=590000,
        ),
        "lufthansagroup.com": CompanyCase(
            domain="lufthansagroup.com", name="Lufthansa Group", fixture="lufthansa.jsonl"
        ),
    }


def evaluate(fixtures, llm=None):
    return asyncio.run(
        run_eval(
            labels(),
            companies(),
            fixtures,
            llm or FakeLLM(llm_handler),
            FakeEmbedder(),
            NOW,
            prefilter=PrefilterConfig(min_cosine=0.5),
        )
    )


def test_run_eval_counts_every_outcome(fixtures):
    result = evaluate(fixtures)
    outcomes = {(d.company_domain, d.question_key): d.outcome for d in result.decisions}
    assert outcomes == {
        ("dhl.com", "ia_ai_projects"): "TP",
        ("dhl.com", "ia_cost"): "FP",
        ("dhl.com", "ia_hiring"): "FN",
        ("dhl.com", "ia_leaders"): "TN",
        ("lufthansagroup.com", "ia_cost"): "not_evaluated",
    }
    m = result.metrics
    assert m.overall == {"tp": 1, "fp": 1, "fn": 1, "tn": 1, "precision": 0.5, "recall": 0.5, "f1": 0.5}
    assert (m.evaluated, m.not_evaluated) == (4, 1)
    assert m.rejected_by_reason == {"quote_not_found": 1}
    assert m.hallucination_rate == pytest.approx(1 / 3, abs=1e-3)  # 1 invented of 3 quotes
    assert m.llm_calls == 1 and m.llm_calls_per_company == 1.0
    assert any("lufthansa.jsonl not found" in n for n in result.notes)

    by_key = {d.question_key: d for d in result.decisions if d.company_domain == "dhl.com"}
    assert by_key["ia_hiring"].auto_no  # no jobs in the fixture → data problem, not the model
    assert by_key["ia_cost"].quotes == ["expects cost and efficiency programs to remain stable"]


def test_report_explains_errors(fixtures, tmp_path):
    result = evaluate(fixtures)
    md = render_markdown(result)
    assert '**Precision on "yes": 0.500** — ❌ below the target of 0.8' in md
    assert "## False positives (1)" in md and "expects cost and efficiency programs" in md
    assert "ia_hiring** — no candidate snippets (data or prefilter); hint: RPA developer jobs" in md
    assert "quote_not_found: 1" in md
    md_path, json_path = write_report(result, tmp_path / "reports")
    assert md_path.name == "2026-09-25-extract_signals-v1.md"
    data = json.loads(json_path.read_text())
    assert {d["outcome"] for d in data["decisions"]} == {"TP", "FP", "FN", "TN", "not_evaluated"}


def test_compute_metrics_by_category():
    def d(key, category, expected, predicted):
        return Decision(
            company_domain="x.com",
            service="s",
            question_key=key,
            category=category,
            polarity="positive",
            expected=expected,
            predicted=predicted,
        )

    m = compute_metrics(
        [d("a", "hiring", "yes", "yes"), d("b", "hiring", "no", "unclear"), d("c", "incident", "yes", "no")],
        verified_total=4,
        rejected_by_reason={"wrong_subject": 1, "no_evidence_for_yes": 2},
        llm_calls=3,
        companies=1,
    )
    assert m.by_category["hiring"]["precision"] == 1.0 and m.by_category["incident"]["recall"] == 0.0
    assert m.abstention_rate == pytest.approx(0.333, abs=1e-3)
    assert m.hallucination_rate == 0.0 and m.evidence_total == 5  # no_evidence_for_yes is not a quote


def test_label_file_validation(tmp_path):
    good = {"company_domain": "a.com", "service": "s", "question_key": "q", "expected": "yes"}
    path = tmp_path / "l.jsonl"
    path.write_text(json.dumps(good) + "\n// comment\n" + json.dumps(good) + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_labels(path)
    path.write_text(json.dumps(good | {"expected": "maybe"}) + "\n")
    with pytest.raises(ValueError, match=re.escape("l.jsonl:1")):
        load_labels(path)


def test_eval_cli(fixtures, tmp_path, monkeypatch):
    golden = tmp_path / "golden.jsonl"
    golden.write_text("\n".join(lbl.model_dump_json() for lbl in labels()[:4]) + "\n")
    comps = tmp_path / "companies.yaml"
    comps.write_text("- {domain: dhl.com, name: DHL Group, fixture: dhl.jsonl, country: DE}\n")
    monkeypatch.setattr(cli, "make_llm", lambda live, cache_dir: (FakeLLM(llm_handler), None))
    monkeypatch.setenv("AI_MIN_COSINE", "0.5")
    args = [
        "eval",
        "--golden",
        str(golden),
        "--companies",
        str(comps),
        "--fixtures-dir",
        str(fixtures),
        "--fake-embeddings",
        "--now",
        "2026-09-25",
        "--out-dir",
        str(tmp_path / "out"),
    ]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "precision 0.5" in result.output and (tmp_path / "out").exists()
    assert CliRunner().invoke(cli.app, [*args, "--fail-under", "0.8"]).exit_code == 1


# --- the packaged golden set on committed fixtures (offline) ------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)  # fixtures were collected on 2026-09-25/26
CODE_CHECKED = {"wrong_subject", "homonym"}


@pytest.fixture(scope="module")
def golden():
    d = default_golden_dir()
    return load_labels(d / "mvp.jsonl"), load_companies(d / "companies.yaml")


@pytest.fixture(scope="module")
def fixture_docs(golden):
    _, cases = golden
    return {domain: load_parser_jsonl(FIXTURES / case.fixture) for domain, case in cases.items()}


def test_packaged_golden_set_is_consistent(golden, fixture_docs):
    lbls, cases = golden
    domains = {lbl.company_domain for lbl in lbls}
    assert len(lbls) >= 100 and len(domains) >= 5
    assert {"dhl.com", "lufthansagroup.com", "sap.com", "orange.com"} <= domains  # Annex A5 + both traps
    for lbl in lbls:
        assert lbl.company_domain in cases
        question = {q.key: q for q in load_preset(lbl.service).questions}[lbl.question_key]
        assert lbl.polarity == question.polarity
        assert lbl.expected == "no" or any(e.expected == "accept" for e in lbl.evidence)
        docs = fixture_docs[lbl.company_domain]
        for e in lbl.evidence:  # every labelled quote is verbatim in the named fixture document
            doc = next(d for d in docs if d.url == e.url)
            assert find_quote(e.quote, doc.text) or find_quote(e.quote, doc.title or ""), e.quote
    traps = [(lbl.company_domain, e.reason) for lbl in lbls for e in lbl.evidence if e.expected == "reject"]
    assert ("orange.com", "homonym") in traps and ("sap.com", "vendor") in traps


def _snippet(doc, n: int) -> Snippet:
    return Snippet(
        id=f"S{n}",
        chunk_id=uuid4(),
        document_id=doc.id,
        text=doc.text,
        char_start=0,
        char_end=len(doc.text),
        source_type=doc.source_type,
        source_name=doc.source_name,
        url=doc.url,
        title=doc.title,
        published_at=doc.published_at,
        fetched_at=doc.fetched_at,
        language=doc.language,
        meta=doc.meta,
    )


def test_golden_evidence_through_verification(golden, fixture_docs):
    """Item by item, a gullible model cites each labelled quote as being about the target company: supporting
    evidence must verify, and every trap that code can recognise (homonyms, other entities) must be rejected
    as wrong_subject — whatever the model said."""
    lbls, cases = golden
    accepted, rejected_traps = [], []
    for lbl in lbls:
        company = company_profile(cases[lbl.company_domain])
        bundle = load_preset(lbl.service).to_bundle()
        q = next(x for x in bundle.questions if x.key == lbl.question_key)
        bundle = bundle.model_copy(update={"questions": [q]})
        for item in lbl.evidence:
            if item.expected == "reject" and item.reason not in CODE_CHECKED:
                continue  # semantic traps (vendor, not an attack) are the model's call, see the oracle eval
            doc = next(d for d in fixture_docs[lbl.company_domain] if d.url == item.url)
            answer = Answer(
                question_id=str(q.id),
                answer="yes",
                confidence=0.9,
                rationale="r",
                evidence=[
                    Evidence(
                        snippet_id="S1",
                        quote=item.quote,
                        subject="target_company",
                        event_date=None,
                        strength="strong",
                        summary=item.quote[:200],
                    )
                ],
            )
            extraction = ServiceExtraction(answers={str(q.id): answer}, model="gullible")
            result = verify_extraction(
                extraction, bundle, [_snippet(doc, 1)], GOLDEN_NOW, "extract_signals@v1", company=company
            )
            if item.expected == "accept":
                accepted.append((item.quote, [r.reason for r in result.rejected]))
            else:
                rejected_traps.append((item.quote, [r.reason for r in result.rejected]))
    assert len(accepted) >= 40 and len(rejected_traps) >= 12
    assert [a for a in accepted if a[1]] == []  # no supporting evidence lost to the checks
    assert all(reasons == ["wrong_subject"] for _, reasons in rejected_traps), rejected_traps


def test_golden_oracle_eval_offline(golden, tmp_path):
    """The full pipeline on the committed fixtures with the golden-label oracle as the model: no trap may
    become a signal, the vendor is flagged, and the report is written."""
    lbls, cases = golden
    result = asyncio.run(
        run_eval(lbls, cases, FIXTURES, GoldenOracleLLM(lbls, cases), FakeEmbedder(), GOLDEN_NOW)
    )
    m = result.metrics
    assert m.evaluated == len(lbls) and m.not_evaluated == 0 and result.notes == []
    assert m.overall["precision"] == 1.0  # regression floor; measured 1.0 (18 TP, 0 FP)
    assert m.overall["recall"] >= 0.55  # regression floor; measured 0.621 with hash embeddings (prefilter)
    assert result.evidence_summary["traps_leaked"] == 0
    assert result.evidence_summary["accept_verified"] >= 20

    rules = {(s.company_domain, s.service): s.rules for s in result.scores}
    assert rules[("sap.com", "intelligent_automation")] == ["IT or software vendor"]
    assert rules[("sap.com", "cybersecurity")] == ["IT or software vendor"]
    orange = [d for d in result.decisions if d.company_domain == "orange.com" and d.predicted == "yes"]
    assert all("County" not in q for d in orange for q in d.quotes)

    md, _ = write_report(result, tmp_path)
    text = md.read_text()
    assert "## Labelled evidence and traps" in text and "Traps that became a signal: 0 of" in text
    assert "| sap.com | intelligent_automation |" in text and "IT or software vendor" in text
