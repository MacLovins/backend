from collections.abc import AsyncIterator
from typing import Protocol

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany, SourceType
from ..http import HttpClient


class SourceAdapter(Protocol):
    id: str
    source_type: SourceType
    requires_env: str | None
    rate_limit: RateLimit

    def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]: ...
