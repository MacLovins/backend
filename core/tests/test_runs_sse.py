"""Runs API: create (validation, kind, mode, queued), cancel (409 when finished), retry, SSE replay names."""

import json
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from leadradar_auth.schemas import Principal
from leadradar_auth.security import create_access_token
from leadradar_core.db.session import async_session_factory, engine
from leadradar_core.main import create_app
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.runs import service as runs_service
from leadradar_core.modules.runs.models import AnalysisRun
from leadradar_core.settings import settings


def parse_sse(body: str) -> list[dict]:
    """[{event, id, data}] from an SSE body (comments skipped)."""
    out = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        item: dict = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                item["event"] = line[len("event: ") :]
            elif line.startswith("id: "):
                item["id"] = int(line[len("id: ") :])
            elif line.startswith("data: "):
                item["data"] = json.loads(line[len("data: ") :])
        if "event" in item:
            out.append(item)
    return out


@pytest.fixture(autouse=True)
async def fresh_engine() -> AsyncIterator[None]:
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
def client() -> AsyncIterator[TestClient]:
    settings.ENV = "test"
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def org_id() -> UUID:
    return uuid4()


def headers(org_id: UUID) -> dict[str, str]:
    principal = Principal(user_id=org_id, org_id=org_id, email="admin@leadradar.ai", role="admin")
    return {"Authorization": f"Bearer {create_access_token(principal)}"}


async def seed_company(org_id: UUID) -> UUID:
    async with async_session_factory() as session, session.begin():
        company = Company(org_id=org_id, name="Acme", domain=f"acme-{uuid4().hex[:6]}.example")
        session.add(company)
        await session.flush()
        company_id = company.id
    await engine.dispose()
    return company_id


@pytest.fixture
def enqueued(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    async def fake_enqueue(run_id, company_ids, service_ids, mode):
        calls.append((run_id, company_ids, service_ids, mode))
        return len(company_ids)

    monkeypatch.setattr(runs_service, "enqueue_analysis", fake_enqueue)
    return calls


async def test_runs_lifecycle_and_sse(client: TestClient, org_id: UUID, enqueued) -> None:
    company_id = await seed_company(org_id)
    h = headers(org_id)
    create_res = client.post("/api/v1/runs", json={"company_ids": [str(company_id)]}, headers=h)
    assert create_res.status_code == 201, create_res.text
    run = create_res.json()
    run_id = run["id"]
    assert run["status"] == "queued" and run["kind"] == "analyze"
    assert run["params"]["mode"] == "incremental" and run["progress"]["total"] == 1
    assert enqueued == [(UUID(run_id), [company_id], [], "incremental")]

    assert client.get(f"/api/v1/runs/{run_id}", headers=h).json()["id"] == run_id

    for method in (client.get, client.post):
        sse_res = method(f"/api/v1/runs/{run_id}/events", headers=h)
        assert sse_res.status_code == 200
        assert "text/event-stream" in sse_res.headers.get("content-type", "")
        first = parse_sse(sse_res.text)[0]
        assert first["event"] == "run.progress" and first["data"]["total"] == 1

    cancel_res = client.post(f"/api/v1/runs/{run_id}/cancel", headers=h)
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "cancelled" and cancel_res.json()["finished_at"]

    # a finished run cannot be cancelled again, nor retried once cancelled
    again = client.post(f"/api/v1/runs/{run_id}/cancel", headers=h)
    assert again.status_code == 409 and again.json()["error"]["code"] == "conflict"
    assert client.post(f"/api/v1/runs/{run_id}/retry-failed", headers=h).status_code == 409

    events = parse_sse(client.get(f"/api/v1/runs/{run_id}/events", headers=h).text)
    assert [e["event"] for e in events] == ["run.progress", "run.finished"]
    assert events[-1]["data"]["status"] == "cancelled"
    # reconnect after the last event: only the terminal notification, no replayed rows
    tail = parse_sse(
        client.get(
            f"/api/v1/runs/{run_id}/events", headers={**h, "Last-Event-ID": str(events[-1]["id"])}
        ).text
    )
    assert [e["event"] for e in tail] == ["run.finished"]


@pytest.mark.parametrize("status", ["succeeded", "failed", "partial"])
async def test_cancel_finished_run_is_409(client: TestClient, org_id: UUID, status: str) -> None:
    async with async_session_factory() as session, session.begin():
        run = AnalysisRun(org_id=org_id, kind="analyze", status=status, params={})
        session.add(run)
        await session.flush()
        run_id = run.id
    await engine.dispose()
    res = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers(org_id))
    assert res.status_code == 409
    assert res.json()["error"]["details"]["status"] == status


async def test_create_run_validation(client: TestClient, org_id: UUID, enqueued) -> None:
    company_id = await seed_company(org_id)
    h = headers(org_id)
    bad = [
        {"company_ids": []},  # would never finish
        {},
        {"company_ids": [str(company_id)], "mode": "turbo"},
        {"company_ids": [str(company_id)], "kind": "whatever"},
        {"company_ids": [str(uuid4())]},  # unknown company
        {"company_ids": [str(company_id)], "service_ids": [str(uuid4())]},  # unknown service
    ]
    for body in bad:
        res = client.post("/api/v1/runs", json=body, headers=h)
        assert res.status_code == 422, (body, res.text)
    assert enqueued == []


async def test_create_run_honours_kind_and_mode(client: TestClient, org_id: UUID, enqueued) -> None:
    company_id = await seed_company(org_id)
    res = client.post(
        "/api/v1/runs",
        json={"kind": "refresh", "mode": "full", "company_ids": [str(company_id), str(company_id)]},
        headers=headers(org_id),
    )
    assert res.status_code == 201, res.text
    run = res.json()
    assert (run["kind"], run["params"]["mode"], run["progress"]["total"]) == ("refresh", "full", 1)
    assert enqueued[0][1:] == ([company_id], [], "full")


def test_unknown_run_is_404(client: TestClient, org_id: UUID) -> None:
    h = headers(org_id)
    assert client.get(f"/api/v1/runs/{uuid4()}", headers=h).status_code == 404
    assert client.post(f"/api/v1/runs/{uuid4()}/cancel", headers=h).status_code == 404
    assert client.get(f"/api/v1/runs/{uuid4()}/events", headers=h).status_code == 404
