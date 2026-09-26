"""Signal temperature: a signal's strength (weak / moderate / strong) shown to sales as cold / medium / hot.

What makes evidence cold, medium or hot depends on the category (hiring: an internship is cold, a hiring drive
is hot), so every category has a default guide; a question may bring its own (QuestionConfig.temperature).
The extraction prompt shows the guide of each question, the model grades `strength` by it.
"""

from leadradar_ai.contracts import QuestionConfig

TEMPERATURE_NAMES: dict[str, str] = {"weak": "cold", "moderate": "medium", "strong": "hot"}

# the generic rule, for a category without a guide of its own
GENERIC_TEMPERATURE: dict[str, str] = {
    "weak": "Generic or marketing language.",
    "moderate": "A stated plan or intent without specifics.",
    "strong": (
        "Explicit and specific: a named program, budget, target, date, open roles or an appointed person."
    ),
}

# every signal category of the presets (presets.SIGNAL_CATEGORIES), in the same order
DEFAULT_TEMPERATURE: dict[str, dict[str, str]] = {
    "cost_efficiency": {
        "weak": "General talk about efficiency or cost discipline; no program, no numbers.",
        "moderate": "A cost or efficiency program is announced, but without amounts, targets or dates.",
        "strong": (
            "A named program with a savings target, amount, job cuts or a deadline "
            "(e.g. '€1bn savings by 2027')."
        ),
    },
    "digital_transformation": {
        "weak": "Generic digital or innovation language on strategy or marketing pages.",
        "moderate": "A transformation initiative or strategy is announced without budget, scope or timeline.",
        "strong": (
            "A named transformation program with budget, scope, timeline or a dedicated leader or team."
        ),
    },
    "ai_automation": {
        "weak": "Interest in AI or automation, an idea or a conference talk; nothing in operation.",
        "moderate": "A pilot, proof of concept or planned project in a named area.",
        "strong": (
            "A named AI, RPA, agentic AI or process mining project in production or being rolled out, "
            "or a budget for it."
        ),
    },
    "hiring": {
        "weak": "1–2 relevant openings, an internship or a working-student role.",  # noqa: RUF001
        "moderate": "3–9 relevant openings, or one senior automation, AI or process role.",  # noqa: RUF001
        "strong": "10 or more relevant openings, a new team being built, or a hiring drive announced.",
    },
    "leadership_change": {
        "weak": "A change below C-level, in an unrelated function, or only a rumour.",
        "moderate": (
            "A new director or head of a relevant function (operations, IT, digital, automation, security)."
        ),
        "strong": (
            "A new C-level executive (COO, CIO, CDO, CTO, CISO) or head of transformation appointed "
            "in the last months."
        ),
    },
    "shared_services": {
        "weak": "Existing shared services are mentioned; no change planned.",
        "moderate": "Plans to consolidate processes or expand a shared service center, without details.",
        "strong": (
            "A new or expanding shared service center, or a consolidation program with scope, location "
            "or timeline."
        ),
    },
    "tech_stack": {
        "weak": "A technology is mentioned in passing or as a nice-to-have in a job ad.",
        "moderate": "A relevant platform (e.g. SAP, UiPath, Celonis, ServiceNow) is used in some areas.",
        "strong": (
            "A major platform rollout or migration is under way (e.g. SAP S/4HANA), or the platform is used "
            "company-wide."
        ),
    },
    "tech_partners": {
        "weak": "A past or one-off project with a provider.",
        "moderate": "An ongoing partnership with a provider in a relevant area.",
        "strong": "A strategic, long-term or exclusive partnership that covers this service.",
    },
    "incident": {
        "weak": "An unconfirmed report, a minor outage or an incident at a supplier.",
        "moderate": "A confirmed incident with limited impact (e.g. phishing, a contained breach).",
        "strong": "A major attack (ransomware, data theft, outage) confirmed in the last months.",
    },
    "compliance": {
        "weak": "The regulation is mentioned in general.",
        "moderate": "The company states it is in scope or preparing, without a deadline.",
        "strong": "A concrete deadline, audit, fine or compliance program is named.",
    },
    "investment": {
        "weak": "General statements about investing in technology.",
        "moderate": "An investment or budget is announced without amounts.",
        "strong": "A named budget, funding round or investment with an amount.",
    },
    "expansion": {
        "weak": "Growth ambitions without specifics.",
        "moderate": "New sites, markets or products are announced.",
        "strong": "A large expansion, acquisition or new operations with dates or amounts.",
    },
    "internal_capability": {
        "weak": "A small internal team or a few specialists.",
        "moderate": "A dedicated in-house team for this area.",
        "strong": "A large center of excellence or own team that builds and runs such solutions.",
    },
    "distress": {
        "weak": "Weak results or a cautious outlook.",
        "moderate": "Losses, a profit warning or a hiring freeze.",
        "strong": "Insolvency, restructuring, large layoffs or a spending freeze.",
    },
}


def temperature_guide(question: QuestionConfig) -> dict[str, str]:
    """{"weak", "moderate", "strong"} → text: the question's own guide when it sets all three levels, else
    the default of its category (the generic rule for a category without one)."""
    own = question.temperature
    if all(own.get(level) for level in TEMPERATURE_NAMES):
        return {level: own[level] for level in TEMPERATURE_NAMES}
    return dict(DEFAULT_TEMPERATURE.get(question.category, GENERIC_TEMPERATURE))
