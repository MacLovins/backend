"""Value of one piece of evidence (SPEC §1.7.5):

v_e = strength_values[strength] × confidence × reliability(source) × 0.5 ** (age_days / half_life(source))
"""

from datetime import date, datetime

from leadradar_ai.contracts import ScoringProfile, VerifiedSignal


def evidence_date(signal: VerifiedSignal) -> date | None:
    """Age is counted from event_date, else from published_at; None when the signal is undated."""
    if signal.event_date is not None:
        return signal.event_date
    return signal.published_at.date() if signal.published_at else None


def age_days(signal: VerifiedSignal, now: datetime) -> int | None:
    d = evidence_date(signal)
    return None if d is None else max(0, (now.date() - d).days)


def decay_factor(signal: VerifiedSignal, profile: ScoringProfile, now: datetime) -> float:
    half_life = profile.half_life_days.get(signal.source_type)
    age = age_days(signal, now)
    if half_life is None or age is None:
        return 1.0
    return 0.5 ** (age / half_life)


def reliability_of(signal: VerifiedSignal, profile: ScoringProfile) -> float:
    """From the profile, so an admin change applies on rescore; the value stored at verify time is a
    fallback."""
    if "headline_only" in signal.flags and "headline_only" in profile.reliability:
        return profile.reliability["headline_only"]
    return profile.reliability.get(signal.source_type, signal.reliability)


def evidence_value(signal: VerifiedSignal, profile: ScoringProfile, now: datetime) -> float:
    return (
        profile.strength_values[signal.strength]
        * signal.confidence
        * reliability_of(signal, profile)
        * decay_factor(signal, profile, now)
    )
