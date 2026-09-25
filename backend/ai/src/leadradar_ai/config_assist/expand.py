"""expand_question@v1 (SPEC §1.7.7, AI-11): multilingual keywords, job titles and stop terms for a question.

Cheap model pool, minimal thinking, cached by the gateway. Limits and cleanup are applied in code, not in the
schema, so an extra term never costs a repair call. Seed terms (from the preset or the admin) always come first.
Used by the prefilter (BM25 query) and by the collect request (news topics, job search).
"""

import json
import re
from functools import cache
from uuid import UUID

from pydantic import BaseModel, Field

from leadradar_ai.contracts import ICPConfig, QuestionConfig, ServiceBundle
from leadradar_ai.llm.types import LLMClient, LLMRequest
from leadradar_ai.prompts.loader import Prompt, load_prompt

PROMPT_NAME, PROMPT_VERSION_TAG = "expand_question", "v1"
PROMPT_VERSION = f"{PROMPT_NAME}@{PROMPT_VERSION_TAG}"

MAX_TERMS_PER_LANGUAGE = 12
MAX_JOB_TITLES = 12
MAX_NEGATIVE_TERMS = 8
MAX_TERM_CHARS = 60
DEFAULT_MAX_LANGUAGES = 6

# Business languages of ICP countries (ISO 3166 → ISO 639-1), most used first
COUNTRY_LANGUAGES: dict[str, list[str]] = {
    "AT": ["de"],
    "BE": ["nl", "fr"],
    "BG": ["bg"],
    "HR": ["hr"],
    "CY": ["el"],
    "CZ": ["cs"],
    "DK": ["da"],
    "EE": ["et"],
    "FI": ["fi", "sv"],
    "FR": ["fr"],
    "DE": ["de"],
    "GR": ["el"],
    "HU": ["hu"],
    "IE": ["en"],
    "IT": ["it"],
    "LV": ["lv"],
    "LT": ["lt"],
    "LU": ["fr", "de"],
    "MT": ["en"],
    "NL": ["nl"],
    "PL": ["pl"],
    "PT": ["pt"],
    "RO": ["ro"],
    "SK": ["sk"],
    "SI": ["sl"],
    "ES": ["es"],
    "SE": ["sv"],
    "GB": ["en"],
    "CH": ["de", "fr", "it"],
    "NO": ["no"],
    "US": ["en"],
    "MD": ["ro"],
}
# When the ICP spans many countries, keep the languages that cover the most business text
LANGUAGE_PRIORITY = ["en", "de", "fr", "nl", "es", "it", "pl", "sv", "ro", "pt", "da", "no", "fi", "cs"]


def languages_for_icp(icp: ICPConfig, max_languages: int = DEFAULT_MAX_LANGUAGES) -> list[str]:
    """English always, then the languages of the ICP countries by priority (empty ICP = English only)."""
    langs = {lang for c in icp.countries for lang in COUNTRY_LANGUAGES.get(c.upper(), [])}
    rank = {lang: i for i, lang in enumerate(LANGUAGE_PRIORITY)}
    ordered = ["en", *sorted(langs - {"en"}, key=lambda lang: (rank.get(lang, len(rank)), lang))]
    return ordered[:max_languages]


class LanguageTerms(BaseModel):
    language: str = Field(description="ISO 639-1 code from <languages>")
    terms: list[str]


class ExpansionOutput(BaseModel):
    keywords: list[LanguageTerms]
    job_titles: list[str] = Field(default_factory=list)
    negative_terms: list[str] = Field(default_factory=list)


class QuestionExpansion(BaseModel):
    """What core stores on signal_question (keywords, job_titles, negative_terms; keywords_status=ready)."""

    keywords: dict[str, list[str]]
    job_titles: list[str]
    negative_terms: list[str]
    model: str | None
    prompt_version: str = PROMPT_VERSION
    from_llm: bool = True  # False: the model declined, only the seed is returned


def expansion_prompt() -> Prompt:
    return load_prompt(PROMPT_NAME, PROMPT_VERSION_TAG)


def _question_context(q: QuestionConfig) -> dict:
    return {
        "category": q.category,
        "polarity": q.polarity,
        "source_types": sorted(q.source_types),
        "text": q.text,
    }


@cache
def render_system() -> str:
    prompt = expansion_prompt()
    examples = [
        {
            "input": prompt.render_user(**ex["input"]),
            "output": json.dumps(ex["output"], ensure_ascii=False, indent=1),
        }
        for ex in prompt.examples
    ]
    return prompt.render_system(examples=examples)


def render_user(
    service: ServiceBundle, question: QuestionConfig, languages: list[str], seed: dict[str, list[str]]
) -> str:
    return expansion_prompt().render_user(
        service={"name": service.name, "description": service.description},
        question=_question_context(question),
        languages=languages,
        seed={lang: terms for lang, terms in seed.items() if terms},
    )


_SPACES = re.compile(r"\s+")


def _clean(terms: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out = []
    for term in terms:
        t = _SPACES.sub(" ", term).strip(" \t\"'.,;:")
        key = t.casefold()
        if not t or len(t) > MAX_TERM_CHARS or key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) == limit:
            break
    return out


def _wants_job_titles(q: QuestionConfig) -> bool:
    return q.category == "hiring" or "jobs" in q.source_types


def merge_expansion(
    question: QuestionConfig,
    languages: list[str],
    seed: dict[str, list[str]],
    output: ExpansionOutput | None,
    model: str | None,
) -> QuestionExpansion:
    generated: dict[str, list[str]] = {}
    for item in output.keywords if output else []:
        lang = item.language.strip().lower()
        if lang in languages:
            generated.setdefault(lang, []).extend(item.terms)
    keywords = {}
    for lang in languages:
        terms = _clean([*seed.get(lang, []), *generated.get(lang, [])], MAX_TERMS_PER_LANGUAGE)
        if terms:
            keywords[lang] = terms
    job_titles = (
        _clean([*question.job_titles, *(output.job_titles if output else [])], MAX_JOB_TITLES)
        if _wants_job_titles(question)
        else []
    )
    negative_terms = _clean(
        [*question.negative_terms, *(output.negative_terms if output else [])], MAX_NEGATIVE_TERMS
    )
    return QuestionExpansion(
        keywords=keywords,
        job_titles=job_titles,
        negative_terms=negative_terms,
        model=model,
        from_llm=output is not None,
    )


async def expand_question(
    llm: LLMClient,
    service: ServiceBundle,
    question: QuestionConfig,
    *,
    languages: list[str] | None = None,
    seed: dict[str, list[str]] | None = None,
    run_id: UUID | None = None,
) -> QuestionExpansion:
    """Keywords for `question`. `seed` defaults to the question's current keywords (the preset seed on create).

    Raises QuotaExhausted / LLMUnavailable like any LLM call: core leaves keywords_status=pending and retries.
    """
    languages = languages or languages_for_icp(service.icp)
    seed = question.keywords if seed is None else seed
    result = await llm.generate(
        LLMRequest(
            purpose="expand_question",
            prompt_version=PROMPT_VERSION,
            system=render_system(),
            user=render_user(service, question, languages, seed),
            output_model=ExpansionOutput,
            pool="cheap",
            thinking="minimal",
            run_id=run_id,
        )
    )
    return merge_expansion(question, languages, seed, None if result.blocked else result.output, result.model)


def apply_expansion(question: QuestionConfig, expansion: QuestionExpansion) -> QuestionConfig:
    """Same question with the expanded search terms (the version does not change: terms do not change meaning)."""
    return question.model_copy(
        update={
            "keywords": expansion.keywords,
            "job_titles": expansion.job_titles,
            "negative_terms": expansion.negative_terms,
        }
    )
