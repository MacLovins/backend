"""GET /leads (filters in SQL, numbers, aggregates, sort), CSV export and the lead card."""

import csv
import io
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import date
from uuid import UUID, uuid4

import httpx
import pytest
from _lead_fixtures import ensure_service, headers, seed_lead
from leadradar_core.db.session import engine
from leadradar_core.main import create_app
from leadradar_core.settings import settings
from sqlalchemy import event


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


@contextmanager
def count_queries() -> Iterator[list[str]]:
    statements: list[str] = []

    def listener(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", listener)


async def seed_three(org_id: UUID) -> dict[str, UUID]:
    service_id = await ensure_service(org_id)
    fresh = await seed_lead(org_id, service_id=service_id, name="Fresh Co", event_date=date(2026, 9, 10))
    old = await seed_lead(org_id, service_id=service_id, name="Old Co", country="FR", detected_days_ago=30)
    none = await seed_lead(org_id, service_id=service_id, name="Quiet Co", country="NL", signals=())
    return {"service": service_id, "fresh": fresh.company_id, "old": old.company_id, "none": none.company_id}


async def test_has_new_is_filtered_before_pagination(org_id, client):
    ids = await seed_three(org_id)
    h = headers(org_id, "sales")
    params = {"service_id": str(ids["service"]), "page_size": 1}

    all_rows = (await client.get("/api/v1/leads", params=params, headers=h)).json()
    assert all_rows["total"] == 3 and len(all_rows["items"]) == 1

    new_only = (await client.get("/api/v1/leads", params={**params, "has_new": True}, headers=h)).json()
    assert new_only["total"] == 1
    assert [i["company"]["id"] for i in new_only["items"]] == [str(ids["fresh"])]
    assert new_only["items"][0]["new_signals_7d"] == 1

    not_new = (await client.get("/api/v1/leads", params={**params, "has_new": False}, headers=h)).json()
    assert not_new["total"] == 2


async def test_list_returns_numbers_aggregates_and_flags_in_one_query(org_id, client):
    ids = await seed_three(org_id)
    await seed_lead(org_id, service_id=ids["service"], name="Soft Vendor", industries=("software",))
    h = headers(org_id, "sales")

    with count_queries() as statements:
        res = await client.get("/api/v1/leads", params={"service_id": str(ids["service"])}, headers=h)
    assert res.status_code == 200, res.text
    assert len(statements) <= 3  # count + page + trends of the page, no per-row queries
    items = {i["company"]["name"]: i for i in res.json()["items"]}

    fresh = items["Fresh Co"]
    for key in ("priority", "fit", "intent", "risk"):
        assert isinstance(fresh["score"][key], (int, float)), key
    assert isinstance(fresh["company"]["revenue_eur"], (int, float))
    assert fresh["signals_count"] == 1 and fresh["last_signal_at"] == "2026-09-10"
    assert items["Quiet Co"]["signals_count"] == 0 and items["Quiet Co"]["last_signal_at"] is None
    assert items["Old Co"]["last_signal_at"] is not None  # from published_at when no event date
    assert items["Soft Vendor"]["flags"] == ["IT or software vendor"]
    assert fresh["flags"] == []


async def test_multi_value_filters_and_secondary_sort(org_id, client):
    service_id = await ensure_service(org_id)
    # no signals: priority 0 for all; fit decides, then the name
    await seed_lead(org_id, service_id=service_id, name="Bravo", employees=2000, signals=())
    await seed_lead(org_id, service_id=service_id, name="alpha", employees=2000, signals=())
    await seed_lead(org_id, service_id=service_id, name="Zulu", employees=20000, signals=())
    await seed_lead(org_id, service_id=service_id, name="Xenon", country="US", signals=())
    h = headers(org_id, "sales")
    base = {"service_id": str(service_id)}

    names = [
        i["company"]["name"]
        for i in (await client.get("/api/v1/leads", params={**base, "country": "DE"}, headers=h)).json()[
            "items"
        ]
    ]
    assert names == ["Zulu", "alpha", "Bravo"]

    both = await client.get(
        "/api/v1/leads", params=[*base.items(), ("country", "de"), ("country", "US")], headers=h
    )
    assert both.json()["total"] == 4
    comma = await client.get(
        "/api/v1/leads", params={**base, "country": "DE,US", "tier": "cold,warm"}, headers=h
    )
    assert comma.json()["total"] == 4
    hot_only = await client.get("/api/v1/leads", params={**base, "tier": "hot"}, headers=h)
    assert hot_only.json()["total"] == 0
    industries = await client.get(
        "/api/v1/leads", params=[*base.items(), ("industry", "banking"), ("industry", "logistics")], headers=h
    )
    assert industries.json()["total"] == 4

    by_name = await client.get("/api/v1/leads", params={**base, "sort": "name:asc"}, headers=h)
    assert [i["company"]["name"] for i in by_name.json()["items"]] == ["alpha", "Bravo", "Xenon", "Zulu"]


async def test_csv_export_respects_list_filters(org_id, client):
    ids = await seed_three(org_id)
    h = headers(org_id, "sales")
    res = await client.get(
        "/api/v1/leads/export.csv",
        params=[
            ("service_id", str(ids["service"])),
            ("country", "DE"),
            ("country", "FR"),
            ("has_new", "true"),
        ],
        headers=h,
    )
    assert res.status_code == 200, res.text
    rows = list(csv.DictReader(io.StringIO(res.text)))
    assert [r["Company Name"] for r in rows] == ["Fresh Co"]
    assert rows[0]["Last Signal"] == "2026-09-10" and rows[0]["Signals"] == "1"

    q = await client.get(
        "/api/v1/leads/export.csv", params={"service_id": str(ids["service"]), "q": "old"}, headers=h
    )
    assert [r["Company Name"] for r in csv.DictReader(io.StringIO(q.text))] == ["Old Co"]


async def test_card_defaults_to_best_service_and_filters_history(org_id, client):
    ia = await ensure_service(org_id, "intelligent_automation")
    cyber = await ensure_service(org_id, "cybersecurity")
    seeded = await seed_lead(org_id, service_id=ia)  # IA: one strong signal
    # the same company scored for cybersecurity without signals (lower priority), twice for history
    from leadradar_core.db.session import async_session_factory
    from leadradar_core.modules.intelligence.service import rescore_company

    for _ in range(2):
        async with async_session_factory() as session, session.begin():
            await rescore_company(session, org_id, seeded.company_id, cyber)

    h = headers(org_id, "sales")
    card = (await client.get(f"/api/v1/leads/{seeded.company_id}", headers=h)).json()
    assert card["service"]["id"] == str(ia)
    assert len(card["history"]) == 1 and card["history"][0]["is_current"] is True

    cyber_card = (
        await client.get(f"/api/v1/leads/{seeded.company_id}", params={"service_id": str(cyber)}, headers=h)
    ).json()
    assert cyber_card["service"]["id"] == str(cyber) and len(cyber_card["history"]) == 2
    assert cyber_card["signals_by_question"] == []
    missing = await client.get(
        f"/api/v1/leads/{seeded.company_id}", params={"service_id": str(uuid4())}, headers=h
    )
    assert missing.status_code == 404


async def test_card_has_feedback_gaps_profile_version_and_unanswered_questions(org_id, client):
    seeded = await seed_lead(org_id, industries=())
    user = uuid4()
    h = headers(org_id, "sales", user)
    sig_id = seeded.signal_ids[0]
    assert (
        await client.post(f"/api/v1/signals/{sig_id}/feedback", headers=h, json={"verdict": "correct"})
    ).is_success
    lead_vote = {"verdict": "good_fit", "service_id": str(seeded.service_id)}
    assert (
        await client.post(f"/api/v1/leads/{seeded.company_id}/feedback", headers=h, json=lead_vote)
    ).is_success

    card = (await client.get(f"/api/v1/leads/{seeded.company_id}", headers=h)).json()
    score = card["score"]
    assert score["scoring_profile_version"] == 1
    assert isinstance(score["data_gaps"], list) and "industry_ids" in score["data_gaps"]
    assert isinstance(score["priority"], (int, float))
    assert card["my_feedback"] == "good_fit"
    [group] = card["signals_by_question"]
    assert group["question"]["key"] == "ia_ai_projects"
    assert group["signals"][0]["my_feedback"] == "correct"
    assert isinstance(group["signals"][0]["confidence"], float)
    unanswered = {q["key"] for q in card["questions_without_evidence"]}
    assert "ia_cost" in unanswered and "ia_ai_projects" not in unanswered

    # another user sees no votes
    other = (await client.get(f"/api/v1/leads/{seeded.company_id}", headers=headers(org_id, "sales"))).json()
    assert other["my_feedback"] is None
    assert other["signals_by_question"][0]["signals"][0]["my_feedback"] is None
