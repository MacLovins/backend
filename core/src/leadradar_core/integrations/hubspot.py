"""HubSpot add-on (SPEC core CO-A3), behind FEATURE_HUBSPOT, authenticated with a private app token.

Consumer of `lead.tier_changed`: pushes the company through the shared client (`modules.integrations.hubspot`),
the same one the manual `POST /leads/{id}/push/hubspot` uses, so both write the same `leadradar_*` company
properties (group "leadradar", created on first use) and the same kind of note with the why-now evidence.
Idempotent per event apart from the note (at-least-once: a retried event may add a second note).
"""

from typing import Any

from structlog import get_logger

from leadradar_core.modules.activity import events
from leadradar_core.modules.activity.dispatcher import Event
from leadradar_core.modules.integrations.hubspot import (
    BASE_URL,
    NOTE_TO_COMPANY,
    PROPERTIES,
    HubSpotClient,
    HubSpotError,
    LeadSnapshot,
    lead_link,
    reasons_from_payload,
)
from leadradar_core.settings import AppSettings

__all__ = ["NOTE_TO_COMPANY", "PROPERTIES", "HubSpotError", "HubSpotSync", "hubspot_consumers", "snapshot"]

log = get_logger(__name__)

TIMEOUT_S = 15.0


def _num(value: Any) -> float:
    return 0.0 if value is None else float(value)


def snapshot(p: dict[str, Any], public_origin: str) -> LeadSnapshot:
    """LeadSnapshot from a `lead.tier_changed` payload (see modules.activity.events.lead_tier_changed)."""
    return LeadSnapshot(
        name=p.get("company_name") or p["domain"],
        domain=p["domain"],
        service=p.get("service_name") or "",
        tier="disqualified" if p.get("disqualified") else str(p.get("tier_after") or "cold"),
        tier_before=p.get("tier_before"),
        priority=_num(p.get("priority")),
        fit=_num(p.get("fit")),
        intent=_num(p.get("intent")),
        risk=_num(p.get("risk")),
        why_now=reasons_from_payload(p.get("why_now")),
        lead_url=lead_link(public_origin, p.get("company_id") or "", p.get("service_id")),
    )


class HubSpotSync:
    name = "hubspot"
    event_types = frozenset({events.LEAD_TIER_CHANGED})

    def __init__(self, token: str, base_url: str = BASE_URL, public_origin: str = "") -> None:
        self._token = token
        self._base_url = base_url
        self._origin = public_origin

    async def handle(self, event: Event) -> None:
        p = event.payload
        if not p.get("domain"):
            log.info("hubspot_skip_no_domain", event_id=str(event.id))
            return
        async with HubSpotClient(self._token, base_url=self._base_url, timeout_s=TIMEOUT_S) as client:
            result = await client.push_lead(snapshot(p, self._origin))
        log.info(
            "hubspot_pushed",
            event_id=str(event.id),
            company_id=p.get("company_id"),
            hubspot_company_id=result.company_id,
            created=result.created,
        )


def hubspot_consumers(s: AppSettings) -> list[HubSpotSync]:
    if not s.FEATURE_HUBSPOT:
        return []
    if not s.HUBSPOT_PRIVATE_APP_TOKEN:
        log.info("hubspot_disabled", reason="APP_HUBSPOT_PRIVATE_APP_TOKEN is empty")
        return []
    return [HubSpotSync(s.HUBSPOT_PRIVATE_APP_TOKEN, s.HUBSPOT_BASE_URL, s.PUBLIC_ORIGIN)]
