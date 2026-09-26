from collections.abc import AsyncIterator
from time import monotonic
from typing import Any

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..errors import ParserError
from ..http import HttpClient, shared_lock
from ..normalize import utc_datetime
from .common import make_document, plain_text

CATALOGUE_URL = "https://haveibeenpwned.com/api/v3/breaches"
ATTRIBUTION = "Have I Been Pwned (CC BY 4.0)"
CATALOGUE_TTL_S = 86_400.0

# Process-wide copy of the public catalogue (~1 MB, ~1,000 breaches): downloaded at most once a day.
_catalogue: tuple[float, list[dict[str, Any]]] | None = None


class HibpAdapter:
    """Data breaches of the company's own domains from the public HIBP breach catalogue (SPEC PR-15).

    Only the public catalogue is used: no key, no e-mail or account search. Each matching breach becomes one
    `incident` document with `meta.attribution` as the CC BY 4.0 licence requires.
    """

    id = "hibp"
    source_type = "incident"
    requires_env = None
    rate_limit = RateLimit(requests=1, per_seconds=86_400, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        domains = company_domains(company)
        if not domains:
            return
        breaches = await breach_catalogue(http)
        emitted = 0
        for breach in sorted(breaches, key=lambda item: str(item.get("AddedDate") or ""), reverse=True):
            if not _matches(str(breach.get("Domain") or ""), domains):
                continue
            if breach.get("IsFabricated") or breach.get("IsSpamList"):
                continue
            added = utc_datetime(breach.get("AddedDate"))
            breach_date = utc_datetime(breach.get("BreachDate"))
            latest = max((value for value in (added, breach_date) if value), default=None)
            if latest is not None and latest < plan.since:
                continue
            yield _document(breach, added)
            emitted += 1
            if emitted >= plan.max_items_per_source:
                return


async def breach_catalogue(http: HttpClient) -> list[dict[str, Any]]:
    global _catalogue
    async with shared_lock("hibp"):
        if _catalogue is not None and _catalogue[0] > monotonic():
            return _catalogue[1]
        payload = (await http.get(CATALOGUE_URL, headers={"Accept": "application/json"})).json()
        if not isinstance(payload, list):
            raise ParserError("HIBP returned an unexpected breach catalogue")
        breaches = [item for item in payload if isinstance(item, dict)]
        _catalogue = (monotonic() + CATALOGUE_TTL_S, breaches)
        return breaches


def company_domains(company: ResolvedCompany) -> set[str]:
    """The company's own domains, without ATS or other third-party hosts collected during resolve."""
    ats_host = company.ats.host.lower() if company.ats and company.ats.host else None
    domains = {company.domain, *company.own_domains}
    return {d.lower().removeprefix("www.") for d in domains if d and d.lower() != ats_host and "." in d}


def _matches(breach_domain: str, domains: set[str]) -> bool:
    host = breach_domain.lower().strip().removeprefix("www.")
    return bool(host) and any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _document(breach: dict[str, Any], added: Any) -> Document:
    name = str(breach.get("Name") or "")
    title = str(breach.get("Title") or name)
    data_classes = [str(item) for item in breach.get("DataClasses") or []]
    count = breach.get("PwnCount")
    lines = [
        f"Data breach: {title} ({breach.get('Domain')})",
        f"Breach date: {breach.get('BreachDate') or 'unknown'}",
        f"Added to Have I Been Pwned: {str(breach.get('AddedDate') or 'unknown')[:10]}",
        f"Accounts affected: {count if count is not None else 'unknown'}",
        f"Compromised data: {', '.join(data_classes) or 'unknown'}",
        f"Verified: {'yes' if breach.get('IsVerified') else 'no'}",
        plain_text(str(breach.get("Description") or "")),
        f"Source: {ATTRIBUTION}",
    ]
    return make_document(
        source_type="incident",
        source_name="hibp",
        url=f"https://haveibeenpwned.com/PwnedWebsites#{name}",
        title=f"{title} data breach",
        text="\n".join(line for line in lines if line),
        # The breach became public when HIBP added it; the breach itself may be years older.
        published_at=added,
        language="en",
        meta={
            "attribution": ATTRIBUTION,
            "breach_name": name,
            "breach_domain": breach.get("Domain"),
            "breach_date": breach.get("BreachDate"),
            "pwn_count": count,
            "data_classes": data_classes,
            "is_verified": bool(breach.get("IsVerified")),
            "is_sensitive": bool(breach.get("IsSensitive")),
        },
    )
