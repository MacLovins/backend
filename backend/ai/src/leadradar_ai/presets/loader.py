"""Service presets (SPEC AI-12, ARCHITECTURE §3.5): YAML templates that core seeds into the database.

A preset has no ids: core assigns them when it creates the service. `to_bundle()` builds a ServiceBundle with
stable uuid5 ids for the CLI, evals and tests.
"""

from functools import cache
from importlib import resources
from typing import Any, Self
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from pydantic import Field, model_validator

from leadradar_ai.contracts import (
    Contract,
    ICPConfig,
    Polarity,
    QuestionConfig,
    RuleConfig,
    ScoringProfile,
    ServiceBundle,
    SourceType,
    Weight,
)

# ARCHITECTURE §3.3 — categories are data, but a preset must use known ones (UI labels exist for them)
SIGNAL_CATEGORIES = {
    "cost_efficiency": "Cost reduction & efficiency",
    "digital_transformation": "Digital transformation",
    "ai_automation": "AI & automation projects",
    "hiring": "Relevant hiring",
    "leadership_change": "New leadership",
    "shared_services": "Shared services & consolidation",
    "tech_stack": "Technology in use",
    "tech_partners": "Existing technology partners",
    "incident": "Cyber incidents",
    "compliance": "Regulatory & compliance",
    "investment": "IT & security investment",
    "expansion": "Expansion & M&A",
    "internal_capability": "Strong in-house capability",
    "distress": "Spending blockers",
}


class PresetQuestion(Contract):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str  # short name for the UI ("Automation & AI projects")
    text: str
    category: str
    polarity: Polarity
    weight: Weight
    source_types: set[SourceType] = Field(min_length=1)
    recency_days: int = Field(gt=0)
    keywords_seed: dict[str, list[str]] = Field(default_factory=dict)  # expand_question starts from these
    job_titles: list[str] = Field(default_factory=list)
    negative_terms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _known_category(self) -> Self:
        if self.category not in SIGNAL_CATEGORIES:
            raise ValueError(f"unknown category '{self.category}'")
        return self


class PresetRule(Contract):
    name: str
    kind: str
    condition: dict
    action: str
    cap_value: float | None = None


class Preset(Contract):
    key: str
    name: str
    description: str
    value_proposition: str
    decision_makers: list[str]
    icp: ICPConfig
    rules: list[PresetRule]
    scoring: dict[str, Any] = Field(default_factory=dict)  # overrides of ScoringProfile defaults
    questions: list[PresetQuestion] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        keys = [q.key for q in self.questions]
        if len(keys) != len(set(keys)):
            raise ValueError("question keys must be unique")
        for rule in self.to_rule_configs():  # validates condition shapes
            cond = rule.condition
            if rule.kind == "signal" and cond["question_key"] not in keys:
                raise ValueError(f"rule '{rule.name}' refers to unknown question '{cond['question_key']}'")
        ScoringProfile(id=self._uuid("scoring"), version=1, **self.scoring)
        return self

    def _uuid(self, *parts: str) -> UUID:
        return uuid5(NAMESPACE_URL, "leadradar:preset:" + ":".join([self.key, *parts]))

    def to_rule_configs(self) -> list[RuleConfig]:
        return [
            RuleConfig(id=self._uuid("rule", r.name), **r.model_dump())  # type: ignore[arg-type]
            for r in self.rules
        ]

    def to_bundle(self, service_id: UUID | None = None) -> ServiceBundle:
        questions = [
            QuestionConfig(
                id=self._uuid("question", q.key),
                version=1,
                keywords=q.keywords_seed,
                **q.model_dump(exclude={"label", "keywords_seed"}),
            )
            for q in self.questions
        ]
        return ServiceBundle(
            service_id=service_id or self._uuid("service"),
            key=self.key,
            name=self.name,
            description=self.description,
            value_proposition=self.value_proposition,
            questions=questions,
            icp=self.icp,
            rules=self.to_rule_configs(),
            scoring=ScoringProfile(id=self._uuid("scoring"), version=1, **self.scoring),
        )


def _preset_files() -> dict[str, Any]:
    base = resources.files("leadradar_ai.presets")
    return {f.name.removesuffix(".yaml"): f for f in base.iterdir() if f.name.endswith(".yaml")}


def list_presets() -> list[str]:
    return sorted(_preset_files())


@cache
def load_preset(key: str) -> Preset:
    files = _preset_files()
    if key not in files:
        raise KeyError(f"unknown preset '{key}', available: {', '.join(sorted(files))}")
    return Preset.model_validate(yaml.safe_load(files[key].read_text(encoding="utf-8")))
