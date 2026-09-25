from functools import lru_cache
from importlib.resources import files

import yaml

from .contracts import Country, Industry


def load_data(name: str) -> object:
    """Parse a YAML file shipped in `leadradar_parser/data`."""
    return yaml.safe_load(files("leadradar_parser.data").joinpath(name).read_text(encoding="utf-8"))


def _load_yaml(name: str) -> list[dict[str, object]]:
    parsed = load_data(name)
    if not isinstance(parsed, list):
        raise ValueError(f"{name} must contain a YAML list")
    return parsed


@lru_cache
def _industries() -> tuple[Industry, ...]:
    return tuple(Industry.model_validate(item) for item in _load_yaml("industries.yaml"))


@lru_cache
def _countries() -> tuple[Country, ...]:
    return tuple(Country.model_validate(item) for item in _load_yaml("countries.yaml"))


def industry_taxonomy() -> list[Industry]:
    return list(_industries())


def country_catalog() -> list[Country]:
    return list(_countries())
