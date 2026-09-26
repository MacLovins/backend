from collections.abc import AsyncIterator
from typing import Any

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..errors import SourceRequestFailed
from ..http import HttpClient
from .common import make_document

API_URL = "https://api.crunchbase.com/api/v4"
FIELDS = (
    "short_description,categories,num_employees_enum,funding_total,location_identifiers,founder_identifiers"
)


class CrunchbaseAdapter:
    """Crunchbase API v4 (paid licence, SPEC PR-21: stage-2 interface). Off unless CRUNCHBASE_API_KEY is set."""

    id = "crunchbase"
    source_type = "registry"
    requires_env = "CRUNCHBASE_API_KEY"
    rate_limit = RateLimit(requests=1, per_seconds=1, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        api_key = http.settings.env(self.requires_env)
        if not api_key:
            return
        headers = {"X-cb-user-key": api_key}  # header, so the key never appears in URLs or errors
        permalink = company.firmographics.crunchbase_id if company.firmographics else None
        if not permalink:
            search = await http.get(
                f"{API_URL}/autocompletes",
                check_robots=False,
                attempts=1,
                headers=headers,
                params={"query": company.name, "collection_ids": "organizations", "limit": 1},
            )
            entities = search.json().get("entities") or []
            if not entities:
                return
            permalink = (entities[0].get("identifier") or {}).get("permalink")
        if not permalink:
            return
        try:
            response = await http.get(
                f"{API_URL}/entities/organizations/{permalink}",
                check_robots=False,
                attempts=1,
                headers=headers,
                params={"field_ids": FIELDS},
            )
        except SourceRequestFailed:
            return
        properties: dict[str, Any] = response.json().get("properties") or {}
        funding = (properties.get("funding_total") or {}).get("value_usd")
        lines = [
            f"Company: {properties.get('identifier', {}).get('value') or company.name}",
            f"Description: {properties.get('short_description') or ''}",
            f"Categories: {', '.join(_values(properties.get('categories')))}",
            f"Total funding (USD): {funding if funding is not None else 'n/a'}",
            f"Employees: {properties.get('num_employees_enum') or 'n/a'}",
            f"Headquarters: {', '.join(_values(properties.get('location_identifiers'))) or 'n/a'}",
        ]
        founders = _values(properties.get("founder_identifiers"))
        if founders:
            lines.append(f"Founders: {', '.join(founders)}")
        yield make_document(
            source_type="registry",
            source_name=self.id,
            url=f"https://www.crunchbase.com/organization/{permalink}",
            title=f"{company.name} — Crunchbase profile",
            text="\n".join(lines),
            meta={
                "crunchbase_permalink": permalink,
                "funding_total_usd": funding,
                "num_employees": properties.get("num_employees_enum"),
                "structured": properties,
            },
        )


def _values(items: object) -> list[str]:
    """Crunchbase lists are identifier objects ({"value": ...}) or plain strings."""
    if not isinstance(items, list):
        return []
    values = [item.get("value") if isinstance(item, dict) else item for item in items]
    return [str(value) for value in values if value]
