"""Disqualification rules (SPEC §1.7.5): exclude → disqualified, cap → priority ≤ cap_value, flag → warning.

A rule whose input is unknown (e.g. employees is None) does not fire: missing data is a data gap,
not a verdict.
"""

from leadradar_ai.contracts import (
    CompanyProfile,
    FirmographicCondition,
    ListCondition,
    RuleConfig,
    SignalCondition,
)


def _norm_domain(domain: str) -> str:
    d = domain.strip().lower()
    return d.removeprefix("www.")


def _firmographic(company: CompanyProfile, cond: FirmographicCondition) -> bool:
    actual = getattr(company, cond.field)
    if actual is None:
        return False
    if isinstance(actual, str):
        actual = actual.casefold()
    expected = cond.value
    if isinstance(expected, str):
        expected = expected.casefold()
    elif isinstance(expected, list):
        expected = [v.casefold() if isinstance(v, str) else v for v in expected]

    if isinstance(actual, list):  # industry_ids, tags
        values = {v.casefold() for v in actual}
        if not values:
            return False
        wanted = set(expected) if isinstance(expected, list) else {expected}
        if cond.op in ("in", "intersects", "eq"):
            return bool(values & wanted)
        if cond.op == "not_in":
            return not values & wanted
        return False

    match cond.op:
        case "lt":
            return actual < expected
        case "gt":
            return actual > expected
        case "eq":
            return actual == expected
        case "in" | "intersects":
            return actual in expected
        case "not_in":
            return actual not in expected
    return False


def _list(company: CompanyProfile, cond: ListCondition) -> bool:
    listed = {_norm_domain(d) for d in cond.domains}
    own = {_norm_domain(company.domain), *(_norm_domain(d) for d in company.own_domains)}
    return bool(listed & own)


def _signal(cond: SignalCondition, strengths: dict[str, float]) -> bool:
    return strengths.get(cond.question_key, 0.0) >= cond.min_strength


def evaluate_rules(
    company: CompanyProfile, rules: list[RuleConfig], strengths: dict[str, float] | None = None
) -> list[dict]:
    """Rules that fire. `strengths` maps question key → s_q (needed by kind="signal")."""
    strengths = strengths or {}
    hits = []
    for rule in rules:
        cond = rule.parsed_condition()
        if isinstance(cond, FirmographicCondition):
            fired = _firmographic(company, cond)
        elif isinstance(cond, ListCondition):
            fired = _list(company, cond)
        else:
            fired = _signal(cond, strengths)
        if fired:
            hits.append(
                {
                    "rule_id": str(rule.id),
                    "name": rule.name,
                    "kind": rule.kind,
                    "action": rule.action,
                    "cap_value": rule.cap_value,
                }
            )
    return hits
