"""Derived signals without the LLM (SPEC AI-16, ARCHITECTURE §3.5).

"Likely in NIS2 scope": an EU company in an Annex I/II sector with ≥ 50 employees or > €10M revenue
(the NIS2 size-cap rule: medium and large enterprises). "In DORA scope": an EU financial entity.

They attach to the service's question with category "compliance" (Cybersecurity: cy_compliance); services
without such a question get none. The verify node persists them with the extracted signals
(source_type="derived", flag "derived", no document) through AnalysisStore.save_extraction, so the ids in
the breakdown resolve to stored rows. score_company still recomputes them from the current firmographics on
every scoring (reusing the stored ids), so a changed profile changes them on the next rescore.
Weak/moderate by design: scope alone is context, explicit readiness programs found in documents must
dominate.
"""

from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from leadradar_ai.contracts import (
    CompanyProfile,
    QuestionConfig,
    ServiceBundle,
    StoredSignal,
    Strength,
    VerifiedSignal,
)

EU_COUNTRIES = frozenset(
    [
        "AT",
        "BE",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DK",
        "EE",
        "FI",
        "FR",
        "DE",
        "GR",
        "HU",
        "IE",
        "IT",
        "LV",
        "LT",
        "LU",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SK",
        "SI",
        "ES",
        "SE",
    ]
)
# Parser taxonomy industry ids → NIS2 annex (I: sectors of high criticality, II: other critical sectors)
NIS2_ANNEX: dict[str, str] = {
    "energy_utilities": "I",
    "oil_gas": "I",
    "airlines": "I",
    "rail": "I",
    "logistics": "I",
    "banking": "I",
    "financial_markets": "I",
    "healthcare": "I",
    "pharma": "I",
    "water": "I",
    "digital_infrastructure": "I",
    "telecom": "I",
    "public_sector": "I",
    "it_services": "I",
    "postal_courier": "II",
    "chemicals": "II",
    "food_beverage": "II",
    "manufacturing": "II",
    "automotive": "II",
    "medical_devices": "II",
}
DORA_INDUSTRIES = frozenset({"banking", "insurance", "financial_markets"})
NIS2_MIN_EMPLOYEES = 50
NIS2_MIN_REVENUE_EUR = 10_000_000

NIS2_URL = "https://eur-lex.europa.eu/eli/dir/2022/2555/oj"
DORA_URL = "https://eur-lex.europa.eu/eli/reg/2022/2554/oj"
DERIVED_VERSION = "derived@v1"

NIS2_STRENGTH: Strength = "weak"
DORA_STRENGTH: Strength = "moderate"
CONFIDENCE = 0.8


def _compliance_question(bundle: ServiceBundle) -> QuestionConfig | None:
    return next(
        (q for q in bundle.questions if q.category == "compliance" and q.polarity == "positive"), None
    )


def _size(company: CompanyProfile) -> str:
    parts = []
    if company.employees is not None:
        parts.append(f"{company.employees:,} employees")
    if company.revenue_eur is not None:
        parts.append(f"€{company.revenue_eur / 1e6:,.0f}M revenue")
    return " and ".join(parts)


def _signal(
    company: CompanyProfile,
    bundle: ServiceBundle,
    q: QuestionConfig,
    now: datetime,
    kind: str,
    strength: Strength,
    source_name: str,
    url: str,
    quote: str,
    summary: str,
) -> StoredSignal:
    doc_id = uuid5(NAMESPACE_URL, f"leadradar:derived:{kind}:{company.id}")
    return StoredSignal(
        id=uuid5(NAMESPACE_URL, f"leadradar:derived:{kind}:{company.id}:{bundle.service_id}:{q.id}"),
        detected_at=now,
        question_id=q.id,
        question_key=q.key,
        question_version=q.version,
        category=q.category,
        polarity=q.polarity,
        document_id=doc_id,
        chunk_id=None,
        url=url,
        source_type="derived",
        source_name=source_name,
        quote=quote,
        quote_start=None,
        quote_end=None,
        summary=summary,
        strength=strength,
        confidence=CONFIDENCE,
        reliability=bundle.scoring.reliability.get("derived", 0.7),
        event_date=None,
        published_at=None,
        flags={"derived"},
        model=None,
        prompt_version=DERIVED_VERSION,
    )


def derived_signals(company: CompanyProfile, bundle: ServiceBundle, now: datetime) -> list[StoredSignal]:
    q = _compliance_question(bundle)
    country = (company.country_code or "").upper()
    if q is None or country not in EU_COUNTRIES:
        return []
    industries = set(company.industry_ids)
    out = []

    annexes = sorted({NIS2_ANNEX[i] for i in industries if i in NIS2_ANNEX})
    big_enough = (company.employees is not None and company.employees >= NIS2_MIN_EMPLOYEES) or (
        company.revenue_eur is not None and company.revenue_eur > NIS2_MIN_REVENUE_EUR
    )
    if annexes and big_enough:
        annex = "I" if "I" in annexes else "II"
        sectors = ", ".join(sorted(i for i in industries if i in NIS2_ANNEX))
        out.append(
            _signal(
                company,
                bundle,
                q,
                now,
                "nis2",
                NIS2_STRENGTH,
                "NIS2 scope (firmographics)",
                NIS2_URL,
                quote=f"{company.name}: {sectors} (NIS2 Annex {annex}), {country}, {_size(company)}.",
                summary=f"Likely in NIS2 scope: {sectors.replace('_', ' ')} company in the EU of NIS2 size "
                f"(Annex {annex}).",
            )
        )

    finance = sorted(industries & DORA_INDUSTRIES)
    if finance:
        out.append(
            _signal(
                company,
                bundle,
                q,
                now,
                "dora",
                DORA_STRENGTH,
                "DORA scope (firmographics)",
                DORA_URL,
                quote=f"{company.name}: {', '.join(finance)}, {country}.",
                summary=(
                    "In DORA scope: EU financial entity subject to the Digital Operational Resilience Act."
                ),
            )
        )
    return out


def derived_for_store(company: CompanyProfile, bundle: ServiceBundle, now: datetime) -> list[VerifiedSignal]:
    """Derived signals as VerifiedSignal for AnalysisStore.save_extraction (the store assigns ids)."""
    fields = set(VerifiedSignal.model_fields)
    return [VerifiedSignal(**d.model_dump(include=fields)) for d in derived_signals(company, bundle, now)]


def derived_fingerprint(company: CompanyProfile, bundle: ServiceBundle, now: datetime) -> str:
    """Part of the extraction fingerprint: a firmographic change that changes derived signals re-saves them."""
    return ";".join(
        sorted(f"{d.source_name}|{d.strength}|{d.quote}" for d in derived_signals(company, bundle, now))
    )
