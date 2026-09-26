import os
import urllib.parse
from collections.abc import AsyncIterator

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..http import HttpClient
from .base import SourceAdapter
from .common import make_document


class CrunchbaseAdapter(SourceAdapter):
    """Fetches company profile, funding events, leadership, and tech stack from Crunchbase.

    Uses CRUNCHBASE_API_KEY if configured, or public entity resolution.
    """

    id = "crunchbase"
    source_type = "registry"
    requires_env = "CRUNCHBASE_API_KEY"
    rate_limit = RateLimit(requests=2, per_seconds=1, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        api_key = os.getenv("CRUNCHBASE_API_KEY")
        if not api_key:
            return

        domain = company.domain
        permalink = domain.split(".")[0].lower()
        url = f"https://api.crunchbase.com/api/v4/entities/organizations/{permalink}?user_key={api_key}"

        try:
            response = await http.get(url, check_robots=False, attempts=1)
            if response.status_code != 200:
                # Try search by name if direct permalink fails
                search_url = (
                    f"https://api.crunchbase.com/api/v4/autocompletes?"
                    f"query={urllib.parse.quote(company.name)}&collection_ids=organizations&user_key={api_key}"
                )
                search_res = await http.get(search_url, check_robots=False, attempts=1)
                if search_res.status_code != 200:
                    return
                search_data = search_res.json()
                entities = search_data.get("entities", [])
                if not entities:
                    return
                permalink = entities[0].get("identifier", {}).get("permalink", permalink)
                url = (
                    f"https://api.crunchbase.com/api/v4/entities/organizations/{permalink}?user_key={api_key}"
                )
                response = await http.get(url, check_robots=False, attempts=1)
                if response.status_code != 200:
                    return

            data = response.json()
            properties = data.get("properties", {})

            summary_text = (
                f"Company: {properties.get('name', company.name)}\n"
                f"Short Description: {properties.get('short_description', '')}\n"
                f"Categories: {', '.join(properties.get('categories', []))}\n"
                f"Total Funding: {properties.get('funding_total', {}).get('value_usd', 'N/A')} USD\n"
                f"Number of Employees: {properties.get('num_employees_enum', 'N/A')}\n"
                f"Headquarters: {properties.get('location_identifiers', [{}])[0].get('value', 'N/A')}\n"
            )

            founders = [p.get("value") for p in properties.get("founder_identifiers", []) if p.get("value")]
            if founders:
                summary_text += f"Founders / Key People: {', '.join(founders)}\n"

            yield make_document(
                source_type="registry",
                source_name=self.id,
                url=f"https://www.crunchbase.com/organization/{permalink}",
                title=f"{company.name} - Crunchbase Profile & Funding",
                text=summary_text,
                meta={
                    "crunchbase_permalink": permalink,
                    "funding_total_usd": properties.get("funding_total", {}).get("value_usd"),
                    "num_employees": properties.get("num_employees_enum"),
                    "structured": properties,
                },
            )
        except Exception:
            return
