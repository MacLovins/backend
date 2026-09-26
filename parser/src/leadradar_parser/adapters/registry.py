from ..contracts import AdapterInfo
from ..settings import ParserSettings
from .base import SourceAdapter
from .incident_hibp import HibpAdapter
from .jobs_adzuna import AdzunaAdapter
from .jobs_ats import JobsAtsAdapter
from .jobs_careers_html import CareersHtmlAdapter
from .news_gdelt import GdeltAdapter
from .news_google import GoogleNewsAdapter
from .news_newsapi import NewsApiAdapter
from .news_rsshub import RSSHubAdapter
from .news_serpapi import SerpApiAdapter
from .registry_crunchbase import CrunchbaseAdapter
from .registry_gleif import GleifAdapter
from .registry_wikidata import WikidataAdapter
from .web_playwright import PlaywrightAdapter
from .web_reports import ReportsAdapter
from .web_site import WebsiteAdapter

ADAPTERS: dict[str, SourceAdapter] = {
    adapter.id: adapter
    for adapter in (
        GoogleNewsAdapter(),
        GdeltAdapter(),
        NewsApiAdapter(),
        SerpApiAdapter(),
        RSSHubAdapter(),
        WebsiteAdapter(),
        PlaywrightAdapter(),
        ReportsAdapter(),
        JobsAtsAdapter(),
        CareersHtmlAdapter(),
        AdzunaAdapter(),
        WikidataAdapter(),
        GleifAdapter(),
        CrunchbaseAdapter(),
        HibpAdapter(),
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
            and (adapter.requires_env is None or bool(settings.env(adapter.requires_env))),
            requires_key=adapter.requires_env,
            rate_limit=adapter.rate_limit,
        )
        for adapter in ADAPTERS.values()
    ]
