import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from leadradar_ai import cli
from leadradar_ai.contracts import LLMCallRecord
from leadradar_ai.local import FileLLMCache, FileUsageSink, load_parser_jsonl
from leadradar_ai.testing import FakeLLM

runner = CliRunner()
NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
SNIPPET = re.compile(r'<snippet id="(S\d+)"[^>]*>(.*?)</snippet>', re.DOTALL)
QUESTION = re.compile(r'<question id="(Q\d+)"[^>]*>(.*?)</question>', re.DOTALL)


def parser_doc(url, text, source_type="website", source_name="website", title=None, days_ago=10, **extra):
    published = (NOW - timedelta(days=days_ago)).isoformat()
    return {
        "source_type": source_type,
        "source_name": source_name,
        "url": url,
        "canonical_url": url,
        "title": title,
        "text": text,
        "published_at": published,
        "fetched_at": NOW.isoformat(),
        "language": "en",
        "content_hash": f"h-{url}",
        "meta": extra,
    }


@pytest.fixture
def fixture_file(tmp_path):
    path = tmp_path / "dhl.jsonl"
    docs = [
        parser_doc(
            "https://www.dhl.com/strategy",
            "Under Strategy 2030 DHL uses agentic AI to process customer "
            "RFQs and automate operational communication across forwarding.",
            title="Strategy 2030",
        ),
        parser_doc(
            "https://dhl.wd3.myworkdayjobs.com/j/1",
            "We are hiring an RPA developer with UiPath experience to automate finance processes.",
            source_type="jobs",
            source_name="workday",
            title="RPA Developer",
        ),
    ]
    path.write_text("\n".join(json.dumps(d) for d in docs) + "\n", encoding="utf-8")
    return path


def smart_llm(request):
    user = request.user.split("</examples>")[-1]
    snippets = SNIPPET.findall(user)
    answers = []
    for qid, text in QUESTION.findall(user):
        topic = "agentic AI" if "AI, RPA" in text else "RPA developer" if "hiring" in text else None
        hit = next(((sid, body) for sid, body in snippets if topic and topic in body), None)
        if hit:
            start = hit[1].index(topic)
            answers.append(
                {
                    "question_id": qid,
                    "answer": "yes",
                    "confidence": 0.9,
                    "rationale": "r",
                    "evidence": [
                        {
                            "snippet_id": hit[0],
                            "quote": hit[1][max(0, start - 15) : start + 45],
                            "subject": "target_company",
                            "event_date": None,
                            "strength": "strong",
                            "summary": f"Signal about {topic}.",
                        }
                    ],
                }
            )
        else:
            answers.append(
                {"question_id": qid, "answer": "unclear", "confidence": 0.3, "rationale": "r", "evidence": []}
            )
    return {"answers": answers}


def test_presets_command():
    result = runner.invoke(cli.app, ["presets"])
    assert (
        result.exit_code == 0
        and "intelligent_automation" in result.output
        and "cybersecurity" in result.output
    )
    detail = runner.invoke(cli.app, ["presets", "cybersecurity"])
    assert "cy_incident" in detail.output and "rule: exclude" in detail.output


def test_analyze_prints_score_signals_and_writes_json(fixture_file, tmp_path, monkeypatch):
    llm = FakeLLM(smart_llm)
    monkeypatch.setattr(cli, "make_llm", lambda live, cache_dir: (llm, None))
    out_json = tmp_path / "out.json"
    result = runner.invoke(
        cli.app,
        [
            "analyze",
            "--fixture",
            str(fixture_file),
            "--domain",
            "dhl.com",
            "--name",
            "DHL Group",
            "--own-domain",
            "dhl.wd3.myworkdayjobs.com",
            "--country",
            "DE",
            "--industry",
            "logistics",
            "--employees",
            "590000",
            "--service",
            "intelligent_automation",
            "--now",
            "2026-09-25",
            "--fake-embeddings",
            "--json",
            str(out_json),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Intelligent Automation" in result.output and "Priority" in result.output
    assert "ia_ai_projects" in result.output and "agentic AI" in result.output
    assert "LLM calls: 1" in result.output
    payload = json.loads(out_json.read_text())
    assert {s["question_key"] for s in payload["signals"]} == {"ia_ai_projects", "ia_hiring"}
    assert payload["scores"][0]["intent"] > 0


def test_analyze_cache_only_without_cache_fails_the_service(fixture_file, tmp_path):
    result = runner.invoke(
        cli.app,
        [
            "analyze",
            "--fixture",
            str(fixture_file),
            "--domain",
            "dhl.com",
            "--service",
            "intelligent_automation",
            "--now",
            "2026-09-25",
            "--fake-embeddings",
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
    )
    assert result.exit_code == 1
    assert "cache-only" in result.output and "--live" in result.output


def test_expand_command(monkeypatch):
    llm = FakeLLM(
        [
            {
                "keywords": [{"language": "en", "terms": ["robotic process automation"]}],
                "job_titles": ["RPA Engineer"],
                "negative_terms": [],
            }
        ]
    )
    monkeypatch.setattr(cli, "make_llm", lambda live, cache_dir: (llm, None))
    result = runner.invoke(
        cli.app,
        ["expand", "--preset", "intelligent_automation", "--question", "ia_hiring", "--language", "en"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "robotic process automation" in data["keywords"]["en"] and "RPA Engineer" in data["job_titles"]
    bad = runner.invoke(cli.app, ["expand", "--preset", "intelligent_automation", "--question", "nope"])
    assert bad.exit_code != 0


def test_load_parser_jsonl_maps_documents(fixture_file, tmp_path):
    docs = load_parser_jsonl(fixture_file)
    assert [d.source_type for d in docs] == ["website", "jobs"]
    assert load_parser_jsonl(fixture_file)[0].id == docs[0].id  # stable ids
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"text": "x"}\n')
    with pytest.raises(ValueError, match="broken.jsonl:1"):
        load_parser_jsonl(broken)


def test_file_cache_and_usage_sink(tmp_path):
    cache = FileLLMCache(tmp_path / "llm")
    asyncio.run(cache.set("abcdef", {"output": {"x": 1}}, {"model": "m"}))
    assert asyncio.run(FileLLMCache(tmp_path / "llm").get("abcdef")) == {"output": {"x": 1}}
    assert asyncio.run(cache.get("missing")) is None

    usage = FileUsageSink(tmp_path / "usage.jsonl")
    base = {"purpose": "extract_signals", "model": "m1", "prompt_version": "v1"}
    for status in ("ok", "cache_hit", "rate_limited", "invalid_output"):
        asyncio.run(usage.record(LLMCallRecord(**base, status=status, cache_hit=status == "cache_hit")))
    assert asyncio.run(usage.used_today("m1")) == 2
    assert asyncio.run(FileUsageSink(tmp_path / "usage.jsonl").used_today("m2")) == 0
