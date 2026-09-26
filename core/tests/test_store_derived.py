"""Derived NIS2/DORA signals (source_type="derived", no document) are persisted by SqlAnalysisStore."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import leadradar_ai as ai
import pytest
from leadradar_core.adapters import mapping
from leadradar_core.adapters.store import SqlAnalysisStore
from leadradar_core.db.session import async_session_factory, engine
from leadradar_core.modules.accounts.models import Company
from leadradar_core.modules.config.presets import create_service_from_preset
from leadradar_core.modules.intelligence.service import load_bundle
from leadradar_core.modules.runs.models import AnalysisRun


@pytest.fixture(autouse=True)
async def fresh_engine() -> AsyncIterator[None]:
    await engine.dispose()
    yield
    await engine.dispose()


async def test_derived_signals_are_saved_without_a_document():
    org_id = uuid4()
    async with async_session_factory() as session, session.begin():
        service, _ = await create_service_from_preset(session, org_id, "cybersecurity")
        company = Company(
            org_id=org_id,
            name="Example Bank",
            domain=f"bank-{uuid4().hex[:6]}.example",
            country_code="DE",
            industry_ids=["banking"],
            employees=5000,
        )
        session.add(company)
        await session.flush()
        run = AnalysisRun(org_id=org_id, kind="analyze", status="running", params={}, progress={})
        session.add(run)
        await session.flush()
        bundle = await load_bundle(session, service)
        profile = mapping.company_profile(company)
        run_id = run.id

    now = datetime.now(UTC)
    derived = ai.derived_signals(profile, bundle, now)
    assert {s.source_name for s in derived} == {"NIS2 scope (firmographics)", "DORA scope (firmographics)"}
    fields = set(ai.VerifiedSignal.model_fields)
    store = SqlAnalysisStore(async_session_factory, org_id)
    await store.save_extraction(
        run_id,
        profile.id,
        bundle.service_id,
        "fp",
        [ai.VerifiedSignal(**s.model_dump(include=fields)) for s in derived],
        [],
    )

    stored = await store.load_signals(profile.id, bundle.service_id)
    assert {s.source_type for s in stored} == {"derived"} and len(stored) == 2
    score = ai.score_company(profile, bundle, stored, now)
    ids = {i for c in score.breakdown for i in c.signal_ids}
    assert ids == {s.id for s in stored}  # breakdown points at the persisted rows
