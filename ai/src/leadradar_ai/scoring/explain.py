"""«Why now» (SPEC §1.7.5): top-3 positive contributions → their strongest signal; the main negative when
Risk ≥ 20; notes on Fit (failed must-have, data gaps)."""

from leadradar_ai.contracts import Contribution, FitResult, Reason, StoredSignal
from leadradar_ai.scoring.decay import evidence_date

RISK_NOTE_THRESHOLD = 20.0
MAX_POSITIVE_REASONS = 3


def _signal_reason(signal: StoredSignal, polarity: str) -> Reason:
    return Reason(
        text=signal.summary,
        polarity=polarity,
        signal_id=signal.id,
        source_name=signal.source_name,
        url=signal.url,
        date=evidence_date(signal),
    )


def build_why_now(
    breakdown: list[Contribution],
    strongest: dict[str, StoredSignal],
    risk: float,
    fit: FitResult,
) -> list[Reason]:
    """`breakdown` is sorted by points desc; `strongest` maps question key → its highest-value signal."""
    reasons = []
    positives = [c for c in breakdown if c.polarity == "positive" and c.points > 0 and c.key in strongest]
    for c in positives[:MAX_POSITIVE_REASONS]:
        reasons.append(_signal_reason(strongest[c.key], "positive"))

    if risk >= RISK_NOTE_THRESHOLD:
        negatives = [c for c in breakdown if c.polarity == "negative" and c.points > 0 and c.key in strongest]
        if negatives:
            reasons.append(_signal_reason(strongest[negatives[0].key], "negative"))

    for d in fit.details:
        if d["required"] and d["status"] == "fail":
            reasons.append(Reason(text=f"Outside ICP: {d['label']}", polarity="fit"))
    if fit.data_gaps:
        reasons.append(Reason(text=f"Unknown: {', '.join(fit.data_gaps)}", polarity="data_gap"))
    return reasons
