from datetime import UTC, datetime, timedelta
from typing import Annotated

import leadradar_ai as ai
import leadradar_parser as parser
from fastapi import APIRouter, Depends
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.intelligence.models import Document
from leadradar_core.modules.meta.models import LLMCall
from leadradar_core.modules.meta.schemas import (
    CountryOut,
    IndustryOut,
    LabelsOut,
    ModelUsageOut,
    PresetOut,
    UsageOut,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/meta", tags=["meta"])

DEFAULT_COUNTRIES = [
    CountryOut(code="DE", name="Germany", is_eu=True),
    CountryOut(code="FR", name="France", is_eu=True),
    CountryOut(code="NL", name="Netherlands", is_eu=True),
    CountryOut(code="AT", name="Austria", is_eu=True),
    CountryOut(code="CH", name="Switzerland", is_eu=False),
    CountryOut(code="GB", name="United Kingdom", is_eu=False),
    CountryOut(code="SE", name="Sweden", is_eu=True),
    CountryOut(code="ES", name="Spain", is_eu=True),
    CountryOut(code="IT", name="Italy", is_eu=True),
    CountryOut(code="PL", name="Poland", is_eu=True),
]

DEFAULT_PRESETS = [
    PresetOut(
        key="intelligent_automation",
        name="Intelligent Automation",
        description="RPA, AI agents, process mining, cost reduction initiatives in European enterprise.",
    ),
    PresetOut(
        key="cybersecurity",
        name="Cybersecurity & Compliance",
        description="NIS2, DORA compliance, security breaches, CISO appointments, SOC modernization.",
    ),
]


@router.get("/industries", response_model=list[IndustryOut])
async def get_industries() -> list[IndustryOut]:
    """The parser taxonomy: the ids companies, ICPs, rules and NIS2/DORA derivation actually use."""
    return [IndustryOut(id=i.id, label=i.label) for i in parser.industry_taxonomy()]


@router.get("/countries", response_model=list[CountryOut])
async def get_countries() -> list[CountryOut]:
    return DEFAULT_COUNTRIES


@router.get("/presets", response_model=list[PresetOut])
async def get_presets() -> list[PresetOut]:
    return DEFAULT_PRESETS


@router.get("/labels", response_model=LabelsOut)
async def get_labels() -> LabelsOut:
    return LabelsOut(
        categories={
            "ai_automation": "AI & Automation Projects",
            "hiring": "Hiring & Talent Growth",
            "leadership": "Leadership & Strategy Shifts",
            "tech_stack": "Technology Stack in Use",
            "compliance": "Compliance & Regulations (NIS2/DORA)",
            "incident": "Security Incidents & Breaches",
        },
        weights={
            "high": "High (+3.0)",
            "medium": "Medium (+2.0)",
            "low": "Low (+1.0)",
        },
        statuses={
            "active": "Active",
            "superseded": "Superseded",
            "rejected_by_user": "Rejected by User",
        },
    )


@router.get("/usage", response_model=UsageOut)
async def get_usage(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> UsageOut:
    """LLM calls and tokens of the last 24 h by model against the free-tier limits (calls today count toward
    the daily quota, which resets at midnight Pacific), and documents collected by source type (SPEC CO-21)."""
    since = datetime.now(UTC) - timedelta(hours=24)
    llm = ai.LLMSettings()
    rows = (
        await session.execute(
            select(
                LLMCall.model,
                func.count(LLMCall.id),
                func.count(LLMCall.id).filter(LLMCall.cache_hit.is_(True)),
                func.count(LLMCall.id).filter(LLMCall.status.not_in(["ok", "cache_hit"])),
                func.coalesce(func.sum(LLMCall.input_tokens), 0),
                func.coalesce(func.sum(LLMCall.output_tokens), 0),
            )
            .where(LLMCall.org_id == principal.org_id, LLMCall.created_at >= since)
            .group_by(LLMCall.model)
        )
    ).all()
    by_model = [
        ModelUsageOut(
            model=model,
            calls=calls,
            cache_hits=hits,
            errors=errors,
            input_tokens=int(tokens_in),
            output_tokens=int(tokens_out),
            rpd_limit=llm.limits(model).rpd,
        )
        for model, calls, hits, errors, tokens_in, tokens_out in rows
    ]
    docs = dict(
        (
            await session.execute(
                select(Document.source_type, func.count(Document.id))
                .where(Document.org_id == principal.org_id, Document.fetched_at >= since)
                .group_by(Document.source_type)
            )
        ).all()
    )
    return UsageOut(
        llm_calls_24h=sum(m.calls - m.cache_hits for m in by_model),
        input_tokens_24h=sum(m.input_tokens for m in by_model),
        output_tokens_24h=sum(m.output_tokens for m in by_model),
        documents_scanned_24h=sum(docs.values()),
        by_model=by_model,
        documents_by_source=docs,
    )
