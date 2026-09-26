"""HubSpot add-on (SPEC core CO-A3), behind FEATURE_HUBSPOT, authenticated with a private app token.

Consumer of `lead.tier_changed`: upserts the company by domain, writes the score as `leadradar_*` company
properties (created on first use when missing) and attaches a note with the why-now evidence.
Idempotent per event apart from the note (at-least-once: a retried event may add a second note).
"""

import html
from datetime import UTC, datetime
from typing import Any

import httpx
from structlog import get_logger

from leadradar_core.modules.activity import events
from leadradar_core.modules.activity.dispatcher import Event
from leadradar_core.settings import AppSettings

log = get_logger(__name__)

TIMEOUT_S = 15.0
NOTE_TO_COMPANY = 190  # HUBSPOT_DEFINED association type id: note → company

PROPERTIES = [
    ("leadradar_tier", "LeadRadar tier", "string", "text"),
    ("leadradar_priority", "LeadRadar priority", "number", "number"),
    ("leadradar_fit", "LeadRadar fit", "number", "number"),
    ("leadradar_intent", "LeadRadar intent", "number", "number"),
    ("leadradar_risk", "LeadRadar risk", "number", "number"),
    ("leadradar_service", "LeadRadar service", "string", "text"),
]


class HubSpotError(RuntimeError):
    pass


def score_properties(p: dict[str, Any]) -> dict[str, Any]:
    props = {
        "leadradar_tier": p.get("tier_after"),
        "leadradar_priority": p.get("priority"),
        "leadradar_fit": p.get("fit"),
        "leadradar_intent": p.get("intent"),
        "leadradar_risk": p.get("risk"),
        "leadradar_service": p.get("service_name"),
    }
    return {k: v for k, v in props.items() if v is not None}


def note_body(p: dict[str, Any]) -> str:
    e = html.escape
    head = (
        f"<p><b>LeadRadar: {e(str(p.get('service_name') or ''))}</b> — tier "
        f"{e(str(p.get('tier_before') or 'new'))} → {e(str(p.get('tier_after')))}, priority {p.get('priority')} "
        f"(fit {p.get('fit')}, intent {p.get('intent')}, risk {p.get('risk')})</p>"
    )
    items = []
    for r in p.get("why_now") or []:
        source = e(str(r.get("source_name") or "source"))
        link = f' — <a href="{e(r["url"])}">{source}</a>' if r.get("url") else f" — {source}"
        date = f" ({e(str(r['date']))})" if r.get("date") else ""
        items.append(f"<li>{e(str(r.get('text') or ''))}{link}{date}</li>")
    return head + (f"<p>Why now:</p><ul>{''.join(items)}</ul>" if items else "")


class HubSpotSync:
    name = "hubspot"
    event_types = frozenset({events.LEAD_TIER_CHANGED})

    def __init__(self, token: str, base_url: str = "https://api.hubapi.com") -> None:
        self._headers = {"Authorization": f"Bearer {token}"}
        self._base = base_url.rstrip("/")
        self._properties_ready = False

    async def _call(self, client: httpx.AsyncClient, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            res = await client.request(method, f"{self._base}{path}", headers=self._headers, **kw)
        except httpx.HTTPError as e:
            raise HubSpotError(f"{method} {path}: {type(e).__name__}") from None
        return res

    async def _ensure_properties(self, client: httpx.AsyncClient) -> None:
        if self._properties_ready:
            return
        for name, label, type_, field_type in PROPERTIES:
            res = await self._call(
                client,
                "POST",
                "/crm/v3/properties/companies",
                json={
                    "name": name,
                    "label": label,
                    "type": type_,
                    "fieldType": field_type,
                    "groupName": "companyinformation",
                },
            )
            if res.status_code not in (200, 201, 409):  # 409: already exists
                log.warning("hubspot_property_not_created", property=name, status=res.status_code)
        self._properties_ready = True

    async def _upsert_company(self, client: httpx.AsyncClient, p: dict[str, Any]) -> str:
        props = score_properties(p)
        found = await self._call(
            client,
            "POST",
            "/crm/v3/objects/companies/search",
            json={
                "filterGroups": [
                    {"filters": [{"propertyName": "domain", "operator": "EQ", "value": p["domain"]}]}
                ],
                "properties": ["domain", "name"],
                "limit": 1,
            },
        )
        _check(found, "search company")
        results = found.json().get("results") or []
        if results:
            company_id = str(results[0]["id"])
            res = await self._call(
                client, "PATCH", f"/crm/v3/objects/companies/{company_id}", json={"properties": props}
            )
            _check(res, "update company")
            return company_id
        props |= {"domain": p["domain"], "name": p.get("company_name") or p["domain"]}
        res = await self._call(client, "POST", "/crm/v3/objects/companies", json={"properties": props})
        _check(res, "create company")
        return str(res.json()["id"])

    async def handle(self, event: Event) -> None:
        p = event.payload
        if not p.get("domain"):
            log.info("hubspot_skip_no_domain", event_id=str(event.id))
            return
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            await self._ensure_properties(client)
            company_id = await self._upsert_company(client, p)
            res = await self._call(
                client,
                "POST",
                "/crm/v3/objects/notes",
                json={
                    "properties": {
                        "hs_timestamp": datetime.now(UTC).isoformat(),
                        "hs_note_body": note_body(p),
                    },
                    "associations": [
                        {
                            "to": {"id": company_id},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": NOTE_TO_COMPANY,
                                }
                            ],
                        }
                    ],
                },
            )
            _check(res, "create note")


def _check(res: httpx.Response, what: str) -> None:
    if res.is_error:
        raise HubSpotError(f"{what}: HTTP {res.status_code}: {res.text[:200]}")


def hubspot_consumers(s: AppSettings) -> list[HubSpotSync]:
    if not s.FEATURE_HUBSPOT:
        return []
    if not s.HUBSPOT_PRIVATE_APP_TOKEN:
        log.info("hubspot_disabled", reason="APP_HUBSPOT_PRIVATE_APP_TOKEN is empty")
        return []
    return [HubSpotSync(s.HUBSPOT_PRIVATE_APP_TOKEN, s.HUBSPOT_BASE_URL)]
