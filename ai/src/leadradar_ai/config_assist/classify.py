"""classify_industry@v1 (SPEC AI-18): industry ids for a company without industries, from its website text.

Cheap model pool, minimal thinking, cached by the gateway. The taxonomy is an input (core passes the parser's
industry taxonomy: id → label), so the ai package does not depend on parser. Ids outside the taxonomy are
dropped in code.
"""

from functools import cache
from uuid import UUID

from pydantic import BaseModel, Field

from leadradar_ai.contracts import CompanyProfile
from leadradar_ai.llm.types import LLMClient, LLMRequest
from leadradar_ai.prompts.loader import Prompt, load_prompt

PROMPT_NAME, PROMPT_VERSION_TAG = "classify_industry", "v1"
PROMPT_VERSION = f"{PROMPT_NAME}@{PROMPT_VERSION_TAG}"

DEFAULT_MAX_INDUSTRIES = 3
MAX_TEXT_CHARS = 6_000  # the head of the site says what the company does; keeps the call cheap


class ClassificationOutput(BaseModel):
    industry_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(default="", max_length=300)


class IndustryClassification(BaseModel):
    industry_ids: list[str]  # main industry first; empty when the text does not say
    confidence: float
    rationale: str
    model: str | None
    prompt_version: str = PROMPT_VERSION
    from_llm: bool = True  # False: no text to classify, or the model declined


def classification_prompt() -> Prompt:
    return load_prompt(PROMPT_NAME, PROMPT_VERSION_TAG)


@cache
def render_system(max_industries: int = DEFAULT_MAX_INDUSTRIES) -> str:
    return classification_prompt().render_system(max_industries=max_industries)


def render_user(company: CompanyProfile, text: str, taxonomy: dict[str, str]) -> str:
    return classification_prompt().render_user(
        industries=dict(sorted(taxonomy.items())),
        company={"name": company.name, "domain": company.domain},
        text=" ".join(text.split())[:MAX_TEXT_CHARS],
    )


def _empty(model: str | None, rationale: str) -> IndustryClassification:
    return IndustryClassification(
        industry_ids=[], confidence=0.0, rationale=rationale, model=model, from_llm=False
    )


async def classify_industry(
    llm: LLMClient,
    company: CompanyProfile,
    text: str,
    taxonomy: dict[str, str],
    *,
    max_industries: int = DEFAULT_MAX_INDUSTRIES,
    run_id: UUID | None = None,
) -> IndustryClassification:
    """Industry ids (from `taxonomy`, id → label) of `company` judged from its website `text`."""
    if not text.strip() or not taxonomy:
        return _empty(None, "no website text or taxonomy to classify with")
    result = await llm.generate(
        LLMRequest(
            purpose="classify_industry",
            prompt_version=PROMPT_VERSION,
            system=render_system(max_industries),
            user=render_user(company, text, taxonomy),
            output_model=ClassificationOutput,
            pool="cheap",
            thinking="minimal",
            run_id=run_id,
        )
    )
    if result.blocked or result.output is None:
        return _empty(result.model, "the model declined to answer")
    ids = [i.strip() for i in result.output.industry_ids if i.strip() in taxonomy]
    ids = list(dict.fromkeys(ids))[:max_industries]
    return IndustryClassification(
        industry_ids=ids,
        confidence=result.output.confidence if ids else 0.0,
        rationale=result.output.rationale.strip(),
        model=result.model,
    )
