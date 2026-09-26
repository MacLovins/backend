"""suggest_questions@v1 (SPEC AI-17): draft signal questions (8 to 12, at least 2 negatives) and 2 disqualification rules
from the service description and its ICP.

Cheap model pool, minimal thinking, cached by the gateway. The model drafts; code decides what is valid:
unknown categories and source types, duplicate or existing keys and rules that fail RuleConfig validation are
dropped, recency is clamped. The result is drafts only — nothing is saved; an admin reviews them.
"""

import re
from functools import cache
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, ValidationError

from leadradar_ai.contracts import QuestionConfig, RuleConfig, ServiceBundle, SourceType
from leadradar_ai.llm.types import LLMClient, LLMRequest
from leadradar_ai.presets.loader import SIGNAL_CATEGORIES
from leadradar_ai.prompts.loader import Prompt, load_prompt

PROMPT_NAME, PROMPT_VERSION_TAG = "suggest_questions", "v1"
PROMPT_VERSION = f"{PROMPT_NAME}@{PROMPT_VERSION_TAG}"

MIN_QUESTIONS, MAX_QUESTIONS = 8, 12
MAX_RULES = 2
MAX_KEYWORDS = 8
RECENCY_RANGE = (30, 730)
SUGGESTABLE_SOURCES: frozenset[SourceType] = frozenset(
    {"news", "website", "report", "jobs", "registry", "incident"}
)
_KEY = re.compile(r"[^a-z0-9]+")


# --- LLM schema -----------------------------------------------------------------------------------


class DraftQuestion(BaseModel):
    key: str
    label: str
    text: str
    category: str
    polarity: Literal["positive", "negative"]
    weight: Literal["high", "medium", "low"]
    source_types: list[str]
    recency_days: int
    keywords: list[str] = Field(default_factory=list)


class DraftRule(BaseModel):
    name: str
    kind: Literal["firmographic", "signal"]
    field: str | None = Field(
        default=None, description="firmographic: employees, revenue_eur, country_code, …"
    )
    op: str | None = Field(default=None, description="firmographic: lt, gt, eq, in, not_in, intersects")
    value: str | None = Field(default=None, description="firmographic: one value; lists comma-separated")
    question_key: str | None = Field(default=None, description="signal: key of one of the drafted questions")
    min_strength: float | None = None
    action: Literal["exclude", "cap", "flag"]
    cap_value: float | None = None


class SuggestionOutput(BaseModel):
    questions: list[DraftQuestion]
    rules: list[DraftRule] = Field(default_factory=list)


# --- result ---------------------------------------------------------------------------------------


class SuggestedQuestion(BaseModel):
    """A draft ready for review: the question config plus a short UI label."""

    label: str
    question: QuestionConfig


class QuestionSuggestions(BaseModel):
    questions: list[SuggestedQuestion]
    rules: list[RuleConfig]
    model: str | None
    prompt_version: str = PROMPT_VERSION
    from_llm: bool = True  # False: the model declined (blocked) — nothing to suggest


def suggestion_prompt() -> Prompt:
    return load_prompt(PROMPT_NAME, PROMPT_VERSION_TAG)


@cache
def render_system() -> str:
    return suggestion_prompt().render_system(min_questions=MIN_QUESTIONS, max_questions=MAX_QUESTIONS)


def render_user(service: ServiceBundle) -> str:
    preferred = [str(v) for c in service.icp.nice_to_have if c.kind == "industry_in" for v in c.values]
    return suggestion_prompt().render_user(
        service={
            "name": service.name,
            "description": service.description,
            "value_proposition": service.value_proposition,
        },
        icp={
            "countries": service.icp.countries,
            "industries": service.icp.industries_any,
            "employees_min": service.icp.employees_min,
            "preferred_industries": preferred,
        },
        categories=SIGNAL_CATEGORIES,
        existing_keys=[q.key for q in service.questions],
        existing_questions=[q.text for q in service.questions],
    )


def _key(raw: str) -> str:
    key = _KEY.sub("_", raw.strip().lower()).strip("_")[:30]
    if key and not key[0].isalpha():
        key = f"q_{key}"[:30]
    return key


def _question(draft: DraftQuestion, taken: set[str]) -> SuggestedQuestion | None:
    key = _key(draft.key or draft.label)
    sources = {s.strip().lower() for s in draft.source_types} & SUGGESTABLE_SOURCES
    if not key or key in taken or draft.category not in SIGNAL_CATEGORIES or not sources:
        return None
    keywords = list(dict.fromkeys(k.strip() for k in draft.keywords if k.strip()))[:MAX_KEYWORDS]
    try:
        question = QuestionConfig(
            id=uuid4(),
            key=key,
            version=1,
            text=draft.text.strip(),
            category=draft.category,
            polarity=draft.polarity,
            weight=draft.weight,
            source_types=sources,
            recency_days=min(max(draft.recency_days, RECENCY_RANGE[0]), RECENCY_RANGE[1]),
            keywords={"en": keywords} if keywords else {},
        )
    except ValidationError:
        return None
    taken.add(key)
    return SuggestedQuestion(label=draft.label.strip() or key, question=question)


def _value(raw: str | None, op: str | None) -> str | int | float | list[str | int] | None:
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    typed: list[str | int] = [int(p) if p.lstrip("-").isdigit() else p for p in parts]
    if op in ("in", "not_in", "intersects"):
        return typed
    return typed[0] if typed else None


def _rule(draft: DraftRule, question_keys: set[str]) -> RuleConfig | None:
    if draft.kind == "signal":
        if draft.question_key not in question_keys or draft.min_strength is None:
            return None
        condition: dict = {"question_key": draft.question_key, "min_strength": draft.min_strength}
    else:
        condition = {"field": draft.field, "op": draft.op, "value": _value(draft.value, draft.op)}
    try:
        return RuleConfig(
            id=uuid4(),
            name=draft.name.strip() or "Suggested rule",
            kind=draft.kind,
            condition=condition,
            action=draft.action,
            cap_value=draft.cap_value if draft.action == "cap" else None,
        )
    except ValidationError:
        return None


def merge_suggestions(
    service: ServiceBundle, output: SuggestionOutput | None, model: str | None
) -> QuestionSuggestions:
    if output is None:
        return QuestionSuggestions(questions=[], rules=[], model=model, from_llm=False)
    taken = {q.key for q in service.questions}
    questions = [s for d in output.questions if (s := _question(d, taken)) is not None][:MAX_QUESTIONS]
    keys = {s.question.key for s in questions} | {q.key for q in service.questions}
    rules = [r for d in output.rules if (r := _rule(d, keys)) is not None][:MAX_RULES]
    return QuestionSuggestions(questions=questions, rules=rules, model=model)


async def suggest_questions(
    llm: LLMClient, service: ServiceBundle, *, run_id: UUID | None = None
) -> QuestionSuggestions:
    """Draft questions and rules for `service` (its existing questions are not repeated). Nothing is saved.

    Raises QuotaExhausted / LLMUnavailable like any LLM call.
    """
    result = await llm.generate(
        LLMRequest(
            purpose="suggest_questions",
            prompt_version=PROMPT_VERSION,
            system=render_system(),
            user=render_user(service),
            output_model=SuggestionOutput,
            pool="cheap",
            thinking="minimal",
            run_id=run_id,
        )
    )
    return merge_suggestions(service, None if result.blocked else result.output, result.model)
