from .contracts import CompanyCandidate, DiscoveryQuery
from .http import HttpClient, create_http_client
from .wikidata import discover_companies


async def discover(query: DiscoveryQuery, *, http: HttpClient | None = None) -> list[CompanyCandidate]:
    if http is not None:
        return await discover_companies(query, http)
    async with create_http_client() as owned_http:
        return await discover_companies(query, owned_http)
