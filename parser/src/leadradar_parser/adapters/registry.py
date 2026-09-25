import os

from ..contracts import AdapterInfo
from ..settings import ParserSettings
from .base import SourceAdapter
from .jobs_ats import JobsAtsAdapter
from .jobs_careers_html import CareersHtmlAdapter
from .news_gdelt import GdeltAdapter
from .registry_wikidata import WikidataAdapter
from .web_site import WebsiteAdapter

ADAPTERS: dict[str, SourceAdapter] = {
    adapter.id: adapter
    for adapter in (
        GdeltAdapter(),
        WebsiteAdapter(),
        JobsAtsAdapter(),
        CareersHtmlAdapter(),
        WikidataAdapter(),
    )
}


def list_adapters(settings: ParserSettings | None = None) -> list[AdapterInfo]:
    settings = settings or ParserSettings()
    enabled = set(settings.adapters)
    return [
        AdapterInfo(
            id=adapter.id,
            source_type=adapter.source_type,
            enabled=adapter.id in enabled
            and (adapter.requires_env is None or bool(os.getenv(adapter.requires_env))),
            requires_key=adapter.requires_env,
            rate_limit=adapter.rate_limit,
        )
        for adapter in ADAPTERS.values()
    ]
