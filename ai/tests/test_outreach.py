from datetime import UTC, datetime
from uuid import uuid4

import pytest
from leadradar_ai import (
    CompanyProfile,
    ICPConfig,
    OutreachRequest,
    ScoringProfile,
    ServiceBundle,
    VerifiedSignal,
    generate_outreach,
)
from leadradar_ai.llm.types import LLMResult


class FakeLLM:
    async def generate(self, request):
        return LLMResult(
            output=None,  # triggers fallback draft
            model="test-fake",
            cache_hit=False,
            blocked=False,
        )


@pytest.mark.asyncio
async def test_generate_outreach_fallback() -> None:
    company = CompanyProfile(
        id=uuid4(),
        name="DHL Group",
        domain="dhl.com",
        industry_ids=["logistics"],
        employees=500000,
    )
    service_id = uuid4()
    service = ServiceBundle(
        service_id=service_id,
        key="robotics",
        name="Robotics Automation Suite",
        description="Autonomous sorting and warehouse robotics.",
        value_proposition="Reduces sorting errors by 40% and speeds fulfillment by 3x.",
        questions=[],
        icp=ICPConfig(),
        rules=[],
        scoring=ScoringProfile(id=uuid4(), version=1),
    )
    question_id = uuid4()
    signals = [
        VerifiedSignal(
            question_id=question_id,
            question_key="logistics_automation",
            question_version=1,
            category="timing",
            polarity="positive",
            document_id=uuid4(),
            chunk_id=uuid4(),
            url="https://dhl.com/news/robotics",
            source_type="news",
            source_name="google_news",
            quote="DHL Group expands autonomous warehouse sorting across 12 European hubs.",
            quote_start=10,
            quote_end=82,
            summary="DHL investing heavily in warehouse sorting automation across Europe",
            strength="strong",
            confidence=0.92,
            reliability=0.95,
            event_date=datetime.now(UTC).date(),
            published_at=datetime.now(UTC),
            flags={"corroborated"},
            model="gemini-2.5-flash",
            prompt_version="extract_signals@v1",
        )
    ]

    llm = FakeLLM()
    draft = await generate_outreach(llm, company, service, signals, OutreachRequest(channel="email"))

    assert draft.channel == "email"
    assert "DHL" in draft.subject
    assert "warehouse sorting automation" in draft.body
    assert question_id in draft.referenced_signals
    assert len(draft.referenced_quotes) >= 1
    assert "warehouse sorting" in draft.referenced_quotes[0]
