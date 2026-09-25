from fastapi import APIRouter
from leadradar_core.modules.meta.schemas import CountryOut, IndustryOut, LabelsOut, PresetOut, UsageOut

router = APIRouter(prefix="/meta", tags=["meta"])

DEFAULT_INDUSTRIES = [
    IndustryOut(id="logistics", label="Logistics & Supply Chain"),
    IndustryOut(id="manufacturing", label="Manufacturing & Industrial"),
    IndustryOut(id="banking", label="Banking & Financial Services"),
    IndustryOut(id="insurance", label="Insurance"),
    IndustryOut(id="healthcare", label="Healthcare & Pharma"),
    IndustryOut(id="retail", label="Retail & E-commerce"),
    IndustryOut(id="telecom", label="Telecommunications"),
    IndustryOut(id="energy", label="Energy & Utilities"),
]

DEFAULT_COUNTRIES = [
    CountryOut(code="DE", name="Germany", is_eu=True),
    CountryOut(code="FR", name="France", is_eu=True),
    CountryOut(code="NL", name="Netherlands", is_eu=True),
    CountryOut(code="AT", name="Austria", is_eu=True),
    CountryOut(code="CH", name="Switzerland", is_eu=False),
    CountryOut(code="GB", name="United Kingdom", is_eu=False),
    CountryOut(code="SE", name="Sweden", is_eu=True),
    CountryOut(code="ES", name="Spain", is_eu=True),
    CountryOut(code="IT", name="Italy", is_eu=True),
    CountryOut(code="PL", name="Poland", is_eu=True),
]

DEFAULT_PRESETS = [
    PresetOut(
        key="intelligent_automation",
        name="Intelligent Automation",
        description="RPA, AI agents, process mining, cost reduction initiatives in European enterprise.",
    ),
    PresetOut(
        key="cybersecurity",
        name="Cybersecurity & Compliance",
        description="NIS2, DORA compliance, security breaches, CISO appointments, SOC modernization.",
    ),
]


@router.get("/industries", response_model=list[IndustryOut])
async def get_industries() -> list[IndustryOut]:
    return DEFAULT_INDUSTRIES


@router.get("/countries", response_model=list[CountryOut])
async def get_countries() -> list[CountryOut]:
    return DEFAULT_COUNTRIES


@router.get("/presets", response_model=list[PresetOut])
async def get_presets() -> list[PresetOut]:
    return DEFAULT_PRESETS


@router.get("/labels", response_model=LabelsOut)
async def get_labels() -> LabelsOut:
    return LabelsOut(
        categories={
            "ai_automation": "AI & Automation Projects",
            "hiring": "Hiring & Talent Growth",
            "leadership": "Leadership & Strategy Shifts",
            "tech_stack": "Technology Stack in Use",
            "compliance": "Compliance & Regulations (NIS2/DORA)",
            "incident": "Security Incidents & Breaches",
        },
        weights={
            "high": "High (+3.0)",
            "medium": "Medium (+2.0)",
            "low": "Low (+1.0)",
        },
        statuses={
            "active": "Active",
            "superseded": "Superseded",
            "rejected_by_user": "Rejected by User",
        },
    )


@router.get("/usage", response_model=UsageOut)
async def get_usage() -> UsageOut:
    return UsageOut()
