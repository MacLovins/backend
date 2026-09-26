"""POST /services/{id}/questions/suggest — draft signal questions and rules with ai.suggest_questions (AI-17).

Admin only. Returns drafts shaped like SignalQuestionCreate / DisqualificationRuleCreate; nothing is saved —
the admin reviews them and creates the ones they keep through the regular config endpoints.
"""

from typing import Annotated, Any
from uuid import UUID

import leadradar_ai as ai
from fastapi import APIRouter, Depends, HTTPException, status
from leadradar_auth.dependencies import get_current_principal, require_roles
from leadradar_auth.schemas import Principal
from leadradar_core.adapters.llm import SqlLLMCache, SqlUsageSink
from leadradar_core.db.session import async_session_factory, get_db_session
from leadradar_core.modules.config.models import Service
from leadradar_core.modules.intelligence.service import load_bundle
from leadradar_core.settings import settings
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["config"])

_llm: ai.LLMClient | None = None


def get_assist_llm() -> ai.LLMClient:
    """The LLM gateway for config helpers (cheap pool); built once per process. Overridden in tests."""
    global _llm
    if _llm is None:
        _llm = ai.GeminiClient.from_settings(
            ai.LLMSettings(),
            usage=SqlUsageSink(async_session_factory, settings.DEFAULT_ORG_ID),
            cache=SqlLLMCache(async_session_factory, settings.DEFAULT_ORG_ID),
        )
    return _llm


class SuggestedQuestionOut(BaseModel):
    key: str
    label: str
    text: str
    category: str
    polarity: str
    weight: str
    source_types: list[str]
    recency_days: int
    keywords: dict[str, list[str]]
    job_titles: list[str]
    negative_terms: list[str]


class SuggestedRuleOut(BaseModel):
    name: str
    kind: str
    condition: dict[str, Any]
    action: str
    cap_value: float | None


class QuestionSuggestionsOut(BaseModel):
    questions: list[SuggestedQuestionOut]
    rules: list[SuggestedRuleOut]
    model: str | None
    prompt_version: str


@router.post(
    "/services/{id}/questions/suggest",
    response_model=QuestionSuggestionsOut,
    dependencies=[Depends(require_roles("admin"))],
)
async def suggest_service_questions(
    id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    llm: Annotated[ai.LLMClient, Depends(get_assist_llm)],
) -> QuestionSuggestionsOut:
    service = await session.get(Service, id)
    if not service or service.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    bundle = await load_bundle(session, service)
    await session.rollback()  # load_bundle may stage a default scoring profile: this endpoint saves nothing
    try:
        result = await ai.suggest_questions(llm, bundle)
    except ai.QuotaExhausted as e:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e)) from e
    except (ai.LLMUnavailable, ai.LLMBadRequest) as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e
    return QuestionSuggestionsOut(
        questions=[
            SuggestedQuestionOut(
                key=s.question.key,
                label=s.label,
                text=s.question.text,
                category=s.question.category,
                polarity=s.question.polarity,
                weight=s.question.weight,
                source_types=sorted(s.question.source_types),
                recency_days=s.question.recency_days,
                keywords=s.question.keywords,
                job_titles=s.question.job_titles,
                negative_terms=s.question.negative_terms,
            )
            for s in result.questions
        ],
        rules=[
            SuggestedRuleOut(
                name=r.name, kind=r.kind, condition=r.condition, action=r.action, cap_value=r.cap_value
            )
            for r in result.rules
        ],
        model=result.model,
        prompt_version=result.prompt_version,
    )
