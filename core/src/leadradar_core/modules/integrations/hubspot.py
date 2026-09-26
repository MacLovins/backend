"""HubSpot CRM client (private app token, CRM API v3): company upsert by domain, LeadRadar score properties,
a note with the evidence. One-way push: LeadRadar never reads or changes anything else in the portal.

Shared by the manual push endpoint (`modules.integrations.router`) and the outbox consumer
(`integrations.hubspot`, `lead.tier_changed`), so a company reaches HubSpot with the same property group,
property names and note layout whichever way it was pushed.

Private app scopes: crm.objects.companies.read, crm.objects.companies.write, crm.schemas.companies.read,
crm.schemas.companies.write. Notes on companies need no extra scope.
"""

import html
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

import httpx
from leadradar_core.modules.leads.trends import lead_link as _lead_link

BASE_URL = "https://api.hubapi.com"
GROUP = "leadradar"
# HUBSPOT_DEFINED association type: note → company
NOTE_TO_COMPANY = 190

TIERS = ("hot", "warm", "cold", "disqualified")
PROPERTIES: list[dict[str, Any]] = [
    {"name": "leadradar_priority", "label": "LeadRadar Priority", "type": "number", "fieldType": "number"},
    {
        "name": "leadradar_tier",
        "label": "LeadRadar Tier",
        "type": "enumeration",
        "fieldType": "select",
        "options": [{"label": t.capitalize(), "value": t, "displayOrder": i} for i, t in enumerate(TIERS)],
    },
    {"name": "leadradar_fit", "label": "LeadRadar Fit", "type": "number", "fieldType": "number"},
    {"name": "leadradar_intent", "label": "LeadRadar Intent", "type": "number", "fieldType": "number"},
    {"name": "leadradar_risk", "label": "LeadRadar Risk", "type": "number", "fieldType": "number"},
    {"name": "leadradar_service", "label": "LeadRadar Service", "type": "string", "fieldType": "text"},
    {"name": "leadradar_why_now", "label": "LeadRadar Why now", "type": "string", "fieldType": "textarea"},
    {"name": "leadradar_url", "label": "LeadRadar Lead", "type": "string", "fieldType": "text"},
    {"name": "leadradar_synced_at", "label": "LeadRadar Synced at", "type": "datetime", "fieldType": "date"},
]


class HubSpotError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"HubSpot {status_code}: {message}")


@dataclass(frozen=True)
class Reason:
    """A why-now reason (ai.Reason without the polarity): text plus its source when known."""

    text: str
    source_name: str | None = None
    url: str | None = None
    date: str | None = None


@dataclass(frozen=True)
class Evidence:
    quote: str
    source_name: str | None
    url: str | None
    event_date: str | None
    question: str


@dataclass(frozen=True)
class LeadSnapshot:
    name: str
    domain: str
    service: str
    tier: str
    priority: float
    fit: float
    intent: float
    risk: float
    lead_url: str
    employees: int | None = None
    city: str | None = None
    tier_before: str | None = None  # set by the tier_changed consumer: the note shows "WARM → HOT"
    why_now: list[Reason] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(frozen=True)
class PushResult:
    company_id: str
    created: bool
    note_id: str
    record_url: str | None


def lead_link(public_origin: str, company_id: UUID | str, service_id: UUID | str | None) -> str:
    """The lead page in the SPA, the same link the alerts use."""
    return _lead_link(public_origin, company_id, service_id)


def reasons_from_payload(rows: list[Any] | None) -> list[Reason]:
    """ai.Reason rows as stored in `lead_score.why_now` and in the `lead.tier_changed` payload."""
    out = []
    for r in rows or []:
        if isinstance(r, dict) and r.get("text"):
            out.append(
                Reason(
                    text=str(r["text"]),
                    source_name=r.get("source_name") or None,
                    url=r.get("url") or None,
                    date=str(r["date"]) if r.get("date") else None,
                )
            )
    return out


def score_properties(lead: LeadSnapshot, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    return {
        "leadradar_priority": round(lead.priority, 1),
        "leadradar_tier": lead.tier if lead.tier in TIERS else "cold",
        "leadradar_fit": round(lead.fit, 1),
        "leadradar_intent": round(lead.intent, 1),
        "leadradar_risk": round(lead.risk, 1),
        "leadradar_service": lead.service,
        "leadradar_why_now": "\n".join(f"• {r.text}" for r in lead.why_now),
        "leadradar_url": lead.lead_url,
        "leadradar_synced_at": int(now.timestamp() * 1000),
    }


def note_body(lead: LeadSnapshot) -> str:
    e = html.escape
    tier = f"{e(lead.tier_before.upper())} → " if lead.tier_before else ""
    parts = [
        f"<p><strong>LeadRadar · {e(lead.service)}: {tier}{e(lead.tier.upper())} "
        f"({lead.priority:.0f}/100)</strong></p>",
        f"<p>Fit {lead.fit:.0f} · Intent {lead.intent:.0f} · Risk {lead.risk:.0f}</p>",
    ]
    if lead.why_now:
        parts.append("<p><strong>Why now</strong></p><ul>")
        for r in lead.why_now:
            source = ", ".join(e(x) for x in (r.source_name, r.date) if x)
            link = f' <a href="{e(r.url, quote=True)}">{source or "source"}</a>' if r.url else ""
            tail = link or (f" ({source})" if source else "")
            parts.append(f"<li>{e(r.text)}{tail}</li>")
        parts.append("</ul>")
    if lead.evidence:
        parts.append("<p><strong>Evidence</strong></p><ul>")
        for ev in lead.evidence:
            source = ", ".join(e(x) for x in (ev.source_name, ev.event_date) if x)
            link = f' <a href="{e(ev.url, quote=True)}">source</a>' if ev.url else ""
            parts.append(f"<li><em>{e(ev.question)}</em><br>“{e(ev.quote)}” ({source}){link}</li>")
        parts.append("</ul>")
    parts.append(f'<p><a href="{e(lead.lead_url, quote=True)}">Open in LeadRadar</a></p>')
    return "".join(parts)


class HubSpotClient:
    # portals whose LeadRadar properties exist already (hash of the token), so a push does not re-create them
    _ready: ClassVar[set[int]] = set()

    def __init__(self, token: str, *, base_url: str = BASE_URL, timeout_s: float = 15) -> None:
        self._key = hash(token)
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    async def __aenter__(self) -> "HubSpotClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _call(
        self, method: str, path: str, *, ok: tuple[int, ...] = (), **kwargs: Any
    ) -> dict[str, Any]:
        try:
            res = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # the token never leaks: httpx errors carry the request, not the headers
            raise HubSpotError(502, f"HubSpot is unreachable: {type(exc).__name__}") from exc
        if res.status_code in ok or res.is_success:
            return res.json() if res.content else {}
        try:
            message = res.json().get("message", res.text)
        except ValueError:
            message = res.text
        raise HubSpotError(res.status_code, str(message)[:300])

    async def ensure_properties(self) -> None:
        """Creates the LeadRadar group and properties; the ones that already exist (409) are left as they are."""
        if self._key in self._ready:
            return
        await self._call(
            "POST",
            "/crm/v3/properties/companies/groups",
            ok=(409,),
            json={"name": GROUP, "label": "LeadRadar", "displayOrder": -1},
        )
        for prop in PROPERTIES:
            await self._call(
                "POST", "/crm/v3/properties/companies", ok=(409,), json=prop | {"groupName": GROUP}
            )
        self._ready.add(self._key)

    async def record_url(self, company_id: str) -> str | None:
        """The UI host depends on data hosting: app.hubspot.com, app-eu1.hubspot.com…"""
        try:
            info = await self._call("GET", "/account-info/v3/details")
        except HubSpotError:
            return None
        portal, host = info.get("portalId"), info.get("uiDomain") or "app.hubspot.com"
        return f"https://{host}/contacts/{portal}/record/0-2/{company_id}" if portal else None

    async def find_company(self, domain: str) -> str | None:
        data = await self._call(
            "POST",
            "/crm/v3/objects/companies/search",
            json={
                "filterGroups": [
                    {"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}
                ],
                "properties": ["domain"],
                "limit": 1,
            },
        )
        results = data.get("results") or []
        return str(results[0]["id"]) if results else None

    async def update_company(self, company_id: str, properties: dict[str, Any]) -> bool:
        """False when the record no longer exists in HubSpot (deleted or merged away)."""
        try:
            await self._call(
                "PATCH", f"/crm/v3/objects/companies/{company_id}", json={"properties": properties}
            )
        except HubSpotError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    async def create_company(self, properties: dict[str, Any]) -> str:
        data = await self._call("POST", "/crm/v3/objects/companies", json={"properties": properties})
        return str(data["id"])

    async def add_note(self, company_id: str, body: str, when: datetime | None = None) -> str:
        when = when or datetime.now(UTC)
        data = await self._call(
            "POST",
            "/crm/v3/objects/notes",
            json={
                "properties": {"hs_timestamp": when.isoformat(), "hs_note_body": body},
                "associations": [
                    {
                        "to": {"id": company_id},
                        "types": [
                            {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_COMPANY}
                        ],
                    }
                ],
            },
        )
        return str(data["id"])

    async def push_lead(self, lead: LeadSnapshot, known_id: str | None = None) -> PushResult:
        """Upsert: the known record id first, then a company with the same domain, else a new company.

        Name, domain, size and city are set only on a new record, so edits made by sales in HubSpot are kept;
        the LeadRadar properties are always overwritten. Each push adds a note: the evidence at that moment.
        """
        await self.ensure_properties()
        props = score_properties(lead)
        company_id = known_id if known_id and await self.update_company(known_id, props) else None
        created = False
        if company_id is None:
            company_id = await self.find_company(lead.domain)
            if company_id is not None:
                await self.update_company(company_id, props)
            else:
                basics: dict[str, Any] = {"name": lead.name, "domain": lead.domain}
                if lead.employees:
                    basics["numberofemployees"] = lead.employees
                if lead.city:
                    basics["city"] = lead.city
                company_id = await self.create_company(basics | props)
                created = True
        note_id = await self.add_note(company_id, note_body(lead))
        url = await self.record_url(company_id)
        return PushResult(company_id=company_id, created=created, note_id=note_id, record_url=url)
