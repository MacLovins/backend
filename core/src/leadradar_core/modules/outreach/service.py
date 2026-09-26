"""Outreach drafts (CO-A1): the API creates an outreach_job and enqueues it; the worker calls the LLM
(`ai.generate_outreach`) and stores the draft. No LLM call happens inside an HTTP request."""

from datetime import UTC, datetime
from uuid import UUID

import leadradar_ai as ai
from leadradar_core.adapters import mapping
from leadradar_core.errors import NotFoundException
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.config.models import Service
from leadradar_core.modules.intelligence.models import Signal
from leadradar_core.modules.intelligence.service import load_bundle
from leadradar_core.modules.leads.models import LeadScore
from leadradar_core.modules.outreach.models import OutreachJob
from leadradar_core.modules.outreach.schemas import OutreachDraftOut, OutreachGenerateIn, OutreachJobOut
from leadradar_core.worker.enqueue import enqueue_outreach
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

MAX_SIGNALS = 5
CHANNELS = ("email", "linkedin_inmail", "call_script")
TONES = ("professional", "conversational", "direct")


def job_out(job: OutreachJob) -> OutreachJobOut:
    return OutreachJobOut(
        id=job.id,
        company_id=job.company_id,
        service_id=job.service_id,
        status=job.status,
        draft=OutreachDraftOut.model_validate(job.result) if job.result else None,
        error=job.error,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


async def _pick_service(
    session: AsyncSession, org_id: UUID, company_id: UUID, service_id: UUID | None
) -> Service:
    if service_id is None:  # the company's top-priority service, else any active one
        service_id = (
            await session.execute(
                select(LeadScore.service_id)
                .where(
                    LeadScore.company_id == company_id,
                    LeadScore.org_id == org_id,
                    LeadScore.is_current.is_(True),
                )
                .order_by(LeadScore.priority.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if service_id is None:
        service_id = (
            await session.execute(
                select(Service.id).where(Service.org_id == org_id, Service.is_active.is_(True)).limit(1)
            )
        ).scalar_one_or_none()
    if service_id is None:
        raise NotFoundException("No active service configured")
    service = await session.get(Service, service_id)
    if service is None or service.org_id != org_id:
        raise NotFoundException("Service not found")
    return service


async def create_job(
    session: AsyncSession, org_id: UUID, user_id: UUID | None, company_id: UUID, data: OutreachGenerateIn
) -> OutreachJob:
    company = await session.get(Company, company_id)
    if company is None or company.org_id != org_id:
        raise NotFoundException("Company not found")
    service = await _pick_service(session, org_id, company_id, data.service_id)
    job = OutreachJob(
        org_id=org_id,
        company_id=company_id,
        service_id=service.id,
        status="queued",
        request=data.model_dump(mode="json", exclude={"service_id"}),
        created_by=user_id,
    )
    session.add(job)
    await session.commit()
    await enqueue_outreach(job.id)
    return job


async def get_job(session: AsyncSession, org_id: UUID, company_id: UUID, job_id: UUID) -> OutreachJob:
    job = await session.get(OutreachJob, job_id)
    if job is None or job.org_id != org_id or job.company_id != company_id:
        raise NotFoundException("Outreach job not found")
    return job


def _verified_signal(s: Signal) -> ai.VerifiedSignal:
    return ai.VerifiedSignal(
        question_id=s.question_id,
        question_key=s.question_key,
        question_version=s.question_version,
        category=s.category,
        polarity=s.polarity,
        document_id=s.document_id or s.id,
        chunk_id=s.chunk_id,
        url=s.url or "",
        source_type=s.source_type if s.source_type in ai.contracts.SourceType.__args__ else "news",
        source_name=s.source_name,
        quote=s.quote,
        quote_start=s.quote_start,
        quote_end=s.quote_end,
        summary=s.summary,
        strength=s.strength if s.strength in ("weak", "moderate", "strong") else "moderate",
        confidence=float(s.confidence),
        reliability=float(s.reliability) if s.reliability is not None else 0.8,
        event_date=s.event_date,
        published_at=s.published_at,
        flags=set(s.flags or []),
        model=s.model or "unknown",
        prompt_version=s.prompt_version or "extract_signals@v1",
    )


def _request(data: dict) -> ai.OutreachRequest:
    return ai.OutreachRequest(
        channel=data.get("channel") if data.get("channel") in CHANNELS else "email",
        language=data.get("language") or "en",
        tone=data.get("tone") if data.get("tone") in TONES else "professional",
        sender_name=data.get("sender_name"),
        sender_title=data.get("sender_title"),
        sender_company=data.get("sender_company") or "LeadRadar",
    )


async def run_job(session: AsyncSession, job_id: UUID, llm: ai.LLMClient) -> str:
    """Worker side: generate and store the draft. Returns the final job status ("skipped" if not queued)."""
    job = await session.get(OutreachJob, job_id)
    if job is None or job.status != "queued":
        return "skipped"
    job.status, job.started_at = "running", datetime.now(UTC)
    await session.commit()
    try:
        company = await session.get(Company, job.company_id)
        service = await session.get(Service, job.service_id) if job.service_id else None
        if company is None or service is None:
            raise LookupError("The company or the service no longer exists")
        bundle = await load_bundle(session, service)
        rows = (
            (
                await session.execute(
                    select(Signal)
                    .where(
                        Signal.company_id == job.company_id,
                        Signal.service_id == service.id,
                        Signal.org_id == job.org_id,
                        Signal.status == "active",
                    )
                    .order_by(Signal.confidence.desc())
                    .limit(MAX_SIGNALS)
                )
            )
            .scalars()
            .all()
        )
        draft = await ai.generate_outreach(
            llm,
            mapping.company_profile(company),
            bundle,
            [_verified_signal(s) for s in rows],
            _request(job.request or {}),
        )
        job.result = OutreachDraftOut.model_validate(draft.model_dump()).model_dump(mode="json")
        job.status = "succeeded"
    except Exception as e:
        await session.rollback()
        job = await session.get(OutreachJob, job_id)
        job.status, job.error = "failed", f"{type(e).__name__}: {e}"
    job.finished_at = datetime.now(UTC)
    await session.commit()
    return job.status
