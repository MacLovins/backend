"""Seeding helpers for feedback / leads / config API tests (real database, no LLM, no network).

Every test uses its own org id, so tests do not interfere with each other on the shared test database.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import leadradar_ai as ai
from leadradar_auth.schemas import Principal
from leadradar_auth.security import create_access_token
from leadradar_core.adapters import mapping
from leadradar_core.db.session import async_session_factory
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.config.models import Service, SignalQuestion
from leadradar_core.modules.config.presets import create_service_from_preset
from leadradar_core.modules.intelligence.models import Document, Signal
from leadradar_core.modules.intelligence.service import rescore_company
from leadradar_core.modules.runs.models import AnalysisRun
from sqlalchemy import select

NOW = datetime.now(UTC)


def headers(org_id: UUID, role: str = "admin", user_id: UUID | None = None) -> dict[str, str]:
    principal = Principal(user_id=user_id or org_id, org_id=org_id, email="t@x.io", role=role)
    return {"Authorization": f"Bearer {create_access_token(principal)}"}


async def ensure_service(org_id: UUID, preset: str = "intelligent_automation") -> UUID:
    async with async_session_factory() as session, session.begin():
        service, _ = await create_service_from_preset(session, org_id, preset)
        return service.id


def verified_signal(
    question: SignalQuestion,
    document: Document,
    quote: str,
    *,
    strength: str = "strong",
    event_date: date | None = None,
) -> ai.VerifiedSignal:
    return ai.VerifiedSignal(
        question_id=question.id,
        question_key=question.key,
        question_version=question.version,
        category=question.category,
        polarity=question.polarity,
        document_id=document.id,
        chunk_id=None,
        url=document.url,
        source_type=document.source_type,
        source_name=document.source_name,
        quote=quote,
        quote_start=None,
        quote_end=None,
        summary=f"Summary: {quote}",
        strength=strength,
        confidence=0.9,
        reliability=0.8,
        event_date=event_date,
        published_at=document.published_at,
        flags=set(),
        model="fake",
        prompt_version="test",
    )


@dataclass
class Seeded:
    org_id: UUID
    service_id: UUID
    company_id: UUID
    document_id: UUID
    signal_ids: list[UUID] = field(default_factory=list)


async def seed_lead(
    org_id: UUID,
    *,
    service_id: UUID | None = None,
    name: str = "Acme Logistics",
    country: str = "DE",
    industries: tuple[str, ...] = ("logistics",),
    employees: int = 20000,
    revenue_eur: int | None = 1_500_000_000,
    signals: tuple[tuple[str, str], ...] = (("ia_ai_projects", "Acme uses agentic AI for RFQs"),),
    detected_days_ago: float = 0,
    event_date: date | None = None,
    source_type: str = "news",
) -> Seeded:
    """Company + document + active signals (question key, quote) + a current lead score."""
    service_id = service_id or await ensure_service(org_id)
    async with async_session_factory() as session, session.begin():
        company = Company(
            org_id=org_id,
            name=name,
            domain=f"{name.lower().replace(' ', '-')}-{uuid4().hex[:6]}.example",
            country_code=country,
            industry_ids=list(industries),
            employees=employees,
            revenue_eur=revenue_eur,
        )
        session.add(company)
        await session.flush()
        url = f"https://news.example.com/{uuid4().hex[:8]}"
        document = Document(
            org_id=org_id,
            company_id=company.id,
            source_type=source_type,
            source_name="gdelt",
            url=url,
            canonical_url=url,
            title="t",
            text=" ".join(q for _, q in signals) or "nothing",
            published_at=NOW - timedelta(days=5),
            content_hash=uuid4().hex,
        )
        session.add(document)
        await session.flush()
        questions = {
            q.key: q
            for q in (
                await session.execute(select(SignalQuestion).where(SignalQuestion.service_id == service_id))
            ).scalars()
        }
        rows = []
        for key, quote in signals:
            row = mapping.signal_row(
                verified_signal(questions[key], document, quote, event_date=event_date),
                company.id,
                service_id,
                org_id,
                None,
            )
            row.detected_at = NOW - timedelta(days=detected_days_ago)
            rows.append(row)
        session.add_all(rows)
        await session.flush()
        await rescore_company(session, org_id, company.id, service_id)
        return Seeded(org_id, service_id, company.id, document.id, [r.id for r in rows])


async def question(service_id: UUID, key: str) -> SignalQuestion:
    async with async_session_factory() as session:
        return (
            await session.execute(
                select(SignalQuestion).where(
                    SignalQuestion.service_id == service_id, SignalQuestion.key == key
                )
            )
        ).scalar_one()


async def signal(signal_id: UUID) -> Signal:
    async with async_session_factory() as session:
        return await session.get(Signal, signal_id)


async def service(service_id: UUID) -> Service:
    async with async_session_factory() as session:
        return await session.get(Service, service_id)


async def create_run(org_id: UUID) -> UUID:
    async with async_session_factory() as session, session.begin():
        run = AnalysisRun(org_id=org_id, kind="analyze", status="running", params={}, progress={})
        session.add(run)
        await session.flush()
        return run.id
