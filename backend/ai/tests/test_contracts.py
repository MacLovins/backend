from datetime import UTC, date, datetime
from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

import leadradar_ai
from leadradar_ai import (
    AnalysisDocument,
    AnalysisInput,
    AnalysisStore,
    Collector,
    CompanyProfile,
    Embedder,
    ICPConfig,
    LeadScore,
    LLMCache,
    ProgressSink,
    QuestionConfig,
    Reason,
    RuleConfig,
    ScoreChange,
    ScoringProfile,
    ServiceBundle,
    StoredSignal,
    UsageSink,
)
from leadradar_ai.contracts import Answer, Polarity, SourceType, Stage, Strength, Tier, Weight

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


def make_question(**overrides) -> QuestionConfig:
    data = {
        "id": uuid4(),
        "key": "ia_ai_projects",
        "version": 1,
        "text": "AI, RPA or process mining initiatives?",
        "category": "ai_automation",
        "polarity": "positive",
        "weight": "high",
        "source_types": {"news", "website", "report"},
        "recency_days": 365,
    }
    return QuestionConfig(**(data | overrides))


def make_bundle() -> ServiceBundle:
    return ServiceBundle(
        service_id=uuid4(),
        key="intelligent_automation",
        name="Intelligent Automation",
        description="RPA, IDP, agentic AI, process mining",
        questions=[make_question()],
        icp=ICPConfig(countries=["DE"], employees_min=1000),
        rules=[],
        scoring=ScoringProfile(id=uuid4(), version=1),
    )


def test_canonical_enums_match_architecture():
    # ARCHITECTURE §4.7 — the same values in Python, OpenAPI and TS
    assert set(get_args(SourceType)) == {
        "news",
        "website",
        "jobs",
        "report",
        "registry",
        "incident",
        "derived",
        "manual",
    }
    assert set(get_args(Polarity)) == {"positive", "negative"}
    assert set(get_args(Weight)) == {"high", "medium", "low"}
    assert set(get_args(Strength)) == {"weak", "moderate", "strong"}
    assert set(get_args(Answer)) == {"yes", "no", "unclear"}
    assert set(get_args(Tier)) == {"hot", "warm", "cold", "disqualified"}
    assert set(get_args(Stage)) == {
        "resolving",
        "collecting",
        "indexing",
        "prefiltering",
        "extracting",
        "verifying",
        "scoring",
        "done",
        "failed",
        "paused",
    }


def test_scoring_profile_defaults_match_architecture():
    p = ScoringProfile(id=uuid4(), version=1)
    assert p.weights == {"high": 3, "medium": 2, "low": 1}
    assert p.strength_values == {"weak": 0.35, "moderate": 0.65, "strong": 1.0}
    assert (p.tau_intent, p.tau_risk) == (3.0, 2.0)
    assert (p.fit_exponent, p.intent_exponent, p.risk_penalty) == (0.4, 0.6, 0.5)
    assert p.tiers == {"hot": 65, "warm": 40}
    assert p.half_life_days["jobs"] == 45 and p.half_life_days["registry"] is None
    assert p.reliability["headline_only"] == 0.6
    assert p.max_evidence_per_question == 3


def test_mutable_defaults_are_not_shared():
    a, b = ScoringProfile(id=uuid4(), version=1), ScoringProfile(id=uuid4(), version=1)
    a.weights["high"] = 10
    assert b.weights["high"] == 3


@pytest.mark.parametrize(
    "overrides",
    [
        {"weights": {"high": 3, "medium": 2}},
        {"strength_values": {"weak": 0.3, "strong": 1.0}},
        {"tiers": {"hot": 30, "warm": 40}},
        {"half_life_days": {"jobs": 0}},
        {"risk_penalty": 1.5},
        {"tau_intent": 0},
    ],
)
def test_scoring_profile_rejects_invalid(overrides):
    with pytest.raises(ValidationError):
        ScoringProfile(id=uuid4(), version=1, **overrides)


def test_question_validation():
    with pytest.raises(ValidationError):
        make_question(source_types=set())
    with pytest.raises(ValidationError):
        make_question(recency_days=0)
    with pytest.raises(ValidationError):
        make_question(polarity="neutral")


def test_icp_and_rule_validation():
    with pytest.raises(ValidationError):
        ICPConfig(employees_min=5000, employees_max=1000)
    with pytest.raises(ValidationError):
        RuleConfig(id=uuid4(), name="cap", kind="signal", condition={}, action="cap")
    RuleConfig(id=uuid4(), name="cap", kind="signal", condition={}, action="cap", cap_value=30)


def test_extra_fields_are_forbidden():
    with pytest.raises(ValidationError):
        CompanyProfile(id=uuid4(), name="DHL Group", domain="dhl.com", typo_field=1)


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValidationError):
        AnalysisDocument(
            id=uuid4(),
            source_type="news",
            source_name="gdelt",
            url="https://x",
            title=None,
            text="t",
            published_at=None,
            fetched_at=datetime(2026, 9, 25),  # noqa: DTZ001
            language="en",
        )


def test_analysis_input_json_round_trip():
    inp = AnalysisInput(
        run_id=uuid4(),
        company=CompanyProfile(id=uuid4(), name="DHL Group", domain="dhl.com", aliases=["Deutsche Post"]),
        services=[make_bundle()],
        now=NOW,
    )
    restored = AnalysisInput.model_validate_json(inp.model_dump_json())
    assert restored == inp
    assert restored.mode == "incremental"
    assert restored.services[0].questions[0].source_types == {"news", "website", "report"}


def test_analysis_input_needs_a_service():
    with pytest.raises(ValidationError):
        AnalysisInput(
            run_id=uuid4(), company=CompanyProfile(id=uuid4(), name="X", domain="x.com"), services=[], now=NOW
        )


def test_stored_signal_and_lead_score_round_trip():
    signal = StoredSignal(
        id=uuid4(),
        detected_at=NOW,
        question_id=uuid4(),
        question_key="ia_ai_projects",
        question_version=1,
        category="ai_automation",
        polarity="positive",
        document_id=uuid4(),
        chunk_id=None,
        url="https://dhl.com/strategy",
        source_type="website",
        source_name="website",
        quote="agentic AI for RFQ processing",
        quote_start=10,
        quote_end=39,
        summary="Uses agentic AI to process RFQs.",
        strength="strong",
        confidence=0.9,
        reliability=1.0,
        event_date=date(2026, 6, 18),
        published_at=NOW,
        flags={"fuzzy_quote"},
        model="m",
        prompt_version="v1",
    )
    assert StoredSignal.model_validate_json(signal.model_dump_json()) == signal
    assert signal.status == "active"

    score = LeadScore(
        company_id=uuid4(),
        service_id=uuid4(),
        scoring_profile_id=uuid4(),
        fit=92.0,
        intent=81.3,
        risk=38.1,
        priority=69.2,
        tier="hot",
        disqualified=False,
        rule_hits=[],
        fit_details=[],
        breakdown=[],
        why_now=[
            Reason(text=signal.summary, polarity="positive", signal_id=signal.id, date=date(2026, 6, 18))
        ],
        data_gaps=[],
        computed_at=NOW,
    )
    assert LeadScore.model_validate_json(score.model_dump_json()) == score
    with pytest.raises(ValidationError):
        StoredSignal.model_validate(signal.model_dump() | {"flags": {"made_up"}})


def test_score_change_tier_changed():
    ids = {"company_id": uuid4(), "service_id": uuid4()}
    assert ScoreChange(
        **ids, priority_before=None, priority_after=50, tier_before=None, tier_after="warm"
    ).tier_changed
    assert not ScoreChange(
        **ids, priority_before=41, priority_after=50, tier_before="warm", tier_after="warm"
    ).tier_changed


class _Impl:
    """Minimal structural implementation of every port."""

    async def resolve(self, company):
        return company

    async def collect(self, company, request):
        return []

    async def documents_without_chunks(self, company_id):
        return []

    async def save_chunks(self, chunks):
        return None

    async def load_snippets(self, company_id, since, source_types):
        return []

    async def get_fingerprint(self, company_id, service_id):
        return None

    async def save_extraction(self, run_id, company_id, service_id, fingerprint, signals, rejected):
        return None

    async def load_signals(self, company_id, service_id):
        return []

    async def save_score(self, run_id, score):
        return None

    async def emit(self, event):
        return None

    async def get(self, key):
        return None

    async def set(self, key, value, meta):
        return None

    async def record(self, call):
        return None

    async def used_today(self, model):
        return 0

    def embed_passages(self, texts):
        return []

    def embed_query(self, text):
        return []


@pytest.mark.parametrize("port", [Collector, AnalysisStore, ProgressSink, LLMCache, UsageSink, Embedder])
def test_ports_are_structural(port):
    assert isinstance(_Impl(), port)
    assert not isinstance(object(), port)


def test_public_api_exports_resolve():
    for name in leadradar_ai.__all__:
        assert getattr(leadradar_ai, name) is not None
