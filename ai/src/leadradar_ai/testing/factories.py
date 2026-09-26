"""Factories with sensible defaults for tests: override only what the test is about."""

from datetime import UTC, date, datetime
from itertools import count
from uuid import UUID, uuid4

from leadradar_ai.contracts import (
    AnalysisDocument,
    CompanyProfile,
    ICPConfig,
    QuestionConfig,
    RuleConfig,
    ScoringProfile,
    ServiceBundle,
    Snippet,
    StoredSignal,
)

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


def make_company(**overrides) -> CompanyProfile:
    data = {
        "id": uuid4(),
        "name": "DHL Group",
        "domain": "dhl.com",
        "aliases": ["Deutsche Post DHL"],
        "country_code": "DE",
        "industry_ids": ["logistics"],
        "employees": 590_000,
    }
    return CompanyProfile(**(data | overrides))


def make_question(**overrides) -> QuestionConfig:
    data = {
        "id": uuid4(),
        "key": "ia_ai_projects",
        "version": 1,
        "text": "Is the company running or planning AI, RPA, agentic AI or process mining initiatives?",
        "category": "ai_automation",
        "polarity": "positive",
        "weight": "high",
        "source_types": {"news", "website", "report"},
        "recency_days": 365,
    }
    return QuestionConfig(**(data | overrides))


def make_profile(**overrides) -> ScoringProfile:
    return ScoringProfile(**({"id": uuid4(), "version": 1} | overrides))


def make_bundle(
    questions: list[QuestionConfig] | None = None,
    icp: ICPConfig | None = None,
    rules: list[RuleConfig] | None = None,
    scoring: ScoringProfile | None = None,
    **overrides,
) -> ServiceBundle:
    data = {
        "service_id": uuid4(),
        "key": "intelligent_automation",
        "name": "Intelligent Automation",
        "description": "RPA, IDP, agentic AI and process mining",
        "questions": questions if questions is not None else [make_question()],
        "icp": icp or ICPConfig(),
        "rules": rules or [],
        "scoring": scoring or make_profile(),
    }
    return ServiceBundle(**(data | overrides))


# Distinct stories by default: identical quotes would be collapsed as reprints of one story (V5)
_QUOTES = [
    "We use agentic AI to process customer RFQs.",
    "The group opened a shared service centre in Krakow last spring.",
    "Our new CIO will lead the digital transformation programme.",
    "Hiring: Senior RPA Developer for finance operations in Bonn.",
    "Capital markets day outlined a two billion euro efficiency target.",
    "Process mining rollout covers procurement and accounts payable.",
]
_quote_counter = count()


def make_signal(question: QuestionConfig, **overrides) -> StoredSignal:
    data = {
        "id": uuid4(),
        "detected_at": NOW,
        "question_id": question.id,
        "question_key": question.key,
        "question_version": question.version,
        "category": question.category,
        "polarity": question.polarity,
        "document_id": uuid4(),
        "chunk_id": None,
        "url": "https://www.dhl.com/strategy-2030",
        "source_type": "website",
        "source_name": "website",
        "quote": _QUOTES[next(_quote_counter) % len(_QUOTES)],
        "quote_start": 0,
        "quote_end": 43,
        "summary": "Uses agentic AI to process customer RFQs.",
        "strength": "strong",
        "confidence": 1.0,
        "reliability": 1.0,
        "event_date": NOW.date(),
        "published_at": NOW,
        "model": "fake",
        "prompt_version": "extract_signals@v1",
    }
    return StoredSignal(**(data | overrides))


def make_document(
    company_id: UUID | None = None, published: date | datetime | None = NOW, **overrides
) -> AnalysisDocument:
    if isinstance(published, date) and not isinstance(published, datetime):
        published = datetime(published.year, published.month, published.day, tzinfo=UTC)
    data = {
        "id": uuid4(),
        "source_type": "news",
        "source_name": "gdelt",
        "url": f"https://news.example.com/{uuid4().hex[:8]}",
        "title": "DHL expands agentic AI",
        "text": "DHL Group expands agentic AI across customer service.",
        "published_at": published,
        "fetched_at": NOW,
        "language": "en",
    }
    return AnalysisDocument(**(data | overrides))


def make_snippet(**overrides) -> Snippet:
    text = overrides.pop(
        "text", "Since March 2026 agentic AI processes incoming customer RFQs in freight forwarding."
    )
    data = {
        "id": "S1",
        "chunk_id": uuid4(),
        "document_id": uuid4(),
        "text": text,
        "char_start": 1000,
        "char_end": 1000 + len(text),
        "source_type": "website",
        "source_name": "website",
        "url": "https://www.dhl.com/strategy-2030",
        "title": "Strategy 2030",
        "published_at": NOW,
        "fetched_at": NOW,
        "language": "en",
    }
    return Snippet(**(data | overrides))
