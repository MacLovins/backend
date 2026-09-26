from functools import cache
from typing import Annotated

import leadradar_ai as ai
from fastapi import APIRouter, Depends
from leadradar_auth.dependencies import get_current_principal
from leadradar_auth.schemas import Principal
from leadradar_core.db.session import get_db_session
from leadradar_core.modules.meta import service as meta_service
from leadradar_core.modules.meta.schemas import (
    CountryOut,
    IndustryOut,
    LabelsOut,
    PresetOut,
    TemperatureDefaultOut,
    UsageOut,
)
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/meta", tags=["meta"], dependencies=[Depends(get_current_principal)])


@cache
def get_llm_settings() -> ai.LLMSettings:
    """Configured LLM pools and limits (LLM_MAIN_MODELS, LLM_CHEAP_MODELS, LLM_LIMITS_JSON)."""
    return ai.LLMSettings()


@router.get("/industries", response_model=list[IndustryOut])
async def get_industries() -> list[IndustryOut]:
    """Industry taxonomy of the parser (ids used in ICP, companies and discovery)."""
    return meta_service.industries()


@router.get("/countries", response_model=list[CountryOut])
async def get_countries() -> list[CountryOut]:
    return meta_service.countries()


@router.get("/presets", response_model=list[PresetOut])
async def get_presets() -> list[PresetOut]:
    return meta_service.presets()


@router.get("/temperature", response_model=list[TemperatureDefaultOut])
async def get_temperature_defaults() -> list[TemperatureDefaultOut]:
    """Signal temperature per category: what cold (weak), medium (moderate) and hot (strong) evidence is."""
    return meta_service.temperature_defaults()


@router.get("/labels", response_model=LabelsOut)
async def get_labels() -> LabelsOut:
    """UI labels: signal categories (ai presets) and canonical enums (ARCHITECTURE §4.7)."""
    return meta_service.labels()


@router.get("/usage", response_model=UsageOut)
async def get_usage(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    llm_settings: Annotated[ai.LLMSettings, Depends(get_llm_settings)],
) -> UsageOut:
    """LLM calls and tokens per model for the current quota day (Pacific time) against configured limits."""
    return await meta_service.usage(session, principal.org_id, llm_settings=llm_settings)
