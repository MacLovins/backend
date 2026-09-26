"""Add-ons as outbox consumers (CO-A2 alerts, CO-A3 HubSpot). All network calls are mocked (respx, fake SMTP)."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import respx
from leadradar_core import integrations
from leadradar_core.integrations import alerts, hubspot
from leadradar_core.modules.activity import events
from leadradar_core.modules.activity.dispatcher import Event
from leadradar_core.settings import AppSettings

HUBSPOT = "https://hubspot.test"


def event(event_type: str, **payload) -> Event:
    return Event(id=uuid4(), org_id=uuid4(), type=event_type, payload=payload, created_at=datetime.now(UTC))


def hot_lead(**over) -> Event:
    payload = {
        "company_id": str(uuid4()),
        "company_name": "Acme <Logistics>",
        "domain": "acme.example",
        "service_id": str(uuid4()),
        "service_name": "Intelligent automation",
        "tier_before": "warm",
        "tier_after": "hot",
        "priority": 71.5,
        "fit": 80.0,
        "intent": 70.0,
        "risk": 5.0,
        "why_now": [
            {"text": "Runs agentic AI in operations.", "source_name": "gdelt", "url": "https://n.example/1"}
        ],
    }
    return event(events.LEAD_TIER_CHANGED, **(payload | over))


def app_settings(**kw) -> AppSettings:
    return AppSettings(_env_file=None, **kw)


# --- registry ----------------------------------------------------------------------------------


def test_addons_are_off_by_default_and_need_credentials():
    assert integrations.enabled_consumers(app_settings()) == []
    assert integrations.enabled_consumers(app_settings(FEATURE_ALERTS=True, FEATURE_HUBSPOT=True)) == []
    names = [
        c.name
        for c in integrations.enabled_consumers(
            app_settings(
                FEATURE_ALERTS=True,
                FEATURE_HUBSPOT=True,
                TELEGRAM_BOT_TOKEN="t",
                TELEGRAM_CHAT_ID="42",
                SMTP_HOST="smtp.test",
                SMTP_FROM="radar@x.io",
                ALERTS_EMAIL_TO="a@x.io, b@x.io",
                HUBSPOT_PRIVATE_APP_TOKEN="pat",
            )
        )
    ]
    assert names == ["alerts.telegram", "alerts.email", "hubspot"]


# --- alerts ------------------------------------------------------------------------------------


def test_alert_filter_hot_leads_and_high_weight_signals():
    assert alerts.is_alert(hot_lead())
    assert not alerts.is_alert(hot_lead(tier_after="warm", tier_before="cold"))
    assert not alerts.is_alert(hot_lead(tier_before="hot"))
    assert alerts.is_alert(event(events.SIGNAL_DETECTED, weight="high"))
    assert not alerts.is_alert(event(events.SIGNAL_DETECTED, weight="medium"))
    assert not alerts.is_alert(event(events.RUN_FINISHED, status="succeeded"))


@respx.mock
async def test_telegram_alert_is_sent_escaped_and_token_never_leaks():
    route = respx.post("https://api.telegram.org/botSECRET/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    consumer = alerts.TelegramAlerts("SECRET", "42", "https://radar.test")
    await consumer.handle(hot_lead())
    await consumer.handle(event(events.SIGNAL_DETECTED, weight="low"))  # filtered: no request
    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    assert body["chat_id"] == "42" and body["parse_mode"] == "HTML"
    assert "Acme &lt;Logistics&gt;" in body["text"] and "Runs agentic AI" in body["text"]
    assert "https://radar.test/leads/" in body["text"]

    route.mock(return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))
    with pytest.raises(RuntimeError) as err:
        await consumer.handle(hot_lead())
    assert "401" in str(err.value) and "SECRET" not in str(err.value)
    route.mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(RuntimeError) as err:
        await consumer.handle(hot_lead())
    assert "SECRET" not in str(err.value)


async def test_email_alert_goes_through_smtp(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            sent.append(("starttls",))

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, msg):
            sent.append(("send", msg["To"], msg["Subject"], msg.get_content()))

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    s = app_settings(
        SMTP_HOST="smtp.test",
        SMTP_USERNAME="u",
        SMTP_PASSWORD="p",
        SMTP_FROM="radar@x.io",
        ALERTS_EMAIL_TO="a@x.io,b@x.io",
    )
    signal = event(
        events.SIGNAL_DETECTED,
        weight="high",
        company_name="Acme",
        service_name="IA",
        summary="Hires an RPA lead",
        quote="we are hiring an RPA lead",
        category="hiring",
        polarity="positive",
        strength="strong",
        source_name="greenhouse",
        url="https://jobs.example/1",
        company_id="c",
        service_id="s",
    )
    await alerts.EmailAlerts(s).handle(signal)
    assert sent[0] == ("connect", "smtp.test", 587) and ("starttls",) in sent and ("login", "u") in sent
    _, to, subject, content = sent[-1]
    assert to == "a@x.io, b@x.io" and subject == "[LeadRadar] Strong signal: Acme (IA)"
    assert "we are hiring an RPA lead" in content


# --- HubSpot -----------------------------------------------------------------------------------


def hubspot_routes(existing: list[dict]) -> dict[str, respx.Route]:
    hubspot.HubSpotClient._ready.clear()  # properties are remembered per token across the process
    respx.post(f"{HUBSPOT}/crm/v3/properties/companies/groups").mock(return_value=httpx.Response(409))
    respx.get(f"{HUBSPOT}/account-info/v3/details").mock(return_value=httpx.Response(200, json={}))
    return {
        "props": respx.post(f"{HUBSPOT}/crm/v3/properties/companies").mock(return_value=httpx.Response(409)),
        "search": respx.post(f"{HUBSPOT}/crm/v3/objects/companies/search").mock(
            return_value=httpx.Response(200, json={"results": existing})
        ),
        "create": respx.post(f"{HUBSPOT}/crm/v3/objects/companies").mock(
            return_value=httpx.Response(201, json={"id": "900"})
        ),
        "update": respx.patch(f"{HUBSPOT}/crm/v3/objects/companies/777").mock(
            return_value=httpx.Response(200, json={"id": "777"})
        ),
        "note": respx.post(f"{HUBSPOT}/crm/v3/objects/notes").mock(
            return_value=httpx.Response(201, json={"id": "1"})
        ),
    }


@respx.mock
async def test_hubspot_creates_company_with_score_and_evidence_note():
    routes = hubspot_routes([])
    sync = hubspot.HubSpotSync("pat", HUBSPOT, "https://radar.test")
    await sync.handle(hot_lead())
    assert routes["props"].call_count == len(hubspot.PROPERTIES)
    assert routes["search"].calls[0].request.headers["Authorization"] == "Bearer pat"
    created = json.loads(routes["create"].calls[0].request.content)["properties"]
    assert created["domain"] == "acme.example" and created["leadradar_tier"] == "hot"
    assert created["leadradar_priority"] == 71.5 and created["name"] == "Acme <Logistics>"
    assert created["leadradar_why_now"] == "• Runs agentic AI in operations."
    assert created["leadradar_url"].startswith("https://radar.test/leads/")
    # the same property set the manual push writes (modules.integrations.hubspot.PROPERTIES)
    assert {p["name"] for p in hubspot.PROPERTIES} <= set(created)
    note = json.loads(routes["note"].calls[0].request.content)
    assert note["associations"][0]["to"]["id"] == "900"
    assert note["associations"][0]["types"][0]["associationTypeId"] == hubspot.NOTE_TO_COMPANY
    body = note["properties"]["hs_note_body"]
    assert (
        "WARM → HOT (72/100)" in body
        and "Runs agentic AI in operations." in body
        and 'href="https://n.example/1"' in body
    )

    await sync.handle(hot_lead())  # properties are ensured once per process
    assert routes["props"].call_count == len(hubspot.PROPERTIES)


@respx.mock
async def test_hubspot_updates_existing_company_and_reports_errors():
    routes = hubspot_routes([{"id": "777", "properties": {"domain": "acme.example"}}])
    sync = hubspot.HubSpotSync("pat", HUBSPOT, "https://radar.test")
    await sync.handle(hot_lead(tier_after="cold", tier_before="hot"))
    assert routes["create"].call_count == 0
    patched = json.loads(routes["update"].calls[0].request.content)["properties"]
    assert patched["leadradar_tier"] == "cold" and "domain" not in patched

    routes["note"].mock(return_value=httpx.Response(403, json={"message": "missing scope"}))
    with pytest.raises(hubspot.HubSpotError, match="HubSpot 403: missing scope"):
        await sync.handle(hot_lead())


async def test_hubspot_skips_leads_without_domain():
    with respx.mock(assert_all_called=False) as mock:
        await hubspot.HubSpotSync("pat", HUBSPOT).handle(hot_lead(domain=None))
        assert mock.calls.call_count == 0
