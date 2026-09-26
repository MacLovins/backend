"""score_company — pure, deterministic, no LLM (SPEC §1.7.5, ARCHITECTURE §3.4).

s_q      = 1 − Π_{top-k stories}(1 − v_e)                    noisy-OR; reprints of one story count once (V5)
points_q = weights[q.weight] × s_q
Intent   = 100 × (1 − exp(−Σ_{q+} points_q / tau_intent))
Risk     = 100 × (1 − exp(−Σ_{q−} points_q / tau_risk))
Priority = 100 × (Fit/100)^fit_exp × (Intent/100)^intent_exp × (1 − risk_penalty × Risk/100)
Rules: exclude → disqualified, priority 0 · cap → min(priority, cap) · flag → warning only.
Hot also needs ≥ hot_min_questions positive questions with s_q ≥ hot_min_strength: one story is not Hot.
"""

import math
from collections import defaultdict
from datetime import datetime

from leadradar_ai.contracts import (
    CompanyProfile,
    Contribution,
    LeadScore,
    ScoringProfile,
    ServiceBundle,
    StoredSignal,
    Tier,
)
from leadradar_ai.scoring.decay import evidence_value
from leadradar_ai.scoring.dedupe import collapse_syndicated
from leadradar_ai.scoring.derived import derived_signals
from leadradar_ai.scoring.explain import build_why_now
from leadradar_ai.scoring.fit import fit_score
from leadradar_ai.scoring.rules import evaluate_rules


def noisy_or(values: list[float]) -> float:
    product = 1.0
    for v in values:
        product *= 1.0 - min(max(v, 0.0), 1.0)
    return 1.0 - product


def saturate(points: float, tau: float) -> float:
    """0..100: the first strong signals matter most, then the curve flattens."""
    return 100.0 * (1.0 - math.exp(-points / tau))


def combine_priority(fit: float, intent: float, risk: float, profile: ScoringProfile) -> float:
    return (
        100.0
        * (fit / 100.0) ** profile.fit_exponent
        * (intent / 100.0) ** profile.intent_exponent
        * (1.0 - profile.risk_penalty * risk / 100.0)
    )


def assign_tier(
    priority: float, disqualified: bool, profile: ScoringProfile, strong_questions: int | None = None
) -> Tier:
    """`strong_questions` = positive questions with s_q ≥ hot_min_strength (None: the check is skipped)."""
    if disqualified:
        return "disqualified"
    if priority >= profile.tiers["hot"] and (
        strong_questions is None or strong_questions >= profile.hot_min_questions
    ):
        return "hot"
    if priority >= profile.tiers["warm"]:
        return "warm"
    return "cold"


def lead_sort_key(score: LeadScore, company_name: str) -> tuple:
    """Ranking order: priority ↓, intent ↓, fit ↓, name ↑."""
    return (-score.priority, -score.intent, -score.fit, company_name.casefold())


def merge_derived(signals: list[StoredSignal], derived: list[StoredSignal]) -> list[StoredSignal]:
    """Stored non-derived signals + the currently derivable ones, each in its stored version if there is one."""
    stored = {s.id: s for s in signals}
    return [s for s in signals if "derived" not in s.flags] + [stored.get(d.id, d) for d in derived]


def _r1(x: float) -> float:
    return round(x, 1)


def score_company(
    company: CompanyProfile,
    bundle: ServiceBundle,
    signals: list[StoredSignal],
    now: datetime,
    *,
    include_derived: bool = True,
) -> LeadScore:
    """`signals` are the stored signals (non-active ones are ignored). With include_derived, NIS2/DORA signals
    are recomputed from firmographics: a stored copy (same deterministic id) wins, so a user's rejection sticks,
    and stored derived signals that no longer apply are dropped."""
    profile = bundle.scoring
    if include_derived:
        signals = merge_derived(signals, derived_signals(company, bundle, now))
    by_question: dict = defaultdict(list)
    for s in signals:
        if s.status == "active" and s.confidence >= profile.min_confidence:
            by_question[s.question_id].append(s)

    breakdown: list[Contribution] = []
    strengths: dict[str, float] = {}
    strongest: dict[str, StoredSignal] = {}
    positive_points = negative_points = 0.0
    strong_questions = 0
    for q in bundle.questions:
        stories = collapse_syndicated(by_question.get(q.id, []), lambda s: evidence_value(s, profile, now))
        valued = [(evidence_value(s, profile, now), s) for s, _ in stories][
            : profile.max_evidence_per_question
        ]
        strength = noisy_or([v for v, _ in valued])
        points = profile.weights[q.weight] * strength
        strengths[q.key] = strength
        if valued and valued[0][0] > 0:
            strongest[q.key] = valued[0][1]
        if q.polarity == "positive":
            positive_points += points
            strong_questions += strength >= profile.hot_min_strength
        else:
            negative_points += points
        breakdown.append(
            Contribution(
                question_id=q.id,
                key=q.key,
                label=q.text,
                polarity=q.polarity,
                weight=profile.weights[q.weight],
                strength=round(strength, 2),
                points=round(points, 2),
                signal_ids=[s.id for _, s in valued],
            )
        )
    breakdown.sort(key=lambda c: c.points, reverse=True)

    fit = fit_score(company, bundle.icp)
    intent = saturate(positive_points, profile.tau_intent)
    risk = saturate(negative_points, profile.tau_risk)
    priority = combine_priority(fit.fit, intent, risk, profile)

    rule_hits = evaluate_rules(company, bundle.rules, strengths)
    disqualified = any(h["action"] == "exclude" for h in rule_hits)
    caps = [h["cap_value"] for h in rule_hits if h["action"] == "cap"]
    if disqualified:
        priority = 0.0
    elif caps:
        priority = min(priority, *caps)
    priority = _r1(priority)

    return LeadScore(
        company_id=company.id,
        service_id=bundle.service_id,
        scoring_profile_id=profile.id,
        fit=_r1(fit.fit),
        intent=_r1(intent),
        risk=_r1(risk),
        priority=priority,
        tier=assign_tier(priority, disqualified, profile, strong_questions),
        disqualified=disqualified,
        rule_hits=rule_hits,
        fit_details=fit.details,
        breakdown=breakdown,
        why_now=build_why_now(breakdown, strongest, risk, fit),
        data_gaps=fit.data_gaps,
        computed_at=now,
    )
