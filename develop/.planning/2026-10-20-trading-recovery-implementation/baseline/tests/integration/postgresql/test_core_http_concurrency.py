"""Concurrent real Core HTTP ingress on migrated disposable PostgreSQL."""

import asyncio
import threading
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.api import app as app_module
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import AutomationEventModel, TradingAutomationModel
from tests.services.test_trading_fact_ingress import (
    AUTOMATION_A,
    AUTOMATION_B,
    INSTRUMENT_A,
    INSTRUMENT_B,
    SCOPE_ID,
    state_fact,
)
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model

pytestmark = pytest.mark.postgresql


@pytest.fixture
def core_http(isolated_postgresql_database_url, monkeypatch):
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(user_broker_model(str(SCOPE_ID), "synthetic-account"))
        session.flush()
        for instrument_id, ticker in ((INSTRUMENT_A, "SYNTH_A"), (INSTRUMENT_B, "SYNTH_B")):
            instrument = instrument_model(str(instrument_id), str(SCOPE_ID))
            instrument.ticker = ticker
            session.add(instrument)
        session.flush()
        for automation_id, instrument_id in ((AUTOMATION_A, INSTRUMENT_A), (AUTOMATION_B, INSTRUMENT_B)):
            session.add(
                automation_model(str(automation_id), user_broker_id=str(SCOPE_ID), instrument_id=str(instrument_id))
            )
    observed = []
    controls = SimpleNamespace(barrier=None, commit_entered=None, commit_release=None, committed=None)

    class ObservedSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.owner = threading.get_ident()
            observed.append(("create", self.owner))

        def commit(self):
            if controls.commit_entered is not None:
                controls.commit_entered.set()
                assert controls.commit_release.wait(5)
            result = super().commit()
            if controls.committed is not None:
                controls.committed.set()
            return result

        def close(self):
            assert threading.get_ident() == self.owner
            observed.append(("close", threading.get_ident()))
            return super().close()

    @event.listens_for(ObservedSession, "after_begin")
    def transaction_began(session, _transaction, connection):
        assert threading.get_ident() == session.owner
        observed.append(("transaction", threading.get_ident()))
        if controls.barrier is not None:
            controls.barrier.wait(timeout=5)

    monkeypatch.setattr(
        app_module,
        "create_session_factory",
        lambda owned_engine: sessionmaker(owned_engine, class_=ObservedSession, expire_on_commit=False),
    )
    application = app_module.create_app(test_auth_bypass=True, database_url=isolated_postgresql_database_url)
    try:
        yield application, factory, observed, controls
    finally:
        engine.dispose()


async def publish(client, facts):
    response = await client.post(
        "/internal/automation-facts", json={"facts": [fact.model_dump(mode="json") for fact in facts]}
    )
    assert response.status_code == 200
    return response.json()


def persisted(factory):
    with factory() as session:
        rows = session.scalars(select(TradingAutomationModel).order_by(TradingAutomationModel.id)).all()
        states = {row.id: (row.revision, row.last_sequence_number) for row in rows}
        return states, session.scalar(select(func.count()).select_from(AutomationEventModel))


@pytest.mark.parametrize("same_envelope", [True, False])
def test_concurrent_same_automation_exact_duplicate_or_conflicting_cas(core_http, same_envelope):
    application, factory, observed, controls = core_http
    first = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000701"))
    second = (
        first if same_envelope else first.model_copy(update={"event_id": UUID("00000000-0000-4000-8000-000000000702")})
    )

    async def scenario():
        loop_thread = threading.get_ident()
        async with (
            application.router.lifespan_context(application),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client,
        ):
            controls.barrier = threading.Barrier(2)
            results = await asyncio.gather(publish(client, [first]), publish(client, [second]))
            controls.barrier = None
            if same_envelope:
                assert results[0] == results[1]
                assert results[0]["failures"] == []
                assert await publish(client, [first]) == results[0]
            else:
                assert sum(bool(result["results"]) for result in results) == 1
                failures = [failure for result in results for failure in result["failures"]]
                assert len(failures) == 1
                assert failures[0]["code"] == "AUTOMATION_SEQUENCE_CONFLICT"
        assert observed
        assert all(thread != loop_thread for _stage, thread in observed)
        assert sum(stage == "create" for stage, _thread in observed) == sum(
            stage == "close" for stage, _thread in observed
        )
        states, events = persisted(factory)
        assert states[str(AUTOMATION_A)] == (2, 1)
        assert events == 1

    asyncio.run(scenario())


def test_independent_automations_overlap_real_transactions(core_http):
    application, factory, observed, controls = core_http
    facts = [
        state_fact(automation, event_id=UUID(f"00000000-0000-4000-8000-{index:012d}"))
        for index, automation in ((711, AUTOMATION_A), (712, AUTOMATION_B))
    ]

    async def scenario():
        async with (
            application.router.lifespan_context(application),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client,
        ):
            controls.barrier = threading.Barrier(2)
            results = await asyncio.gather(*(publish(client, [fact]) for fact in facts))
            controls.barrier = None
            assert all(result["failures"] == [] for result in results)
        assert len({thread for stage, thread in observed if stage == "transaction"}) == 2
        states, events = persisted(factory)
        assert set(states.values()) == {(2, 1)}
        assert events == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("lost_response", ["transport", "abandoned"])
def test_invalid_later_fact_rolls_back_and_lost_response_replays_exactly(core_http, lost_response):
    application, factory, _observed, controls = core_http
    first = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000721"))
    invalid = first.model_copy(update={"event_id": UUID("00000000-0000-4000-8000-000000000722"), "sequence_number": 3})

    class LoseResponse(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.ASGITransport(app=application)
            self.armed = False

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if self.armed:
                self.armed = False
                raise httpx.ReadError("synthetic committed response loss")
            return response

    async def scenario():
        async with application.router.lifespan_context(application):
            transport = LoseResponse()
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                rejected = await publish(client, [first, invalid])
                assert rejected["results"] == []
                assert rejected["failures"][0]["code"] == "AUTOMATION_SEQUENCE_GAP"
                assert persisted(factory)[1] == 0
                assert persisted(factory)[0][str(AUTOMATION_A)] == (1, 0)
                if lost_response == "transport":
                    transport.armed = True
                    with pytest.raises(httpx.ReadError, match="synthetic"):
                        await publish(client, [first])
                else:
                    controls.commit_entered, controls.commit_release, controls.committed = (
                        threading.Event() for _ in range(3)
                    )
                    request = asyncio.create_task(publish(client, [first]))
                    try:
                        assert await asyncio.to_thread(controls.commit_entered.wait, 5)
                        request.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await request
                    finally:
                        controls.commit_release.set()
                    assert await asyncio.to_thread(controls.committed.wait, 5)
                    controls.commit_entered = None
                replay = await publish(client, [first])
                assert replay["failures"] == []
                assert await publish(client, [first]) == replay
                states, events = persisted(factory)
                assert states[str(AUTOMATION_A)] == (2, 1)
                assert events == 1

    asyncio.run(scenario())
