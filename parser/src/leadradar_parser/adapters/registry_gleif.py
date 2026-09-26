from collections.abc import AsyncIterator

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..gleif import lei_record, search_lei, ultimate_parent
from ..http import HttpClient
from .common import make_document


class GleifAdapter:
    """Legal entity from GLEIF: legal name, LEI, registration country, ultimate parent (no key, CC0)."""

    id = "gleif"
    source_type = "registry"
    requires_env = None
    rate_limit = RateLimit(requests=1, per_seconds=1, scope="host")  # GLEIF allows 60 requests/min

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        firmographics = company.firmographics
        record = None
        if firmographics and firmographics.lei:
            record = await lei_record(firmographics.lei, http)
        if record is None:
            names = [company.name, *company.aliases]
            if firmographics and firmographics.legal_name:
                names.insert(0, firmographics.legal_name)
            record = await search_lei(company, http, names)
        if record is None:
            return
        parent = await ultimate_parent(record.lei, http)
        lines = [
            f"Legal name: {record.legal_name}",
            f"LEI: {record.lei}",
            f"Registration country: {record.country_code or 'n/a'}",
            f"Jurisdiction: {record.jurisdiction or 'n/a'}",
            f"Headquarters city: {record.city or 'n/a'}",
            f"Registered as: {record.registered_as or 'n/a'}",
            f"Entity status: {record.entity_status or 'n/a'}; LEI registration: {record.registration_status or 'n/a'}",
            f"Ultimate parent: {f'{parent.legal_name} (LEI {parent.lei})' if parent else 'none reported'}",
        ]
        yield make_document(
            source_type="registry",
            source_name=self.id,
            url=f"https://search.gleif.org/#/record/{record.lei}",
            title=f"{record.legal_name} — GLEIF legal entity record",
            text="\n".join(lines),
            language="en",
            meta={
                "structured": {
                    "legal_name": record.legal_name,
                    "lei": record.lei,
                    "country_code": record.country_code,
                    "jurisdiction": record.jurisdiction,
                    "hq_city": record.city,
                    "legal_form": record.legal_form,
                    "entity_status": record.entity_status,
                    "registration_status": record.registration_status,
                    "parent_lei": parent.lei if parent else None,
                    "parent_name": parent.legal_name if parent else None,
                },
                "attribution": "GLEIF (CC0)",
            },
        )
