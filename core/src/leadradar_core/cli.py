import asyncio
import csv
import io
import json
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import leadradar_ai as ai
import typer
from leadradar_auth.schemas import UserCreate
from leadradar_auth.service import AuthService
from sqlalchemy import select

from leadradar_core.db.models import Org
from leadradar_core.db.session import async_session_factory
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.config.models import (
    ScoringProfile,
    Service,
    SignalQuestion,
)
from leadradar_core.modules.config.presets import UnknownPreset, create_service_from_preset
from leadradar_core.modules.intelligence.models import Document, Signal
from leadradar_core.modules.leads.models import LeadScore
from leadradar_core.settings import settings
from leadradar_core.utils.domain import normalize_domain

app = typer.Typer(help="LeadRadar Core CLI")


@app.command()
def export_openapi(output_path: str = "openapi.json") -> None:
    """Export OpenAPI specification to JSON file."""
    from leadradar_core.main import create_app

    application = create_app()
    openapi_schema = application.openapi()
    Path(output_path).write_text(json.dumps(openapi_schema, indent=2), encoding="utf-8")
    typer.echo(f"OpenAPI schema successfully written to {output_path}")


async def _seed_presets(session, org_id: UUID, preset_keys: list[str]) -> dict[str, Service]:
    """Services from the leadradar-ai presets (the same definitions the AI engine is tested with)."""
    services = {}
    for key in preset_keys:
        clean_key = key.strip().lower()
        try:
            service, created = await create_service_from_preset(session, org_id, clean_key)
        except UnknownPreset:
            typer.echo(
                f"Warning: preset '{clean_key}' not recognized (available: {', '.join(ai.list_presets())})"
            )
            continue
        await session.commit()
        typer.echo(
            f"{'Created' if created else 'Kept existing'} preset service: {service.name} ({service.slug})"
        )
        services[clean_key] = service
    return services


async def _seed_accounts_from_csv(session, org_id: UUID, csv_text: str | None) -> dict[str, Company]:
    if not csv_text:
        typer.echo("Warning: accounts CSV is empty or not found")
        return {}

    reader = csv.DictReader(io.StringIO(csv_text))
    created_count = updated_count = 0
    companies = {}

    for row in reader:
        domain_raw = row.get("domain") or row.get("Website") or row.get("Domain")
        name = row.get("name") or row.get("Company Name")
        if not domain_raw or not name:
            continue

        normalized = normalize_domain(domain_raw)
        if not normalized:
            continue

        stmt = select(Company).where(Company.org_id == org_id, Company.domain == normalized)
        existing = (await session.execute(stmt)).scalar_one_or_none()
        ind_ids = [s.strip() for s in row.get("industry_ids", "").split(";") if s.strip()]

        if not existing:
            emp = int(row["employees"]) if row.get("employees") and row["employees"].isdigit() else None
            rev = (
                Decimal(row["revenue_eur"])
                if row.get("revenue_eur") and row["revenue_eur"].isdigit()
                else None
            )
            tags = [s.strip() for s in row.get("tags", "").split(";") if s.strip()]

            company = Company(
                org_id=org_id,
                name=name.strip(),
                domain=normalized,
                country_code=row.get("country_code", "").strip().upper() or None,
                industry_ids=ind_ids,
                employees=emp,
                revenue_eur=rev,
                hq_city=row.get("hq_city"),
                homepage_url=row.get("homepage_url") or f"https://{normalized}",
                careers_url=row.get("careers_url"),
                linkedin_url=row.get("linkedin_url"),
                notes=row.get("notes"),
                tags=tags,
                origin="csv",
                is_tracked=True,
            )
            session.add(company)
            companies[normalized] = company
            created_count += 1
        else:
            # seed rows are reference data: corrected industries reach already seeded companies too
            if existing.origin == "csv" and ind_ids and list(existing.industry_ids or []) != ind_ids:
                existing.industry_ids = ind_ids
                updated_count += 1
            companies[normalized] = existing

    await session.commit()
    typer.echo(
        f"Seeded accounts: {created_count} new companies imported, {updated_count} industries corrected "
        f"(total {len(companies)})"
    )
    return companies


async def _seed_demo_lead_scores(
    session, org_id: UUID, services: dict[str, Service], companies: dict[str, Company]
) -> None:
    # Ensure DHL demo company has full baseline intelligence for demonstration
    dhl = companies.get("dhl.com")
    ia_service = services.get("intelligent_automation")
    if not dhl or not ia_service:
        return

    # Check if lead score already exists
    stmt = select(LeadScore).where(
        LeadScore.company_id == dhl.id,
        LeadScore.service_id == ia_service.id,
        LeadScore.is_current == True,  # noqa: E712
    )
    existing_score = (await session.execute(stmt)).scalar_one_or_none()
    if existing_score:
        return

    # Get scoring profile
    prof_stmt = select(ScoringProfile).where(
        ScoringProfile.service_id == ia_service.id,
        ScoringProfile.is_current == True,  # noqa: E712
    )
    prof = (await session.execute(prof_stmt)).scalar_one_or_none()
    if not prof:
        return

    # Insert DHL documents
    doc1 = Document(
        org_id=org_id,
        company_id=dhl.id,
        source_type="website",
        source_name="dhl.com",
        url="https://dhl.com/global-en/home/press/press-releases/2026/dhl-deploys-ai-agents.html",
        canonical_url="https://dhl.com/press/2026/ai-agents",
        title="DHL Deploys Agentic AI across European Freight Terminals",
        text="DHL Group announced a strategic expansion of autonomous AI agents in freight processing.",
        content_hash="dhl_hash_doc_001",
    )
    session.add(doc1)
    await session.commit()
    await session.refresh(doc1)

    # Insert DHL Signal
    q_stmt = select(SignalQuestion).where(
        SignalQuestion.service_id == ia_service.id,
        SignalQuestion.key == "ia_ai_projects",
    )
    question = (await session.execute(q_stmt)).scalar_one_or_none()
    if question:
        sig = Signal(
            org_id=org_id,
            company_id=dhl.id,
            service_id=ia_service.id,
            question_id=question.id,
            question_key=question.key,
            question_version=1,
            document_id=doc1.id,
            category="ai_automation",
            polarity="positive",
            quote="DHL Supply Chain is rolling out AI agent assistants to orchestrate RFQ quoting automatically.",
            summary="DHL rolled out autonomous AI assistants to orchestrate customer RFQs across 14 hubs.",
            strength="strong",
            confidence=Decimal("0.94"),
            source_type="website",
            source_name="dhl.com",
            url=doc1.url,
            status="active",
        )
        session.add(sig)

    score = LeadScore(
        org_id=org_id,
        company_id=dhl.id,
        service_id=ia_service.id,
        scoring_profile_id=prof.id,
        fit=Decimal("94.00"),
        intent=Decimal("88.50"),
        risk=Decimal("12.00"),
        priority=Decimal("86.20"),
        tier="hot",
        disqualified=False,
        breakdown=[
            {"criterion": "fit_employees", "points": 35.0, "category": "firmographic"},
            {"criterion": "intent_ai_deployment", "points": 45.0, "category": "signal"},
            {"criterion": "risk_penalty", "points": -2.0, "category": "risk"},
        ],
        why_now=[
            {
                "text": "Uses agentic AI in freight operations and recently deployed autonomous RFQ quoting in Europe.",
                "source_name": "dhl.com",
                "date": "2026-06-18",
                "polarity": "positive",
            }
        ],
        is_current=True,
    )
    session.add(score)
    await session.commit()
    typer.echo("Seeded demonstration lead score & verified signals for DHL Group")


async def _run_seed(presets: str, accounts_csv_text: str | None, demo_score: bool = False) -> None:
    async with async_session_factory() as session:
        # 1. Seed Default Org
        org = await session.get(Org, settings.DEFAULT_ORG_ID)
        if not org:
            org = Org(
                id=settings.DEFAULT_ORG_ID,
                org_id=settings.DEFAULT_ORG_ID,
                name="LeadRadar Main Org",
            )
            session.add(org)
            await session.commit()
            typer.echo(f"Created default organization: {org.name} ({org.id})")
        else:
            typer.echo(f"Default organization already exists: {org.name}")

        # 2. Seed Admin and Sales Users
        auth_service = AuthService(session)
        default_users = [
            ("admin@leadradar.ai", "admin12345!", "LeadRadar Admin", "admin"),
            ("sales@leadradar.ai", "sales12345!", "LeadRadar Sales", "sales"),
        ]

        for email, password, full_name, role in default_users:
            existing = await auth_service.repo.get_by_email(email)
            if not existing:
                user = await auth_service.create_user(
                    UserCreate(
                        email=email,
                        password=password,
                        full_name=full_name,
                        role=role,  # type: ignore
                        org_id=settings.DEFAULT_ORG_ID,
                    )
                )
                typer.echo(f"Created user: {user.email} (role: {user.role})")
            else:
                typer.echo(f"User already exists: {email}")

        # 3. Seed Presets
        preset_list = [p.strip() for p in presets.split(",") if p.strip()]
        services = await _seed_presets(session, settings.DEFAULT_ORG_ID, preset_list)

        # 4. Seed Demo Accounts
        companies = await _seed_accounts_from_csv(session, settings.DEFAULT_ORG_ID, accounts_csv_text)

        # 5. Optional placeholder score for UI work before the first real analysis. Its signal is not
        # verified evidence, so it is off by default.
        if demo_score:
            await _seed_demo_lead_scores(session, settings.DEFAULT_ORG_ID, services, companies)

        typer.echo("Seed completed successfully!")


@app.command()
def seed(
    presets: str = typer.Option(
        "intelligent_automation,cybersecurity",
        "--presets",
        help="Comma-separated list of preset services to seed",
    ),
    accounts: str = typer.Option(
        "seeds/demo_accounts.csv",
        "--accounts",
        help="Path to CSV file with demo companies",
    ),
    demo_score: bool = typer.Option(
        False,
        "--demo-score",
        help="Also insert a placeholder DHL lead score (not produced by the analysis; for UI work only)",
    ),
) -> None:
    """Seed initial data (organization, admin/sales users, preset services, and demo accounts)."""
    accounts_path = Path(accounts)
    csv_text = accounts_path.read_text(encoding="utf-8-sig") if accounts_path.exists() else None
    asyncio.run(_run_seed(presets, csv_text, demo_score))


if __name__ == "__main__":
    app()
