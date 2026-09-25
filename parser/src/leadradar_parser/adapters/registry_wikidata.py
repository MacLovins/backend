import json
from collections.abc import AsyncIterator

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..http import HttpClient
from ..wikidata import resolve_firmographics
from .common import make_document


class WikidataAdapter:
    id = "wikidata"
    source_type = "registry"
    requires_env = None
    rate_limit = RateLimit(requests=1, per_seconds=1, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        firmographics = company.firmographics or await resolve_firmographics(company, http)
        if firmographics is None:
            return
        qid = firmographics.wikidata_qid or company.wikidata_qid
        url = f"https://www.wikidata.org/wiki/{qid}" if qid else company.homepage_url
        payload = firmographics.model_dump(mode="json", exclude_none=True)
        yield make_document(
            source_type="registry",
            source_name=self.id,
            url=url,
            title=firmographics.legal_name or company.name,
            text=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            meta={"structured": payload},
        )
