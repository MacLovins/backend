"""Decision-level metrics (SPEC §1.7.8).

A question answered "yes" after verification is a positive prediction.

TP = expected yes & predicted yes · FP = expected no & predicted yes
FN = expected yes & predicted no/unclear · TN = expected no & predicted no/unclear
Decisions whose service failed or was paused are "not evaluated" and excluded from the confusion counts.
"""

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel

Predicted = Literal["yes", "no", "unclear", "not_evaluated"]


class Decision(BaseModel):
    company_domain: str
    service: str
    question_key: str
    category: str
    polarity: str
    expected: Literal["yes", "no"]
    predicted: Predicted
    evidence_hint: str | None = None
    quotes: list[str] = []  # verified quotes behind a "yes"
    rejected: dict[str, int] = {}  # verifier rejections for this question, by reason
    auto_no: bool = False  # answered "no" without the LLM (no candidate snippets)

    @property
    def outcome(self) -> str:
        if self.predicted == "not_evaluated":
            return "not_evaluated"
        yes = self.predicted == "yes"
        return {("yes", True): "TP", ("no", True): "FP", ("yes", False): "FN", ("no", False): "TN"}[
            (self.expected, yes)
        ]


class Confusion(BaseModel):
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def add(self, outcome: str) -> None:
        if outcome in ("TP", "FP", "FN", "TN"):
            setattr(self, outcome.lower(), getattr(self, outcome.lower()) + 1)

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p and r else None

    def summary(self) -> dict:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "precision": _r(self.precision),
            "recall": _r(self.recall),
            "f1": _r(self.f1),
        }


class Metrics(BaseModel):
    overall: dict
    by_category: dict[str, dict]
    by_service: dict[str, dict]
    evaluated: int
    not_evaluated: int
    abstention_rate: float | None  # predicted "unclear" among evaluated decisions
    hallucination_rate: float | None  # quote_not_found / all evidence returned by the model
    evidence_total: int
    rejected_by_reason: dict[str, int]
    llm_calls: int
    llm_calls_per_company: float | None


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


def compute_metrics(
    decisions: list[Decision],
    verified_total: int,
    rejected_by_reason: dict[str, int],
    llm_calls: int,
    companies: int,
) -> Metrics:
    overall = Confusion()
    by_category: dict[str, Confusion] = defaultdict(Confusion)
    by_service: dict[str, Confusion] = defaultdict(Confusion)
    for d in decisions:
        overall.add(d.outcome)
        by_category[d.category].add(d.outcome)
        by_service[d.service].add(d.outcome)
    evaluated = [d for d in decisions if d.predicted != "not_evaluated"]
    # evidence the model returned = verified + rejected for a reason tied to a quote
    quote_evidence = (
        sum(v for k, v in rejected_by_reason.items() if k != "no_evidence_for_yes") + verified_total
    )
    return Metrics(
        overall=overall.summary(),
        by_category={k: v.summary() for k, v in sorted(by_category.items())},
        by_service={k: v.summary() for k, v in sorted(by_service.items())},
        evaluated=len(evaluated),
        not_evaluated=len(decisions) - len(evaluated),
        abstention_rate=_r(sum(d.predicted == "unclear" for d in evaluated) / len(evaluated))
        if evaluated
        else None,
        hallucination_rate=_r(rejected_by_reason.get("quote_not_found", 0) / quote_evidence)
        if quote_evidence
        else None,
        evidence_total=quote_evidence,
        rejected_by_reason=dict(sorted(rejected_by_reason.items())),
        llm_calls=llm_calls,
        llm_calls_per_company=_r(llm_calls / companies) if companies else None,
    )
