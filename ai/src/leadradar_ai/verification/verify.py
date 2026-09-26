"""Verification (SPEC §1.7.4) — code decides, not the LLM.

V1 quote   — verbatim (or fuzzy ≥ 90, confidence × 0.9) in the cited snippet (or its title) → document offsets
V2 subject — subject == target_company, and — checked in code, the LLM's word is not enough — for third-party
             sources a genuine mention of the company (name, alias or domain; homonyms like "Orange County"
             excluded) within ±SUBJECT_WINDOW_CHARS of the quote (or in the title of a headline / document
             head) → else wrong_subject
V3 recency — event date = min(LLM event_date, published_at) — an event cannot happen after it was reported —
             and never in the future (a future event_date falls back to published_at, then today); undated
             evidence uses fetched_at (flag "undated"); within the question's window
V4 answer  — "yes" without a valid evidence becomes "unclear"; evidence of "no"/"unclear" is ignored
Threshold  — confidence ≥ profile.min_confidence
V5         — reprints of one event are clustered (scoring.corroboration); a cluster confirmed by ≥ 2 independent
             sources is flagged "corroborated"
Cap        — ≤ profile.max_evidence_per_question events per question (by cluster value), ≤ MAX_CLUSTER_MEMBERS
             signals each
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
from leadradar_ai.retrieval.entity import CONTEXT_CHARS, find_mentions, is_own_source, mentioned_near
from leadradar_ai.scoring.corroboration import cluster_signals
from leadradar_ai.scoring.decay import reliability_of
from leadradar_ai.verification.quotes import find_quote

FUZZY_CONFIDENCE_FACTOR = 0.9
SUBJECT_WINDOW_CHARS = CONTEXT_CHARS
MAX_CLUSTER_MEMBERS = 3  # reprints kept per event (corroboration evidence); the event counts once

FinalAnswer = Literal["yes", "no", "unclear"]


@dataclass
class VerificationResult:
    signals: list[VerifiedSignal] = field(default_factory=list)
    rejected: list[RejectedEvidence] = field(default_factory=list)
    final_answers: dict[str, FinalAnswer] = field(default_factory=dict)  # str(question.id) → after V4


def _event_date(ev: Evidence, snippet: Snippet, now: datetime) -> date | None:
    """min(LLM event date, document date); never in the future."""
    today = now.date()
    published = snippet.published_at.date() if snippet.published_at else None
    event = ev.event_date if ev.event_date is not None and ev.event_date <= today else None
    candidates = [d for d in (event, published) if d is not None]
    return min(min(candidates), today) if candidates else None


def _subject_confirmed(snippet: Snippet, company: CompanyProfile, start: int | None, end: int | None) -> bool:
    """The company itself is named near the quote. start/end: quote offsets in snippet.text (None when the
    quote was found in the title)."""
    if is_own_source(snippet, company):
        return True
    title_mention = bool(find_mentions(snippet.title or "", company))
    if start is None or end is None:
        return title_mention
    if mentioned_near(snippet.text, start, end, company, SUBJECT_WINDOW_CHARS):
        return True
    # the title names the subject of the document's head (headline + lead paragraph)
    return title_mention and snippet.char_start + start <= SUBJECT_WINDOW_CHARS


def _reject(q: QuestionConfig, snippet_id: str, quote: str, reason: RejectReason) -> RejectedEvidence:
    return RejectedEvidence(question_id=q.id, snippet_id=snippet_id, quote=quote, reason=reason)


def _verify_evidence(
    q: QuestionConfig,
    answer: Answer,
    ev: Evidence,
    snippets: dict[str, Snippet],
    bundle: ServiceBundle,
    now: datetime,
    model: str | None,
    prompt_version: str,
    company: CompanyProfile | None,
) -> VerifiedSignal | RejectedEvidence:
    snippet = snippets.get(ev.snippet_id.strip())
    if snippet is None:
        return _reject(q, ev.snippet_id, ev.quote, "quote_not_found")

    flags: set[SignalFlag] = set()
    in_text: tuple[int | None, int | None] = (None, None)
    match = find_quote(ev.quote, snippet.text)
    if match is not None:
        quote = snippet.text[match.start : match.end]
        start, end = snippet.char_start + match.start, snippet.char_start + match.end
        in_text = (match.start, match.end)
    elif snippet.title and (title_match := find_quote(ev.quote, snippet.title)) is not None:
        match = title_match
        quote, start, end = snippet.title[match.start : match.end], None, None
        flags.add("headline_only")
    else:
        return _reject(q, snippet.id, ev.quote, "quote_not_found")
    if match.fuzzy:
        flags.add("fuzzy_quote")

    if ev.subject != "target_company":
        return _reject(q, snippet.id, quote, "wrong_subject")
    if company is not None and not _subject_confirmed(snippet, company, *in_text):
        return _reject(q, snippet.id, quote, "wrong_subject")

    event_date = _event_date(ev, snippet, now)
    reference = event_date or snippet.fetched_at.date()
    if event_date is None:
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
        fetched_at=snippet.fetched_at,
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
    """`company` enables the code check of the subject (V2); without it only the LLM's subject is used."""
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
                q, answer, ev, by_id, bundle, now, extraction.model, prompt_version, company
            )
            if isinstance(outcome, RejectedEvidence):
                result.rejected.append(outcome)
                continue
            key = (outcome.document_id, outcome.quote_start, outcome.quote_end, outcome.quote)
            if key not in seen:
                seen.add(key)
                verified.append(outcome)

        for cluster in cluster_signals(verified, profile, now)[: profile.max_evidence_per_question]:
            members = cluster.members[:MAX_CLUSTER_MEMBERS]
            if cluster.corroborated:
                members = [s.model_copy(update={"flags": s.flags | {"corroborated"}}) for s in members]
            result.signals.extend(members)
        if verified:
            result.final_answers[qid] = "yes"
        else:
            result.final_answers[qid] = "unclear"
            if not answer.evidence:
                result.rejected.append(_reject(q, "", "", "no_evidence_for_yes"))
    return result
