"""Versioned prompts live in files: prompts/<name>/<version>/{system.md, user.jinja, examples.json}.

Any change to a prompt or its examples → new version directory; the version is part of the LLM cache key
and of the extraction fingerprint, and it is stored in every signal.
"""

import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from jinja2 import Environment, StrictUndefined


def _attr(value: object) -> str:
    """Safe value for an XML-like attribute in the prompt."""
    return str(value if value is not None else "").replace('"', "'").replace("\n", " ")


_ENV = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True, autoescape=False)
_ENV.filters["attr"] = _attr


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    system_template: str
    user_template: str
    examples: list[dict[str, Any]]

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    def render_user(self, **context: Any) -> str:
        return _ENV.from_string(self.user_template).render(**context).strip()

    def render_system(self, **context: Any) -> str:
        return _ENV.from_string(self.system_template).render(**context).strip()


@cache
def load_prompt(name: str, version: str) -> Prompt:
    base = resources.files("leadradar_ai.prompts").joinpath(name, version)
    examples_file = base.joinpath("examples.json")
    return Prompt(
        name=name,
        version=version,
        system_template=base.joinpath("system.md").read_text(encoding="utf-8"),
        user_template=base.joinpath("user.jinja").read_text(encoding="utf-8"),
        examples=json.loads(examples_file.read_text(encoding="utf-8")) if examples_file.is_file() else [],
    )
