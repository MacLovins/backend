"""Fit of a company to the ICP (SPEC §1.7.5).

Must-have (ICPConfig fields): a known failing value → Fit = 0; an unknown value passes and becomes a data gap.
Nice-to-have (Criterion): Fit = floor + (100 − floor) × Σ weights of matched / Σ weights; unknown counts as
half + data gap; no nice-to-have → 100. The floor (ICPConfig.nice_to_have_floor, default 40) keeps nice-to-have
a ranking factor: a company that passes every must-have but matches no nice-to-have is a weaker fit, not Fit 0.
"""

from collections.abc import Callable

from leadradar_ai.contracts import CompanyProfile, Criterion, FitResult, ICPConfig

_UNKNOWN = None  # tri-state: True = match, False = no match, None = unknown


def _upper(values: list) -> set[str]:
    return {str(v).upper() for v in values}


def _country_in(company: CompanyProfile, values: list) -> bool | None:
    if not company.country_code:
        return _UNKNOWN
    return company.country_code.upper() in _upper(values)


def _industry_in(company: CompanyProfile, values: list) -> bool | None:
    if not company.industry_ids:
        return _UNKNOWN
    return bool(set(company.industry_ids) & {str(v) for v in values})


def _employees_between(company: CompanyProfile, values: list) -> bool | None:
    if company.employees is None:
        return _UNKNOWN
    lo = int(values[0]) if values else 0
    hi = int(values[1]) if len(values) > 1 else None
    return company.employees >= lo and (hi is None or company.employees <= hi)


def _revenue_at_least(company: CompanyProfile, values: list) -> bool | None:
    if company.revenue_eur is None:
        return _UNKNOWN
    return company.revenue_eur >= int(values[0])


def _tag_in(company: CompanyProfile, values: list) -> bool | None:
    return bool(set(company.tags) & {str(v) for v in values})  # tags are user labels: never unknown


_NICE: dict[str, tuple[Callable[[CompanyProfile, list], bool | None], str | None]] = {
    "country_in": (_country_in, "country_code"),
    "industry_in": (_industry_in, "industry_ids"),
    "employees_between": (_employees_between, "employees"),
    "revenue_at_least": (_revenue_at_least, "revenue_eur"),
    "tag_in": (_tag_in, None),
}


def _must_have_checks(company: CompanyProfile, icp: ICPConfig) -> list[tuple[str, str, bool | None, str]]:
    """(criterion, company field, result, label) for every must-have that is set."""
    checks = []
    if icp.countries:
        checks.append(
            (
                "countries",
                "country_code",
                _country_in(company, icp.countries),
                f"Country in {', '.join(icp.countries)}",
            )
        )
    if icp.industries_any:
        checks.append(
            (
                "industries_any",
                "industry_ids",
                _industry_in(company, icp.industries_any),
                f"Industry in {', '.join(icp.industries_any)}",
            )
        )
    if icp.employees_min is not None:
        ok = None if company.employees is None else company.employees >= icp.employees_min
        checks.append(("employees_min", "employees", ok, f"At least {icp.employees_min:,} employees"))
    if icp.employees_max is not None:
        ok = None if company.employees is None else company.employees <= icp.employees_max
        checks.append(("employees_max", "employees", ok, f"At most {icp.employees_max:,} employees"))
    if icp.revenue_min_eur is not None:
        ok = None if company.revenue_eur is None else company.revenue_eur >= icp.revenue_min_eur
        checks.append(("revenue_min_eur", "revenue_eur", ok, f"Revenue at least €{icp.revenue_min_eur:,}"))
    return checks


def _status(result: bool | None, required: bool) -> str:
    if result is None:
        return "unknown"
    if required:
        return "pass" if result else "fail"
    return "match" if result else "no_match"


def fit_score(company: CompanyProfile, icp: ICPConfig) -> FitResult:
    details: list[dict] = []
    gaps: list[str] = []

    def gap(field: str | None) -> None:
        if field and field not in gaps:
            gaps.append(field)

    passed = True
    for criterion, field, result, label in _must_have_checks(company, icp):
        details.append(
            {"criterion": criterion, "required": True, "status": _status(result, True), "label": label}
        )
        if result is None:
            gap(field)
        elif not result:
            passed = False

    total = matched = 0.0
    for c in icp.nice_to_have:
        check, field = _NICE[c.kind]
        result = check(company, c.values)
        details.append(
            {
                "criterion": c.kind,
                "required": False,
                "status": _status(result, False),
                "label": _nice_label(c),
                "weight": c.weight,
            }
        )
        total += c.weight
        if result is None:
            matched += 0.5 * c.weight
            gap(field)
        elif result:
            matched += c.weight

    if not passed:
        fit = 0.0
    elif total == 0:
        fit = 100.0
    else:
        floor = icp.nice_to_have_floor
        fit = floor + (100.0 - floor) * matched / total
    return FitResult(fit=fit, must_have_passed=passed, details=details, data_gaps=gaps)


def _nice_label(c: Criterion) -> str:
    values = ", ".join(str(v) for v in c.values)
    return {
        "country_in": f"Country in {values}",
        "industry_in": f"Industry in {values}",
        "employees_between": f"Employees between {values.replace(', ', ' and ')}",
        "revenue_at_least": f"Revenue at least €{values}",
        "tag_in": f"Tagged {values}",
    }[c.kind]
