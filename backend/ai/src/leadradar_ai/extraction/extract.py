"""Signal extraction for one service (SPEC AI-06, AI-07).

One LLM call per service: all questions that have candidates + the prefiltered snippets. Questions without
candidates get "no" without the LLM. If the prompt exceeds the input budget, the questions are split into
batches (each with only its own candidates). A blocked call (SAFETY / RECITATION) makes its answers "unclear".
"""

import json
from dataclasses import dataclass, field
from functools import cache
from uuid import UUID

from leadradar_ai.contracts import CompanyProfile, QuestionConfig, ServiceBundle, Snippet
from leadradar_ai.extraction.schema import Answer, ExtractionOutput
from leadradar_ai.llm.cache_key import estimate_tokens
from leadradar_ai.llm.types import LLMClient, LLMRequest
from leadradar_ai.prompts.loader import Prompt, load_prompt
from leadradar_ai.retrieval.prefilter import PrefilterResult

PROMPT_NAME, PROMPT_VERSION_TAG = "extract_signals", "v1"
PROMPT_VERSION = f"{PROMPT_NAME}@{PROMPT_VERSION_TAG}"

NO_CANDIDATES_RATIONALE = "No relevant snippets were found for this question."


def extraction_prompt() -> Prompt:
    return load_prompt(PROMPT_NAME, PROMPT_VERSION_TAG)


# --- rendering ----------------------------------------------------------------------------------


def company_context(company: CompanyProfile) -> dict:
    return {
        "name": company.name,
        "domain": company.domain,
        "aliases": company.aliases,
        "country": company.country_code,
        "industries": company.industry_ids,
        "employees": company.employees,
    }


def snippet_context(s: Snippet) -> dict:
    published = s.published_at.date().isoformat() if s.published_at else None
    return {
        "id": s.id,
        "source_type": s.source_type,
        "source": s.source_name,
        "published": published,
        "title": s.title,
        "text": s.text,
    }


def question_context(pid: str, q: QuestionConfig) -> dict:
    return {"id": pid, "polarity": q.polarity, "category": q.category, "text": q.text}


@cache
def render_system() -> str:
    prompt = extraction_prompt()
    examples = [
        {
            "input": prompt.render_user(**ex["input"]),
            "output": json.dumps(ex["output"], ensure_ascii=False, indent=1),
        }
        for ex in prompt.examples
    ]
    return prompt.render_system(examples=examples)


def render_user(
    company: CompanyProfile,
    bundle: ServiceBundle,
    snippets: list[Snippet],
    questions: list[tuple[str, QuestionConfig]],
) -> str:
    return extraction_prompt().render_user(
        company=company_context(company),
        service={"name": bundle.name, "description": bundle.description},
        snippets=[snippet_context(s) for s in snippets],
        questions=[question_context(pid, q) for pid, q in questions],
    )


# --- extraction ---------------------------------------------------------------------------------


@dataclass
class ServiceExtraction:
    answers: dict[str, Answer] = field(
        default_factory=dict
    )  # str(question.id) → answer, question_id = UUID str
    auto_no: list[str] = field(default_factory=list)  # answered "no" without the LLM
    llm_calls: int = 0
    cache_hits: int = 0
    model: str | None = None
    blocked: bool = False


def _auto_answer(qid: str, answer: str, rationale: str) -> Answer:
    return Answer(
        question_id=qid,
        answer=answer,
        confidence=1.0 if answer == "no" else 0.0,
        evidence=[],
        rationale=rationale,
    )


def _batches(
    company: CompanyProfile,
    bundle: ServiceBundle,
    pre: PrefilterResult,
    questions: list[QuestionConfig],
    max_input_tokens: int,
) -> list[tuple[list[QuestionConfig], list[Snippet]]]:
    by_id = {s.id: s for s in pre.snippets}
    system_tokens = estimate_tokens(render_system())

    def snippets_for(qs: list[QuestionConfig]) -> list[Snippet]:
        wanted = {sid for q in qs for sid in pre.candidates[str(q.id)]}
        return [s for s in pre.snippets if s.id in wanted and s.id in by_id]

    def fits(qs: list[QuestionConfig], snippets: list[Snippet]) -> bool:
        user = render_user(company, bundle, snippets, [(f"Q{i}", q) for i, q in enumerate(qs, 1)])
        return system_tokens + estimate_tokens(user) <= max_input_tokens

    def split(qs: list[QuestionConfig]) -> list[tuple[list[QuestionConfig], list[Snippet]]]:
        snippets = snippets_for(qs)
        if len(qs) == 1 or fits(qs, snippets):
            return [(qs, snippets)]
        half = len(qs) // 2
        return split(qs[:half]) + split(qs[half:])

    return split(questions)


async def extract_service(
    llm: LLMClient,
    company: CompanyProfile,
    bundle: ServiceBundle,
    pre: PrefilterResult,
    *,
    max_input_tokens: int = 30_000,
    run_id: UUID | None = None,
) -> ServiceExtraction:
    out = ServiceExtraction()
    asked = [q for q in bundle.questions if pre.candidates.get(str(q.id))]
    for q in bundle.questions:
        if q not in asked:
            out.answers[str(q.id)] = _auto_answer(str(q.id), "no", NO_CANDIDATES_RATIONALE)
            out.auto_no.append(str(q.id))
    if not asked:
        return out

    system = render_system()
    for questions, snippets in _batches(company, bundle, pre, asked, max_input_tokens):
        pids = {f"Q{i}": q for i, q in enumerate(questions, 1)}
        result = await llm.generate(
            LLMRequest(
                purpose="extract_signals",
                prompt_version=PROMPT_VERSION,
                system=system,
                user=render_user(company, bundle, snippets, list(pids.items())),
                output_model=ExtractionOutput,
                pool="main",
                run_id=run_id,
            )
        )
        out.llm_calls += 0 if result.cache_hit else 1
        out.cache_hits += 1 if result.cache_hit else 0
        out.model = result.model

        if result.blocked or result.output is None:
            out.blocked = True
            for q in questions:
                out.answers[str(q.id)] = _auto_answer(str(q.id), "unclear", "The model declined to answer.")
            continue

        returned: dict[str, Answer] = {}
        for a in result.output.answers:
            returned.setdefault(a.question_id.strip(), a)
        for pid, q in pids.items():
            a = returned.get(pid)
            qid = str(q.id)
            out.answers[qid] = (
                a.model_copy(update={"question_id": qid})
                if a is not None
                else _auto_answer(qid, "unclear", "The model returned no answer for this question.")
            )
    return out
