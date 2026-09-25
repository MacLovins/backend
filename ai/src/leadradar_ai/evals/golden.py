"""Golden set format (SPEC §1.7.8).

labels (JSONL), one decision per line:
    {"company_domain": "dhl.com", "service": "intelligent_automation", "question_key": "ia_ai_projects",
     "expected": "yes", "evidence_hint": "agentic AI RFQ processing", "source_url": null, "source": "annex"}

companies (YAML): the profile of every labelled company and the parser fixture it is analysed on.
Labels must be made against the documents in the fixture: a label for a fact the fixture does not contain
measures data coverage, not the model.
"""

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class GoldenLabel(BaseModel):
    company_domain: str
    service: str
    question_key: str
    expected: Literal["yes", "no"]
    evidence_hint: str | None = None
    source_url: str | None = None
    source: str | None = None  # where the label comes from: "annex", "fixture", "manual review"


class CompanyCase(BaseModel):
    domain: str
    name: str
    fixture: str  # file name inside the fixtures directory
    aliases: list[str] = Field(default_factory=list)
    own_domains: list[str] = Field(default_factory=list)
    country: str | None = None
    industries: list[str] = Field(default_factory=list)
    employees: int | None = None
    tags: list[str] = Field(default_factory=list)
    note: str | None = None


def load_labels(path: str | Path) -> list[GoldenLabel]:
    labels = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip() and not line.lstrip().startswith("//"):
            try:
                labels.append(GoldenLabel.model_validate(json.loads(line)))
            except ValueError as e:
                raise ValueError(f"{path}:{n}: {e}") from e
    keys = [(label.company_domain, label.service, label.question_key) for label in labels]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise ValueError(f"{path}: duplicate labels {sorted(duplicates)}")
    return labels


def load_companies(path: str | Path) -> dict[str, CompanyCase]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    cases = [CompanyCase.model_validate(item) for item in raw]
    return {c.domain: c for c in cases}


def default_golden_dir() -> Path:
    return Path(__file__).parent / "golden"
