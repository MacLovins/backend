"""Verification (SPEC §1.7.4) — code decides, not the LLM.

V1 quote   — verbatim (or fuzzy ≥ 90, confidence × 0.9) in the cited snippet (or its title) → document offsets
V2 subject — subject == target_company, and on third-party sources the company is named (case-sensitive) in the
             title or within 250 characters of the quote: the LLM alone passes homonyms like Orange
V3 recency — event_date from the LLM is capped by the document date (published_at, else fetched_at): an article
             cannot report a later event, so an invented fresh date cannot dodge freshness and decay
V4 answer  — "yes" without a valid evidence becomes "unclear"; evidence of "no"/"unclear" is ignored
Threshold  — confidence ≥ profile.min_confidence
V5 story   — reprints of one story (same document or near-identical quotes) keep the best copy, flagged
             corroborated; the scoring engine collapses them again across runs
Cap        — ≤ profile.max_evidence_per_question evidence per question, by contribution to the score
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from leadradar_ai.contracts import (
    CompanyProfile,
    QuestionConfig,
    RejectedEvidence,
    RejectReason,
    ServiceBundle,
    SignalFlag,
    Snippet,
    VerifiedSignal,
)
from leadradar_ai.extraction.extract import ServiceExtraction
from leadradar_ai.extraction.schema import Answer, Evidence
from leadradar_ai.retrieval.entity import mentioned_near_quote
from leadradar_ai.scoring.decay import evidence_value, reliability_of
from leadradar_ai.scoring.dedupe import collapse_syndicated
from leadradar_ai.verification.quotes import find_quote

FUZZY_CONFIDENCE_FACTOR = 0.9

FinalAnswer = Literal["yes", "no", "unclear"]


@dataclass
class VerificationResult:
    signals: list[VerifiedSignal] = field(default_factory=list)
    rejected: list[RejectedEvidence] = field(default_factory=list)
    final_answers: dict[str, FinalAnswer] = field(default_factory=dict)  # str(question.id) → after V4


def _event_date(ev: Evidence, snippet: Snippet) -> date | None:
    published = snippet.published_at.date() if snippet.published_at else None
    if ev.event_date is None:
        return published
    return min(ev.event_date, published or snippet.fetched_at.date())


def _reject(q: QuestionConfig, snippet_id: str, quote: str, reason: RejectReason) -> RejectedEvidence:
    return RejectedEvidence(question_id=q.id, snippet_id=snippet_id, quote=quote, reason=reason)


def _verify_evidence(
    q: QuestionConfig,
    answer: Answer,
    ev: Evidence,
    snippets: dict[str, Snippet],
    bundle: ServiceBundle,
    company: CompanyProfile | None,
    now: datetime,
    model: str | None,
    prompt_version: str,
) -> VerifiedSignal | RejectedEvidence:
    snippet = snippets.get(ev.snippet_id.strip())
    if snippet is None:
        return _reject(q, ev.snippet_id, ev.quote, "quote_not_found")

    flags: set[SignalFlag] = set()
    match = find_quote(ev.quote, snippet.text)
    if match is not None:
        quote = snippet.text[match.start : match.end]
        start, end = snippet.char_start + match.start, snippet.char_start + match.end
        local = (match.start, match.end)
    elif snippet.title and (title_match := find_quote(ev.quote, snippet.title)) is not None:
        match = title_match
        quote, start, end = snippet.title[match.start : match.end], None, None
        local = (0, 0)
        flags.add("headline_only")
    else:
        return _reject(q, snippet.id, ev.quote, "quote_not_found")
    if match.fuzzy:
        flags.add("fuzzy_quote")

    if ev.subject != "target_company":
        return _reject(q, snippet.id, quote, "wrong_subject")
    if company is not None and not mentioned_near_quote(snippet, *local, company):
        return _reject(q, snippet.id, quote, "wrong_subject")

    event_date = _event_date(ev, snippet)
    reference = event_date or snippet.fetched_at.date()
    if snippet.published_at is None and ev.event_date is None:
        flags.add("undated")
    if reference < (now - timedelta(days=q.recency_days)).date():
        return _reject(q, snippet.id, quote, "stale")

    confidence = answer.confidence * (FUZZY_CONFIDENCE_FACTOR if match.fuzzy else 1.0)
    if confidence < bundle.scoring.min_confidence:
        return _reject(q, snippet.id, quote, "below_confidence")

    if snippet.meta.get("headline_only"):
        flags.add("headline_only")
    signal = VerifiedSignal(
        question_id=q.id,
        question_key=q.key,
        question_version=q.version,
        category=q.category,
        polarity=q.polarity,
        document_id=snippet.document_id,
        chunk_id=snippet.chunk_id,
        url=snippet.url,
        source_type=snippet.source_type,
        source_name=snippet.source_name,
        quote=quote,
        quote_start=start,
        quote_end=end,
        summary=ev.summary.strip(),
        strength=ev.strength,
        confidence=round(confidence, 4),
        reliability=0.0,
        event_date=event_date,
        published_at=snippet.published_at,
        flags=flags,
        model=model,
        prompt_version=prompt_version,
    )
    return signal.model_copy(update={"reliability": reliability_of(signal, bundle.scoring)})


def verify_extraction(
    extraction: ServiceExtraction,
    bundle: ServiceBundle,
    snippets: list[Snippet],
    now: datetime,
    prompt_version: str,
    company: CompanyProfile | None = None,
) -> VerificationResult:
    """`company` enables the in-code V2 check (the graph always passes it)."""
    by_id = {s.id: s for s in snippets}
    profile = bundle.scoring
    result = VerificationResult()
    for q in bundle.questions:
        qid = str(q.id)
        answer = extraction.answers.get(qid)
        if answer is None or answer.answer != "yes":
            result.final_answers[qid] = answer.answer if answer else "unclear"
            continue

        verified: list[VerifiedSignal] = []
        seen: set[tuple] = set()
        for ev in answer.evidence:
            outcome = _verify_evidence(
                q, answer, ev, by_id, bundle, company, now, extraction.model, prompt_version
            )
            if isinstance(outcome, RejectedEvidence):
                result.rejected.append(outcome)
                continue
            key = (outcome.document_id, outcome.quote_start, outcome.quote_end, outcome.quote)
            if key not in seen:
                seen.add(key)
                verified.append(outcome)

        stories = collapse_syndicated(verified, lambda s: evidence_value(s, profile, now))
        for signal, copies in stories[: profile.max_evidence_per_question]:
            if copies > 1:
                signal = signal.model_copy(update={"flags": signal.flags | {"corroborated"}})
            result.signals.append(signal)
        if verified:
            result.final_answers[qid] = "yes"
        else:
            result.final_answers[qid] = "unclear"
            if not answer.evidence:
                result.rejected.append(_reject(q, "", "", "no_evidence_for_yes"))
    return result
