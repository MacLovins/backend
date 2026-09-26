"""Resolver firmographics ↔ company row (no database needed)."""

from datetime import UTC, datetime
from decimal import Decimal

import leadradar_parser as parser
from leadradar_core.adapters import mapping
from leadradar_core.modules.accounts.models import Company

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def resolved(**firmographics: object) -> parser.ResolvedCompany:
    return parser.ResolvedCompany(
        name="Example AG",
        domain="example.com",
        homepage_url="https://www.example.com/",
        own_domains=["example.com", "www.example.com"],
        firmographics=parser.Firmographics(source="wikidata+gleif", **firmographics),  # type: ignore[arg-type]
        resolved_at=NOW,
    )


def test_apply_resolved_persists_every_firmographics_field() -> None:
    company = Company(name="Example AG", domain="example.com", own_domains=[], industry_ids=[])
    mapping.apply_resolved(
        company,
        resolved(
            country_code="DE",
            hq_city="Bonn",
            industry_ids=["logistics"],
            employees=1200,
            revenue_eur=81_758_000_000,
            lei="529900EXAMPLEPARENT01",
            wikidata_qid="Q157645",
            crunchbase_id="example-ag",
        ),
    )
    assert (company.country_code, company.hq_city, company.industry_ids) == ("DE", "Bonn", ["logistics"])
    assert (company.employees, company.revenue_eur) == (1200, Decimal(81_758_000_000))
    assert (company.lei, company.wikidata_qid, company.crunchbase_id) == (
        "529900EXAMPLEPARENT01",
        "Q157645",
        "example-ag",
    )
    assert company.resolved_at == NOW


def test_apply_resolved_keeps_manual_values() -> None:
    company = Company(
        name="Example AG", domain="example.com", own_domains=[], industry_ids=["banking"], country_code="AT"
    )
    mapping.apply_resolved(company, resolved(country_code="DE", industry_ids=["logistics"], lei="L"))
    assert (company.country_code, company.industry_ids, company.lei) == ("AT", ["banking"], "L")


def test_stored_firmographics_are_passed_to_collection() -> None:
    company = Company(
        name="Example AG",
        domain="example.com",
        lei="529900EXAMPLEPARENT01",
        country_code="DE",
        industry_ids=[],
    )
    firmographics = mapping.firmographics(company)
    assert firmographics is not None and firmographics.lei == "529900EXAMPLEPARENT01"
    assert mapping.firmographics(Company(name="New", domain="new.example", industry_ids=[])) is None
