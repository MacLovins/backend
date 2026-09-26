"""Trends on leads, activity filters, alert rules (CRUD, preview, matching, delivery), notifications,
company watch, the leads summary and the jobs-threshold task. Real database, fake SMTP, no network.

Every test uses its own org id (and user ids) so it does not interfere with others on the shared database.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import ClassVar
from uuid import UUID, uuid4

import httpx
import pytest
from _lead_fixtures import NOW, ensure_service, headers, seed_lead
from leadradar_auth import UserAccount
from leadradar_core.db.session import async_session_factory, engine
from leadradar_core.integrations import alert_rules, alerts
from leadradar_core.main import create_app
from leadradar_core.modules.activity import events
from leadradar_core.modules.activity.dispatcher import Event, dispatch_pending
from leadradar_core.modules.alerts import service as alerts_service
from leadradar_core.modules.alerts.models import AlertRule, Notification
from leadradar_core.modules.intelligence.models import Document
from leadradar_core.modules.leads import trends
from leadradar_core.settings import AppSettings, settings
from leadradar_core.worker import scheduled
from sqlalchemy import select
from test_leads_list_card import count_queries


@pytest.fixture(autouse=True)
async def fresh_engine() -> AsyncIterator[None]:
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    settings.ENV = "test"
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def org_id() -> UUID:
    return uuid4()


@pytest.fixture
def user_id() -> UUID:
    return uuid4()


def app_settings(**kw) -> AppSettings:
    return AppSettings(_env_file=None, PUBLIC_ORIGIN="https://radar.test", **kw)


async def create_user(org_id: UUID, user_id: UUID, email: str | None = None) -> str:
    email = email or f"{user_id.hex[:8]}@x.io"
    async with async_session_factory() as session, session.begin():
        session.add(UserAccount(id=user_id, org_id=org_id, email=email, password_hash="x", role="sales"))
    return email


async def add_jobs(org_id: UUID, company_id: UUID, n: int, *, hours_ago: float = 1) -> None:
    async with async_session_factory() as session, session.begin():
        for i in range(n):
            url = f"https://jobs.example/{uuid4().hex[:8]}"
            session.add(
                Document(
                    org_id=org_id,
                    company_id=company_id,
                    source_type="jobs",
                    source_name="greenhouse",
                    url=url,
                    canonical_url=url,
                    title=f"RPA developer {i}",
                    text="RPA developer wanted",
                    published_at=NOW - timedelta(hours=hours_ago),
                    content_hash=uuid4().hex,
                )
            )


def signal_event(org_id: UUID, company_id: UUID, service_id: UUID, **over) -> Event:
    payload = {
        "signal_id": str(uuid4()),
        "company_id": str(company_id),
        "company_name": "DHL Group",
        "domain": "dhl.com",
        "service_id": str(service_id),
        "service_name": "Intelligent Automation",
        "question_key": "ia_hiring",
        "weight": "high",
        "category": "hiring",
        "polarity": "positive",
        "strength": "strong",
        "confidence": 0.9,
        "summary": "3 new job postings match 'RPA developer'",
        "quote": "We are hiring RPA developers in Bonn",
        "url": "https://jobs.example/1",
        "source_name": "greenhouse",
    }
    return Event(
        id=uuid4(), org_id=org_id, type=events.SIGNAL_DETECTED, payload=payload | over, created_at=NOW
    )


def tier_event(org_id: UUID, company_id: UUID, service_id: UUID, **over) -> Event:
    payload = {
        "company_id": str(company_id),
        "company_name": "DHL Group",
        "domain": "dhl.com",
        "service_id": str(service_id),
        "service_name": "Intelligent Automation",
        "tier_before": "warm",
        "tier_after": "hot",
        "priority": 71.5,
        "fit": 80.0,
        "intent": 70.0,
        "risk": 5.0,
        "disqualified": False,
        "why_now": [{"text": "Runs agentic AI in operations.", "source_name": "gdelt"}],
    }
    return Event(
        id=uuid4(), org_id=org_id, type=events.LEAD_TIER_CHANGED, payload=payload | over, created_at=NOW
    )


def rule(org_id: UUID, user_id: UUID, **over) -> AlertRule:
    fields = {
        "id": uuid4(),
        "org_id": org_id,
        "user_id": user_id,
        "name": "r",
        "is_active": True,
        "channels": ["inapp"],
        "scope": {"company_ids": None, "service_ids": None},
        "trigger": {"kind": "signal"},
    }
    return AlertRule(**(fields | over))


async def user_notifications(user_id: UUID) -> list[Notification]:
    async with async_session_factory() as session:
        stmt = select(Notification).where(Notification.user_id == user_id).order_by(Notification.created_at)
        return list((await session.execute(stmt)).scalars().all())


# --- trends ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "summary", "expected"),
    [
        ("hiring", "", "hiring"),
        ("distress", "Hiring freeze announced", "distress"),
        ("distress", "Plans Stellenabbau of 500 roles", "layoffs"),
        ("distress", "Redundancies in the Hamburg plant", "layoffs"),
        ("cost_efficiency", "", "cost_cutting"),
        ("incident", "", "cyber_incident"),
        ("leadership_change", "", "leadership_change"),
        ("expansion", "", "growth"),
        ("investment", "", "growth"),
        ("ai_automation", "", "ai_automation"),
        ("digital_transformation", "", "ai_automation"),
        ("compliance", "", "compliance"),
        ("tech_stack", "", None),
        (None, "", None),
    ],
)
def test_trend_kind_mapping(category, summary, expected):
    assert trends.trend_kind(category, summary, "") == expected


async def test_leads_list_has_trends_jobs_open_and_watched_in_three_queries(org_id, user_id, client):
    seeded = await seed_lead(
        org_id,
        signals=(
            ("ia_ai_projects", "Acme runs agentic AI"),
            ("ia_dt", "Strategy 2030 digitizes processes"),
            ("ia_hiring", "Hiring RPA developers"),
            ("ia_distress", "Acme announces layoffs in Q3"),  # distress evidence about job cuts → layoffs
            ("ia_distress", "Acme freezes hiring"),  # plain distress
            ("ia_erp", "Runs SAP S/4HANA"),  # tech_stack carries no trend
        ),
        event_date=datetime(2026, 9, 10).date(),
    )
    await add_jobs(org_id, seeded.company_id, 2)
    await add_jobs(org_id, seeded.company_id, 1, hours_ago=24 * 40)  # outside the 30-day window
    quiet = await seed_lead(org_id, service_id=seeded.service_id, name="Quiet Co", signals=())
    h = headers(org_id, "sales", user_id)
    assert (await client.post(f"/api/v1/companies/{seeded.company_id}/watch", headers=h)).status_code == 200

    with count_queries() as statements:
        res = await client.get("/api/v1/leads", params={"service_id": str(seeded.service_id)}, headers=h)
    assert res.status_code == 200, res.text
    assert len(statements) <= 3
    items = {i["company"]["name"]: i for i in res.json()["items"]}
    acme = items["Acme Logistics"]
    by_kind = {t["kind"]: t for t in acme["trends"]}
    assert [t["kind"] for t in acme["trends"]] == [
        "ai_automation",
        "distress",
        "hiring",
        "layoffs",
    ]  # count desc, then kind
    assert by_kind["ai_automation"] == {
        "kind": "ai_automation",
        "count": 2,
        "latest_at": "2026-09-10",
        "strength": "strong",
    }
    assert by_kind["layoffs"]["count"] == 1 and by_kind["distress"]["count"] == 1
    assert acme["jobs_open"] == 2 and acme["watched"] is True and acme["company"]["watched"] is True
    assert items["Quiet Co"]["trends"] == [] and items["Quiet Co"]["jobs_open"] is None
    assert items["Quiet Co"]["watched"] is False

    # the SQL mapping agrees with the Python one for the seeded evidence
    expected = {
        trends.trend_kind(c, s, q)
        for c, s, q in [
            ("ai_automation", "", ""),
            ("digital_transformation", "", ""),
            ("hiring", "", ""),
            ("distress", "Summary: Acme announces layoffs in Q3", ""),
            ("distress", "Summary: Acme freezes hiring", ""),
            ("tech_stack", "", ""),
        ]
    } - {None}
    assert set(by_kind) == expected

    card = (await client.get(f"/api/v1/leads/{seeded.company_id}", headers=h)).json()
    assert [t["kind"] for t in card["trends"]] == [t["kind"] for t in acme["trends"]]
    assert card["jobs_open"] == 2 and card["watched"] is True and card["company"]["watched"] is True
    other = (await client.get(f"/api/v1/leads/{quiet.company_id}", headers=h)).json()
    assert other["trends"] == [] and other["watched"] is False


async def test_leads_summary(org_id, user_id, client):
    service_id = await ensure_service(org_id)
    a = await seed_lead(org_id, service_id=service_id, name="A", signals=(("ia_hiring", "hiring RPA devs"),))
    await seed_lead(
        org_id,
        service_id=service_id,
        name="B",
        signals=(("ia_hiring", "SOC analysts wanted"), ("ia_cost", "cost programme")),
        detected_days_ago=30,
    )
    await seed_lead(org_id, service_id=service_id, name="C", signals=())
    h = headers(org_id, "sales", user_id)
    await client.post(f"/api/v1/companies/{a.company_id}/watch", headers=h)

    with count_queries() as statements:
        res = await client.get("/api/v1/leads/summary", params={"service_id": str(service_id)}, headers=h)
    assert res.status_code == 200, res.text
    assert len(statements) <= 3
    body = res.json()
    assert body["total"] == 3 and sum(body["by_tier"].values()) == 3
    assert set(body["by_tier"]) == {"hot", "warm", "cold", "disqualified"}
    assert body["new_signals_7d"] == 1 and body["watched"] == 1
    assert body["trends_top"] == [{"kind": "hiring", "count": 2}, {"kind": "cost_cutting", "count": 1}]

    empty = (await client.get("/api/v1/leads/summary", params={"service_id": str(uuid4())}, headers=h)).json()
    assert empty["total"] == 0 and empty["trends_top"] == [] and empty["watched"] == 1


# --- activity filters --------------------------------------------------------------------------


async def test_activity_filters_by_company_type_and_since(org_id, client):
    c1, c2 = uuid4(), uuid4()
    async with async_session_factory() as session, session.begin():
        old = events.emit_event(session, org_id, events.SIGNAL_DETECTED, {"company_id": str(c1), "n": 1})
        old.created_at = NOW - timedelta(days=3)
        events.emit_event(session, org_id, events.LEAD_TIER_CHANGED, {"company_id": str(c1), "n": 2})
        events.emit_event(session, org_id, events.SIGNAL_DETECTED, {"company_id": str(c2), "n": 3})
        events.emit_event(session, org_id, events.RUN_FINISHED, {"run_id": "r", "n": 4})
    h = headers(org_id, "sales")

    def ns(res):
        assert res.status_code == 200, res.text
        return sorted(e["payload"]["n"] for e in res.json())

    assert ns(await client.get("/api/v1/activity", headers=h)) == [1, 2, 3, 4]
    assert ns(await client.get("/api/v1/activity", params={"company_id": str(c1)}, headers=h)) == [1, 2]
    assert ns(await client.get("/api/v1/activity", params={"types": "signal.detected"}, headers=h)) == [1, 3]
    assert ns(
        await client.get(
            "/api/v1/activity", params=[("types", "signal.detected"), ("types", "run.finished")], headers=h
        )
    ) == [1, 3, 4]
    assert ns(
        await client.get("/api/v1/activity", params={"types": "signal.detected,lead.tier_changed"}, headers=h)
    ) == [1, 2, 3]
    since = (NOW - timedelta(days=1)).isoformat()
    assert ns(await client.get("/api/v1/activity", params={"since": since}, headers=h)) == [2, 3, 4]
    assert ns(
        await client.get(
            "/api/v1/activity",
            params={"since": since, "company_id": str(c1), "types": "lead.tier_changed"},
            headers=h,
        )
    ) == [2]


# --- rules: CRUD and validation ----------------------------------------------------------------


async def test_rule_crud_is_user_scoped(org_id, user_id, client):
    h = headers(org_id, "sales", user_id)
    body = {
        "name": "Hiring in my list",
        "channels": ["inapp", "email"],
        "scope": {"company_ids": [str(uuid4())], "service_ids": None},
        "trigger": {"kind": "signal", "categories": ["hiring"], "min_strength": "moderate"},
    }
    created = await client.post("/api/v1/alerts/rules", json=body, headers=h)
    assert created.status_code == 201, created.text
    rule_out = created.json()
    assert rule_out["user_id"] == str(user_id) and rule_out["is_active"] is True
    assert rule_out["trigger"] == {
        "kind": "signal",
        "categories": ["hiring"],
        "polarity": None,
        "min_strength": "moderate",
        "tier_to": None,
        "jobs_min": None,
        "jobs_window_h": None,
    }
    assert rule_out["scope"]["company_ids"] == body["scope"]["company_ids"]

    listed = (await client.get("/api/v1/alerts/rules", headers=h)).json()
    assert [r["id"] for r in listed] == [rule_out["id"]]
    assert (await client.get("/api/v1/alerts/rules", headers=headers(org_id, "sales", uuid4()))).json() == []

    patched = await client.patch(
        f"/api/v1/alerts/rules/{rule_out['id']}",
        json={"is_active": False, "trigger": {"kind": "tier", "tier_to": ["hot"]}, "channels": ["inapp"]},
        headers=h,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["is_active"] is False and patched.json()["trigger"]["tier_to"] == ["hot"]
    assert patched.json()["channels"] == ["inapp"]

    other = headers(org_id, "sales", uuid4())
    assert (
        await client.patch(f"/api/v1/alerts/rules/{rule_out['id']}", json={"name": "x"}, headers=other)
    ).status_code == 404
    assert (await client.delete(f"/api/v1/alerts/rules/{rule_out['id']}", headers=other)).status_code == 404
    assert (await client.delete(f"/api/v1/alerts/rules/{rule_out['id']}", headers=h)).status_code == 204
    assert (await client.get("/api/v1/alerts/rules", headers=h)).json() == []
    assert (await client.get("/api/v1/alerts/rules")).status_code == 401


@pytest.mark.parametrize(
    "trigger",
    [
        {"kind": "bogus"},
        {"kind": "signal", "categories": ["hiring", "not_a_kind"]},
        {"kind": "signal", "min_strength": "huge"},
        {"kind": "signal", "polarity": "neutral"},
        {"kind": "signal", "tier_to": ["hot"]},
        {"kind": "tier", "tier_to": ["lukewarm"]},
        {"kind": "tier", "categories": ["hiring"]},
        {"kind": "jobs_threshold"},
        {"kind": "jobs_threshold", "jobs_min": 0},
        {"kind": "signal", "jobs_min": 5},
    ],
)
async def test_rule_validation(org_id, user_id, client, trigger):
    h = headers(org_id, "sales", user_id)
    res = await client.post("/api/v1/alerts/rules", json={"name": "x", "trigger": trigger}, headers=h)
    assert res.status_code == 422, res.text
    res = await client.post("/api/v1/alerts/rules/preview", json={"name": "x", "trigger": trigger}, headers=h)
    assert res.status_code == 422


async def test_rule_rejects_empty_channels_and_unknown_fields(org_id, user_id, client):
    h = headers(org_id, "sales", user_id)
    base = {"name": "x", "trigger": {"kind": "signal"}}
    assert (
        await client.post("/api/v1/alerts/rules", json={**base, "channels": []}, headers=h)
    ).status_code == 422
    assert (
        await client.post("/api/v1/alerts/rules", json={**base, "channels": ["sms"]}, headers=h)
    ).status_code == 422
    assert (
        await client.post("/api/v1/alerts/rules", json={**base, "extra": 1}, headers=h)
    ).status_code == 422
    ok = await client.post(
        "/api/v1/alerts/rules",
        json={**base, "trigger": {"kind": "jobs_threshold", "jobs_min": 100}},
        headers=h,
    )
    assert ok.status_code == 201 and ok.json()["trigger"]["jobs_window_h"] == 24  # default window: a day


# --- matching ----------------------------------------------------------------------------------


def test_match_scope_and_signal_trigger():
    org, user, company, service = uuid4(), uuid4(), uuid4(), uuid4()
    ev = signal_event(org, company, service)
    origin = "https://radar.test"

    assert alerts_service.match_event(rule(org, user), ev, origin) is not None
    assert alerts_service.match_event(rule(org, user, is_active=False), ev, origin) is None
    assert alerts_service.match_event(rule(uuid4(), user), ev, origin) is None  # another org
    assert alerts_service.match_event(rule(org, user, trigger={"kind": "tier"}), ev, origin) is None

    scoped = rule(org, user, scope={"company_ids": [str(company)], "service_ids": [str(service)]})
    assert alerts_service.match_event(scoped, ev, origin) is not None
    assert (
        alerts_service.match_event(rule(org, user, scope={"company_ids": [str(uuid4())]}), ev, origin) is None
    )
    assert (
        alerts_service.match_event(rule(org, user, scope={"service_ids": [str(uuid4())]}), ev, origin) is None
    )

    by_kind = rule(org, user, trigger={"kind": "signal", "categories": ["hiring", "growth"]})
    assert alerts_service.match_event(by_kind, ev, origin) is not None
    assert (
        alerts_service.match_event(by_kind, signal_event(org, company, service, category="incident"), origin)
        is None
    )
    layoffs = rule(org, user, trigger={"kind": "signal", "categories": ["layoffs"]})
    cuts = signal_event(
        org, company, service, category="distress", summary="Announces job cuts", polarity="negative"
    )
    freeze = signal_event(
        org, company, service, category="distress", summary="Hiring freeze", polarity="negative"
    )
    assert alerts_service.match_event(layoffs, cuts, origin) is not None
    assert alerts_service.match_event(layoffs, freeze, origin) is None
    assert alerts_service.match_event(
        rule(org, user, trigger={"kind": "signal", "categories": ["distress"]}), freeze, origin
    )

    negative = rule(org, user, trigger={"kind": "signal", "polarity": "negative"})
    assert alerts_service.match_event(negative, ev, origin) is None
    assert alerts_service.match_event(negative, cuts, origin) is not None

    strong = rule(org, user, trigger={"kind": "signal", "min_strength": "moderate"})
    assert (
        alerts_service.match_event(strong, signal_event(org, company, service, strength="weak"), origin)
        is None
    )
    assert alerts_service.match_event(
        strong, signal_event(org, company, service, strength="moderate"), origin
    )

    draft = alerts_service.match_event(rule(org, user), ev, origin)
    assert (
        draft.title == "Hiring signal at DHL Group"
        and draft.kind == "signal"
        and draft.trend_kind == "hiring"
    )
    assert draft.body.startswith("3 new job postings match 'RPA developer'")
    assert '"We are hiring RPA developers in Bonn" — greenhouse' in draft.body
    assert "Why it matters: hiring for these roles" in draft.body
    assert draft.url == f"https://radar.test/leads/{company}?service={service}"


def test_match_tier_trigger():
    org, user, company, service = uuid4(), uuid4(), uuid4(), uuid4()
    origin = "https://radar.test"
    any_tier = rule(org, user, trigger={"kind": "tier"})
    hot_only = rule(org, user, trigger={"kind": "tier", "tier_to": ["hot"]})
    hot = tier_event(org, company, service)
    cold = tier_event(org, company, service, tier_before="warm", tier_after="cold")
    assert alerts_service.match_event(any_tier, hot, origin) and alerts_service.match_event(
        any_tier, cold, origin
    )
    assert alerts_service.match_event(hot_only, hot, origin) is not None
    assert alerts_service.match_event(hot_only, cold, origin) is None
    assert alerts_service.match_event(rule(org, user), hot, origin) is None  # a signal rule ignores tiers
    draft = alerts_service.match_event(hot_only, hot, origin)
    assert draft.title == "DHL Group is now HOT for Intelligent Automation" and draft.kind == "tier"
    assert "warm → hot" in draft.body and "Runs agentic AI" in draft.body


# --- consumer: in-app + e-mail delivery --------------------------------------------------------


class FakeSMTP:
    sent: ClassVar[list[tuple]] = []
    fail: ClassVar[bool] = False

    def __init__(self, host, port, timeout):
        FakeSMTP.sent.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        FakeSMTP.sent.append(("login", user))

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise OSError("smtp down")
        FakeSMTP.sent.append(("send", msg["To"], msg["Subject"], msg.get_content()))


@pytest.fixture
def fake_smtp(monkeypatch) -> type[FakeSMTP]:
    FakeSMTP.sent = []
    FakeSMTP.fail = False
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


async def test_consumer_creates_notifications_and_sends_email(org_id, user_id, fake_smtp):
    seeded = await seed_lead(org_id)
    email = await create_user(org_id, user_id)
    other_user = uuid4()
    async with async_session_factory() as session, session.begin():
        session.add_all(
            [
                rule(org_id, user_id, name="all signals", channels=["inapp", "email"]),
                rule(org_id, user_id, name="hot leads", trigger={"kind": "tier", "tier_to": ["hot"]}),
                rule(
                    org_id,
                    user_id,
                    name="cyber only",
                    trigger={"kind": "signal", "categories": ["cyber_incident"]},
                ),
                rule(org_id, other_user, name="in-app only", channels=["inapp"]),
                rule(uuid4(), other_user, name="other org"),
            ]
        )
    s = app_settings(SMTP_HOST="smtp.test", SMTP_USERNAME="u", SMTP_PASSWORD="p", SMTP_FROM="radar@x.io")
    consumer = alert_rules.AlertRulesConsumer(s)
    assert consumer.name == "alerts.rules" and events.SIGNAL_DETECTED in consumer.event_types

    ev = signal_event(org_id, seeded.company_id, seeded.service_id)
    await consumer.handle(ev)
    await consumer.handle(ev)  # re-delivered by the outbox: no duplicate
    await consumer.handle(tier_event(org_id, seeded.company_id, seeded.service_id))

    mine = await user_notifications(user_id)
    assert [(n.kind, n.trend_kind, n.delivered) for n in mine] == [
        ("signal", "hiring", {"email": "sent"}),
        ("tier", None, {}),
    ]
    assert mine[0].event_id == ev.id and mine[0].company_id == seeded.company_id
    assert mine[0].service_id == seeded.service_id and mine[0].read_at is None
    assert mine[0].url == f"https://radar.test/leads/{seeded.company_id}?service={seeded.service_id}"
    theirs = await user_notifications(other_user)
    assert [n.delivered for n in theirs] == [{}]  # in-app only: no e-mail attempted
    _, to, subject, content = fake_smtp.sent[-1]
    assert to == email and subject == "[LeadRadar] Hiring signal at DHL Group"
    assert "Why it matters" in content and mine[0].url in content
    assert ("login", "u") in fake_smtp.sent and len([x for x in fake_smtp.sent if x[0] == "send"]) == 1


async def test_consumer_email_failure_and_missing_smtp_are_recorded_not_raised(org_id, user_id, fake_smtp):
    seeded = await seed_lead(org_id)
    await create_user(org_id, user_id)
    no_email_user = uuid4()
    async with async_session_factory() as session, session.begin():
        session.add(rule(org_id, user_id, channels=["email"]))
        session.add(rule(org_id, no_email_user, channels=["email"]))

    fake_smtp.fail = True
    smtp = app_settings(SMTP_HOST="smtp.test", SMTP_FROM="radar@x.io")
    await alert_rules.AlertRulesConsumer(smtp).handle(
        signal_event(org_id, seeded.company_id, seeded.service_id)
    )
    assert [n.delivered for n in await user_notifications(user_id)] == [{"email": "failed"}]
    assert [n.delivered for n in await user_notifications(no_email_user)] == [{"email": "skipped"}]

    # without SMTP the in-app notification still arrives; e-mail is skipped
    assert alert_rules.mailer_for(app_settings()) is None
    await alert_rules.AlertRulesConsumer(app_settings()).handle(
        signal_event(org_id, seeded.company_id, seeded.service_id)
    )
    assert [n.delivered for n in await user_notifications(user_id)][-1] == {"email": "skipped"}


async def test_consumer_runs_through_the_outbox_dispatcher(org_id, user_id):
    seeded = await seed_lead(org_id)
    async with async_session_factory() as session, session.begin():
        session.add(rule(org_id, user_id))
        payload = signal_event(org_id, seeded.company_id, seeded.service_id).payload
        events.emit_event(session, org_id, events.SIGNAL_DETECTED, payload)
    await dispatch_pending(
        async_session_factory, [alert_rules.AlertRulesConsumer(app_settings())], batch_size=1000
    )
    assert len(await user_notifications(user_id)) == 1


# --- notifications API -------------------------------------------------------------------------


async def test_notifications_api(org_id, user_id, client):
    seeded = await seed_lead(org_id)
    async with async_session_factory() as session, session.begin():
        r = rule(org_id, user_id)
        session.add(r)
        await session.flush()
        for i in range(3):
            ev = signal_event(org_id, seeded.company_id, seeded.service_id, summary=f"signal {i}")
            ev = Event(ev.id, ev.org_id, ev.type, ev.payload, NOW + timedelta(seconds=i))
            await alerts_service.notify(
                session, r, alerts_service.render_signal(ev, "https://radar.test"), None
            )
    h = headers(org_id, "sales", user_id)

    listed = await client.get("/api/v1/notifications", headers=h)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 3 and listed.json()[0]["title"] == "Hiring signal at DHL Group"
    first = listed.json()[0]
    assert first["kind"] == "signal" and first["trend_kind"] == "hiring" and first["read_at"] is None
    assert first["rule_id"] == str(r.id) and first["company_id"] == str(seeded.company_id)
    assert len((await client.get("/api/v1/notifications", params={"limit": 2}, headers=h)).json()) == 2
    assert (await client.get("/api/v1/notifications/unread-count", headers=h)).json() == {"count": 3}

    read = await client.post(f"/api/v1/notifications/{first['id']}/read", headers=h)
    assert read.status_code == 200 and read.json()["read_at"] is not None
    assert (await client.get("/api/v1/notifications/unread-count", headers=h)).json() == {"count": 2}
    unread = (await client.get("/api/v1/notifications", params={"unread_only": True}, headers=h)).json()
    assert len(unread) == 2 and first["id"] not in {n["id"] for n in unread}

    stranger = headers(org_id, "sales", uuid4())
    assert (
        await client.post(f"/api/v1/notifications/{first['id']}/read", headers=stranger)
    ).status_code == 404
    assert (await client.get("/api/v1/notifications", headers=stranger)).json() == []
    assert (await client.post("/api/v1/notifications/read-all", headers=stranger)).json() == {"updated": 0}

    assert (await client.post("/api/v1/notifications/read-all", headers=h)).json() == {"updated": 2}
    assert (await client.get("/api/v1/notifications/unread-count", headers=h)).json() == {"count": 0}
    assert (await client.get("/api/v1/notifications")).status_code == 401


# --- preview -----------------------------------------------------------------------------------


async def test_preview_replays_the_last_30_days(org_id, user_id, client):
    seeded = await seed_lead(org_id)
    other_company = uuid4()
    async with async_session_factory() as session, session.begin():
        for i, (company, over) in enumerate(
            [
                (seeded.company_id, {}),
                (seeded.company_id, {"category": "incident", "summary": "Ransomware attack"}),
                (other_company, {}),
                (seeded.company_id, {"strength": "weak"}),
            ]
        ):
            ev = signal_event(org_id, company, seeded.service_id, **over)
            row = events.emit_event(session, org_id, ev.type, ev.payload)
            row.created_at = NOW - timedelta(days=i)
        stale = events.emit_event(
            session,
            org_id,
            events.SIGNAL_DETECTED,
            signal_event(org_id, seeded.company_id, seeded.service_id).payload,
        )
        stale.created_at = NOW - timedelta(days=40)
        events.emit_event(
            session,
            org_id,
            events.LEAD_TIER_CHANGED,
            tier_event(org_id, seeded.company_id, seeded.service_id).payload,
        )
    h = headers(org_id, "sales", user_id)

    async def preview(trigger, scope=None):
        body = {"name": "p", "trigger": trigger, "scope": scope or {}}
        res = await client.post("/api/v1/alerts/rules/preview", json=body, headers=h)
        assert res.status_code == 200, res.text
        return res.json()

    everything = await preview({"kind": "signal"})
    assert everything["count"] == 4 and len(everything["notifications"]) == 4
    assert everything["notifications"][0]["title"] == "Hiring signal at DHL Group"
    assert everything["notifications"][0]["occurred_at"].startswith(str(NOW.date()))
    assert (await preview({"kind": "signal", "categories": ["hiring"]}))["count"] == 3
    assert (await preview({"kind": "signal", "min_strength": "moderate"}))["count"] == 3
    assert (await preview({"kind": "signal"}, {"company_ids": [str(seeded.company_id)]}))["count"] == 3
    assert (await preview({"kind": "tier", "tier_to": ["hot"]}))["count"] == 1
    assert (await preview({"kind": "tier", "tier_to": ["cold"]}))["count"] == 0

    await add_jobs(org_id, seeded.company_id, 3)
    jobs = await preview({"kind": "jobs_threshold", "jobs_min": 3, "jobs_window_h": 24})
    assert jobs["count"] == 1 and jobs["notifications"][0]["kind"] == "jobs_threshold"
    assert jobs["notifications"][0]["title"] == "Hiring spike at Acme Logistics: 3 job postings"
    assert (await preview({"kind": "jobs_threshold", "jobs_min": 4}))["count"] == 0
    # nothing stored by a preview
    assert (await client.get("/api/v1/notifications", headers=h)).json() == []
    assert (await client.get("/api/v1/alerts/rules", headers=h)).json() == []


# --- jobs threshold task -----------------------------------------------------------------------


async def test_jobs_threshold_task_notifies_once_per_window(org_id, user_id, fake_smtp, monkeypatch):
    service_id = await ensure_service(org_id)
    busy = await seed_lead(org_id, service_id=service_id, name="Busy Co", signals=())
    quiet = await seed_lead(org_id, service_id=service_id, name="Quiet Co", signals=())
    await add_jobs(org_id, busy.company_id, 3, hours_ago=2)
    await add_jobs(org_id, quiet.company_id, 1, hours_ago=2)
    await create_user(org_id, user_id)
    async with async_session_factory() as session, session.begin():
        session.add(
            rule(
                org_id,
                user_id,
                name="3+ jobs a day",
                channels=["inapp", "email"],
                scope={"company_ids": None, "service_ids": [str(service_id)]},
                trigger={"kind": "jobs_threshold", "jobs_min": 3, "jobs_window_h": 24},
            )
        )
        session.add(rule(org_id, user_id, name="signals", trigger={"kind": "signal"}))  # not a jobs rule
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(settings, "SMTP_FROM", "radar@x.io")
    monkeypatch.setattr(settings, "PUBLIC_ORIGIN", "https://radar.test")

    assert await scheduled.evaluate_jobs_thresholds(now=NOW, only_org=org_id) == 1
    assert await scheduled.evaluate_jobs_thresholds(now=NOW + timedelta(hours=3), only_org=org_id) == 0
    [n] = await user_notifications(user_id)
    assert n.kind == "jobs_threshold" and n.company_id == busy.company_id and n.service_id == service_id
    assert n.title == "Hiring spike at Busy Co: 3 job postings" and "threshold: 3" in n.body
    assert n.delivered == {"email": "sent"} and n.url.endswith(
        f"/leads/{busy.company_id}?service={service_id}"
    )

    # a new window: the postings are still within 24 h of `now`, the last notification is not
    later = NOW + timedelta(hours=25)
    async with async_session_factory() as session, session.begin():
        for row in await user_notifications(user_id):
            (await session.get(Notification, row.id)).created_at = NOW - timedelta(hours=30)
        for doc in (
            await session.execute(select(Document).where(Document.company_id == busy.company_id))
        ).scalars():
            doc.published_at = later - timedelta(hours=1)
    assert await scheduled.evaluate_jobs_thresholds(now=later, only_org=org_id) == 1
    assert len(await user_notifications(user_id)) == 2


# --- watch / unwatch ---------------------------------------------------------------------------


async def test_watch_and_unwatch_company(org_id, user_id, client):
    seeded = await seed_lead(org_id, name="DHL Group")
    h = headers(org_id, "sales", user_id)
    company = f"/api/v1/companies/{seeded.company_id}"
    assert (await client.get(company, headers=h)).json()["watched"] is False

    res = await client.post(f"{company}/watch", headers=h)
    assert res.status_code == 200, res.text
    watch_rule = res.json()
    assert watch_rule["name"] == "DHL Group — any signal" and watch_rule["channels"] == ["inapp", "email"]
    assert watch_rule["scope"] == {"company_ids": [str(seeded.company_id)], "service_ids": None}
    assert watch_rule["trigger"]["kind"] == "signal" and watch_rule["trigger"]["categories"] is None
    assert (await client.post(f"{company}/watch", headers=h)).json()["id"] == watch_rule["id"]  # idempotent
    assert (await client.get(company, headers=h)).json()["watched"] is True
    listed = (await client.get("/api/v1/companies", headers=h)).json()["items"]
    assert {c["id"]: c["watched"] for c in listed}[str(seeded.company_id)] is True
    assert (await client.get(company, headers=headers(org_id, "sales", uuid4()))).json()["watched"] is False

    assert (await client.delete(f"{company}/watch", headers=h)).status_code == 204
    assert (await client.get(company, headers=h)).json()["watched"] is False
    rules = (await client.get("/api/v1/alerts/rules", headers=h)).json()
    assert [r["is_active"] for r in rules] == [False]  # kept, deactivated
    again = (await client.post(f"{company}/watch", headers=h)).json()
    assert again["id"] == watch_rule["id"] and again["is_active"] is True

    assert (await client.post(f"/api/v1/companies/{uuid4()}/watch", headers=h)).status_code == 404
    assert (await client.post(f"{company}/watch", headers=headers(uuid4(), "sales"))).status_code == 404
