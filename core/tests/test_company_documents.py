"""CO-14: GET /companies/{id}/documents is paginated ({items, total, page, page_size})."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from leadradar_auth.schemas import Principal
from leadradar_auth.security import create_access_token
from leadradar_core.main import create_app
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.intelligence.models import Document
from leadradar_core.settings import settings
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings.ENV = "test"
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    principal = Principal(
        user_id=uuid4(), org_id=settings.DEFAULT_ORG_ID, email="sales@leadradar.ai", role="sales"
    )
    return {"Authorization": f"Bearer {create_access_token(principal)}"}


def _run_db(fn):
    async def main():
        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(main())


@pytest.fixture
def company_id() -> Iterator[str]:
    domain = f"docs-{uuid4().hex[:8]}.com"
    now = datetime.now(UTC)

    async def seed(session):
        company = Company(org_id=settings.DEFAULT_ORG_ID, name="Docs test", domain=domain)
        session.add(company)
        await session.flush()
        for i in range(5):
            session.add(
                Document(
                    org_id=settings.DEFAULT_ORG_ID,
                    company_id=company.id,
                    source_type="news" if i < 3 else "jobs",
                    source_name="test",
                    url=f"https://{domain}/{i}",
                    canonical_url=f"https://{domain}/{i}",
                    title=f"doc {i}",
                    text="text",
                    content_hash=f"hash-{i}",
                    fetched_at=now - timedelta(minutes=i),
                )
            )
        await session.commit()
        return str(company.id)

    async def cleanup(session):
        await session.execute(delete(Company).where(Company.domain == domain))
        await session.commit()

    cid = _run_db(seed)
    yield cid
    _run_db(cleanup)


def test_documents_paginated(client: TestClient, auth_headers: dict[str, str], company_id: str) -> None:
    res = client.get(
        f"/api/v1/companies/{company_id}/documents", params={"page_size": 2}, headers=auth_headers
    )
    assert res.status_code == 200
    data = res.json()
    assert (data["total"], data["page"], data["page_size"]) == (5, 1, 2)
    assert [d["title"] for d in data["items"]] == ["doc 0", "doc 1"]  # newest first

    page3 = client.get(
        f"/api/v1/companies/{company_id}/documents", params={"page": 3, "page_size": 2}, headers=auth_headers
    ).json()
    assert [d["title"] for d in page3["items"]] == ["doc 4"]

    news = client.get(
        f"/api/v1/companies/{company_id}/documents", params={"source_type": "news"}, headers=auth_headers
    ).json()
    assert news["total"] == 3 and len(news["items"]) == 3


def test_documents_pagination_validation(
    client: TestClient, auth_headers: dict[str, str], company_id: str
) -> None:
    res = client.get(
        f"/api/v1/companies/{company_id}/documents", params={"page_size": 101}, headers=auth_headers
    )
    assert res.status_code == 422
    res = client.get(f"/api/v1/companies/{uuid4()}/documents", headers=auth_headers)
    assert res.status_code == 404
