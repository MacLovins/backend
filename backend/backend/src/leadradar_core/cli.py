import asyncio
import csv
import io
import json
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import typer
from leadradar_auth.schemas import UserCreate
from leadradar_auth.service import AuthService
from sqlalchemy import select

from leadradar_core.db.models import Org
from leadradar_core.db.session import async_session_factory
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.config.models import (
    ICPProfile,
    ScoringProfile,
    Service,
    SignalQuestion,
)
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
    preset_definitions = {
        "intelligent_automation": {
            "name": "Intelligent Automation",
            "description": "RPA, AI agents, process mining, cost reduction initiatives in European enterprise.",
            "value_proposition": "We accelerate enterprise process automation by 3x while cutting manual operational costs by 40%.",
            "decision_makers": [
                "COO",
                "CIO",
                "Head of Digital Transformation",
                "Head of Automation",
                "Head of Process Excellence",
            ],
            "questions": [
                {
                    "key": "ia_ai_projects",
                    "text": "Is the company deploying or evaluating AI agents, LLMs, or autonomous workflows in operations?",
                    "category": "ai_automation",
                    "polarity": "positive",
                    "weight": "high",
                    "source_types": ["news", "website", "jobs"],
                },
                {
                    "key": "ia_rpa_legacy",
                    "text": "Does the company utilize legacy RPA tools (UiPath, Blue Prism, Automation Anywhere) looking for modernization?",
                    "category": "tech_stack",
                    "polarity": "positive",
                    "weight": "medium",
                    "source_types": ["jobs", "newsroom"],
                },
                {
                    "key": "ia_process_mining",
                    "text": "Is there evidence of process mining (Celonis, Signavio) initiatives or process bottleneck analysis?",
                    "category": "ai_automation",
                    "polarity": "positive",
                    "weight": "medium",
                    "source_types": ["news", "jobs"],
                },
            ],
            "icp": {
                "countries": ["DE", "FR", "NL", "CH", "GB", "SE", "ES", "IT"],
                "industries_any": ["logistics", "manufacturing", "banking", "insurance", "retail"],
                "employees_min": 1000,
                "employees_max": None,
                "revenue_min_eur": Decimal("100000000"),
            },
        },
        "cybersecurity": {
            "name": "Cybersecurity & Compliance",
            "description": "NIS2, DORA compliance, security breaches, CISO appointments, SOC modernization.",
            "value_proposition": "End-to-end NIS2/DORA regulatory readiness and automated cyber risk monitoring.",
            "decision_makers": ["CISO", "CIO", "Head of Information Security", "VP Compliance & Risk"],
            "questions": [
                {
                    "key": "cyber_nis2_prep",
                    "text": "Is the organization within scope of NIS2 or DORA regulations preparing critical infrastructure compliance?",
                    "category": "compliance",
                    "polarity": "positive",
                    "weight": "high",
                    "source_types": ["news", "report", "website"],
                },
                {
                    "key": "cyber_ciso_hire",
                    "text": "Has the company recently appointed a new CISO, CIO, or Head of Security?",
                    "category": "leadership",
                    "polarity": "positive",
                    "weight": "medium",
                    "source_types": ["news", "jobs"],
                },
                {
                    "key": "cyber_soc_modernization",
                    "text": "Is the enterprise expanding its Security Operations Center (SOC) or adopting XDR / SIEM solutions?",
                    "category": "tech_stack",
                    "polarity": "positive",
                    "weight": "high",
                    "source_types": ["jobs", "website"],
                },
            ],
            "icp": {
                "countries": ["DE", "FR", "NL", "CH", "GB", "SE", "ES", "IT", "PL"],
                "industries_any": ["energy", "telecom", "banking", "logistics", "healthcare"],
                "employees_min": 500,
                "employees_max": None,
                "revenue_min_eur": Decimal("50000000"),
            },
        },
    }

    services = {}
    for key in preset_keys:
        clean_key = key.strip().lower()
        if clean_key not in preset_definitions:
            typer.echo(f"Warning: preset '{clean_key}' not recognized, skipping")
            continue

        p_data = preset_definitions[clean_key]
        stmt = select(Service).where(Service.org_id == org_id, Service.slug == clean_key)
        service = (await session.execute(stmt)).scalar_one_or_none()

        if not service:
            service = Service(
                org_id=org_id,
                name=p_data["name"],
                slug=clean_key,
                description=p_data["description"],
                value_proposition=p_data["value_proposition"],
                decision_makers=p_data["decision_makers"],
                is_active=True,
            )
            session.add(service)
            await session.commit()
            await session.refresh(service)
            typer.echo(f"Created preset service: {service.name} ({service.slug})")

            # Seed Questions
            for q_def in p_data["questions"]:
                q = SignalQuestion(
                    org_id=org_id,
                    service_id=service.id,
                    key=q_def["key"],
                    text=q_def["text"],
                    category=q_def["category"],
                    polarity=q_def["polarity"],
                    weight=q_def["weight"],
                    source_types=q_def["source_types"],
                    keywords_status="ready",
                    version=1,
                    is_active=True,
                )
                session.add(q)

            # Seed ICP
            icp_def = p_data["icp"]
            icp = ICPProfile(
                org_id=org_id,
                service_id=service.id,
                countries=icp_def["countries"],
                industries_any=icp_def["industries_any"],
                employees_min=icp_def["employees_min"],
                employees_max=icp_def["employees_max"],
                revenue_min_eur=icp_def["revenue_min_eur"],
                version=1,
            )
            session.add(icp)

            # Seed Scoring Profile
            sc_profile = ScoringProfile(
                org_id=org_id,
                service_id=service.id,
                version=1,
                params={
                    "weights": {"fit": 0.35, "intent": 0.45, "risk": 0.20},
                    "thresholds": {"hot": 70.0, "warm": 45.0},
                },
                is_current=True,
            )
            session.add(sc_profile)
            await session.commit()
        else:
            typer.echo(f"Service '{service.name}' already exists")

        services[clean_key] = service

    return services


async def _seed_accounts_from_csv(session, org_id: UUID, csv_path: Path) -> dict[str, Company]:
    if not csv_path.exists():
        typer.echo(f"Warning: accounts CSV not found at {csv_path}")
        return {}

    text_data = csv_path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text_data))
    created_count = 0
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

        if not existing:
            emp = int(row["employees"]) if row.get("employees") and row["employees"].isdigit() else None
            rev = (
                Decimal(row["revenue_eur"])
                if row.get("revenue_eur") and row["revenue_eur"].isdigit()
                else None
            )
            ind_ids = [s.strip() for s in row.get("industry_ids", "").split(";") if s.strip()]
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
            companies[normalized] = existing

    await session.commit()
    typer.echo(f"Seeded accounts: {created_count} new companies imported (total {len(companies)})")
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


async def _run_seed(presets: str, accounts: str) -> None:
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
        accounts_path = Path(accounts)
        companies = await _seed_accounts_from_csv(session, settings.DEFAULT_ORG_ID, accounts_path)

        # 5. Seed DHL Demo Lead Score
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
) -> None:
    """Seed initial data (organization, admin/sales users, preset services, and demo accounts)."""
    asyncio.run(_run_seed(presets, accounts))


if __name__ == "__main__":
    app()
