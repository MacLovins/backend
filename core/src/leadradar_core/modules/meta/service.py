"""Reference data (CO-16) and LLM usage (CO-21) for the UI."""

import types
from datetime import datetime, time, timedelta
from functools import cache
from typing import Any, Literal, Union, get_args, get_origin
from uuid import UUID
from zoneinfo import ZoneInfo

import leadradar_ai as ai
import leadradar_parser as parser
from leadradar_auth import Role
from leadradar_core.modules.intelligence.models import Document
from leadradar_core.modules.meta.models import LLMCall
from leadradar_core.modules.meta.schemas import (
    CountryOut,
    IndustryOut,
    LabelsOut,
    ModelUsageOut,
    PresetOut,
    TemperatureDefaultOut,
    UsageOut,
)
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

PACIFIC = ZoneInfo("America/Los_Angeles")  # Gemini daily quotas reset at midnight Pacific time (§4.8)
NOT_QUOTA_STATUSES = ("cache_hit", "rate_limited")  # same rule as the UsageSink adapter's used_today

# Canonical enums owned by core (ARCHITECTURE §4.7); the ai-owned ones are read from the ai contracts below
_RUN_KINDS = ("analyze", "discover", "rescore", "refresh")
_RUN_STATUSES = ("queued", "running", "succeeded", "partial", "failed", "cancelled")
_SIGNAL_FEEDBACK = ("correct", "incorrect", "irrelevant")
_LEAD_FEEDBACK = ("good_fit", "bad_fit")
_ANSWERS = ("yes", "no", "unclear")
_SIGNAL_STATUSES = ("active", "superseded", "rejected_by_user")

_LABEL_OVERRIDES = {
    "ai_automation": "AI & automation",
    "rejected_by_user": "Rejected by user",
    "good_fit": "Good fit",
    "bad_fit": "Bad fit",
    "quote_not_found": "Quote not found in source",
    "wrong_subject": "About another company",
    "below_confidence": "Low confidence",
    "no_evidence_for_yes": "No evidence for “yes”",
    "fuzzy_quote": "Approximate quote",
    "headline_only": "Headline only",
}


def _label(value: str) -> str:
    return _LABEL_OVERRIDES.get(value, value.replace("_", " ").capitalize())


def _labels(values: tuple[str, ...] | list[str]) -> dict[str, str]:
    return {v: _label(v) for v in values}


def literal_values(model: type[BaseModel], field: str) -> tuple[str, ...]:
    """Values of a Literal field of a public contract, unwrapping Optional / list / set."""
    annotation: Any = model.model_fields[field].annotation
    while get_origin(annotation) is not Literal:
        origin = get_origin(annotation)
        if origin in (list, set, frozenset, tuple, Union, types.UnionType):
            args = [a for a in get_args(annotation) if a is not type(None)]
            annotation = args[0]
        else:
            raise TypeError(f"{model.__name__}.{field} is not a Literal field")
    return tuple(str(v) for v in get_args(annotation))


@cache
def industries() -> list[IndustryOut]:
    return [
        IndustryOut(id=i.id, label=i.label, nace=list(i.nace), nis2=i.nis2, dora=i.dora)
        for i in parser.industry_taxonomy()
    ]


@cache
def countries() -> list[CountryOut]:
    return [
        CountryOut(code=c.code, name=c.label, is_eu=c.is_eu, languages=list(c.languages))
        for c in parser.country_catalog()
    ]


@cache
def presets() -> list[PresetOut]:
    out = []
    for key in ai.list_presets():
        preset = ai.load_preset(key)
        categories = sorted({q.category for q in preset.questions})
        out.append(
            PresetOut(
                key=preset.key,
                name=preset.name,
                description=preset.description,
                questions_count=len(preset.questions),
                categories=categories,
            )
        )
    return out


@cache
def temperature_defaults() -> list[TemperatureDefaultOut]:
    return [TemperatureDefaultOut(category=c, **guide) for c, guide in ai.DEFAULT_TEMPERATURE.items()]


@cache
def labels() -> LabelsOut:
    return LabelsOut(
        categories=dict(ai.SIGNAL_CATEGORIES),
        weights=_labels(literal_values(ai.QuestionConfig, "weight")),
        statuses=_labels(_SIGNAL_STATUSES),
        polarities=_labels(literal_values(ai.QuestionConfig, "polarity")),
        source_types=_labels(literal_values(ai.QuestionConfig, "source_types")),
        strengths=_labels(literal_values(ai.VerifiedSignal, "strength")),
        answers=_labels(_ANSWERS),
        tiers=_labels(literal_values(ai.LeadScore, "tier")),
        stages=_labels(literal_values(ai.ProgressEvent, "stage")),
        run_kinds=_labels(_RUN_KINDS),
        run_statuses=_labels(_RUN_STATUSES),
        signal_feedback=_labels(_SIGNAL_FEEDBACK),
        lead_feedback=_labels(_LEAD_FEEDBACK),
        roles=_labels(get_args(Role)),
        reject_reasons=_labels(literal_values(ai.RejectedEvidence, "reason")),
        signal_flags=_labels(literal_values(ai.VerifiedSignal, "flags")),
    )


# --- usage ----------------------------------------------------------------------------------------


def quota_day(now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) of the current quota day: midnight to midnight America/Los_Angeles."""
    local = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
    start = datetime.combine(local.date(), time.min, tzinfo=PACIFIC)
    end = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=PACIFIC)
    return start, end


def _pools(settings: ai.LLMSettings) -> dict[str, str]:
    out: dict[str, str] = {}
    for pool in ("main", "cheap"):
        try:
            models = settings.pool(pool)
        except Exception:
            continue
        for model in models:
            out.setdefault(model, pool)
    return out


async def usage(
    session: AsyncSession, org_id: UUID, *, llm_settings: ai.LLMSettings, now: datetime | None = None
) -> UsageOut:
    start, end = quota_day(now)
    quota_expr = case((LLMCall.status.in_(NOT_QUOTA_STATUSES), 0), else_=1)
    cache_expr = case(((LLMCall.cache_hit.is_(True)) | (LLMCall.status == "cache_hit"), 1), else_=0)
    error_expr = case((LLMCall.status.in_(("error", "invalid_output", "blocked")), 1), else_=0)
    rows = (
        await session.execute(
            select(
                LLMCall.model,
                func.count(LLMCall.id),
                func.coalesce(func.sum(quota_expr), 0),
                func.coalesce(func.sum(cache_expr), 0),
                func.coalesce(func.sum(error_expr), 0),
                func.coalesce(func.sum(LLMCall.input_tokens), 0),
                func.coalesce(func.sum(LLMCall.output_tokens), 0),
            )
            .where(LLMCall.org_id == org_id, LLMCall.created_at >= start, LLMCall.created_at < end)
            .group_by(LLMCall.model)
        )
    ).all()
    by_model = {
        r[0]: {
            "calls": int(r[1]),
            "quota_calls": int(r[2]),
            "cache_hits": int(r[3]),
            "errors": int(r[4]),
            "input_tokens": int(r[5]),
            "output_tokens": int(r[6]),
        }
        for r in rows
    }

    pools = _pools(llm_settings)
    model_names = list(pools)
    model_names += [m for m in llm_settings.limits_json if m not in model_names]
    model_names += sorted(m for m in by_model if m not in model_names)
    models: list[ModelUsageOut] = []
    for model in model_names:
        stats = by_model.get(model, {})
        limits = llm_settings.limits(model)
        quota_calls = stats.get("quota_calls", 0)
        models.append(
            ModelUsageOut(
                model=model,
                pool=pools.get(model),
                rpd_limit=limits.rpd,
                rpm_limit=limits.rpm,
                remaining=max(0, limits.rpd - quota_calls) if limits.rpd is not None else None,
                **stats,
            )
        )

    docs = (
        await session.execute(
            select(Document.source_type, func.count(Document.id))
            .where(Document.org_id == org_id, Document.fetched_at >= start, Document.fetched_at < end)
            .group_by(Document.source_type)
        )
    ).all()
    documents_by_source = {str(r[0]): int(r[1]) for r in docs}

    return UsageOut(
        day_start=start,
        resets_at=end,
        llm_calls_24h=sum(s["calls"] for s in by_model.values()),
        input_tokens_24h=sum(s["input_tokens"] for s in by_model.values()),
        output_tokens_24h=sum(s["output_tokens"] for s in by_model.values()),
        documents_scanned_24h=sum(documents_by_source.values()),
        models=models,
        documents_by_source=documents_by_source,
    )
