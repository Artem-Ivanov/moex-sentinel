import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from moex_sentinel.composition import build_application_usecases
from moex_sentinel.services.trading_sessions import TradingSessionService
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.automations import AutomationRepository
from sentinel_contracts.broker_execution import BrokerConnection, BrokerTradingStatus
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model


class Automations:
    def list_active(self):
        return [
            SimpleNamespace(broker_id="b1", instrument_id="i1"),
            SimpleNamespace(broker_id="b1", instrument_id="i2"),
        ]


class Connections:
    def connection(self, broker_id: str):
        return BrokerConnection(broker_id, "TINVEST_SANDBOX", "sandbox", "test", True)


class Adapter:
    async def get_trading_status(self, instrument_id: str):
        opened = instrument_id == "i1"
        return BrokerTradingStatus("NORMAL" if opened else "CLOSED", opened, opened, True)


class Catalog:
    def get(self, user_broker_id: str, instrument_id: str):
        return SimpleNamespace(external_instrument_id=instrument_id)


def test_session_status_reuses_success_until_ttl_expires(monkeypatch) -> None:
    clock = [10.0]
    calls = []

    class RecordingAdapter(Adapter):
        async def get_trading_status(self, instrument_id):
            calls.append(instrument_id)
            return await super().get_trading_status(instrument_id)

    monkeypatch.setattr("moex_sentinel.services.trading_sessions.monotonic", lambda: clock[0], raising=False)
    service = TradingSessionService(Automations(), Connections(), lambda _: RecordingAdapter(), Catalog())

    async def run():
        initial = await service.status()
        clock[0] = 69.999
        assert await service.status() == initial
        assert calls == ["i1", "i2"]
        clock[0] = 70
        await service.status()
        assert calls == ["i1", "i2", "i1", "i2"]

    asyncio.run(run())


@pytest.mark.parametrize("change", ["scope", "connection", "catalog"])
def test_session_status_invalidates_changed_scope_or_configuration(change) -> None:
    automations = Automations()
    targets = automations.list_active()
    automations.list_active = lambda: targets
    connections = Connections()
    catalog = Catalog()
    calls = []

    class RecordingAdapter(Adapter):
        async def get_trading_status(self, instrument_id):
            calls.append(instrument_id)
            return await super().get_trading_status(instrument_id)

    service = TradingSessionService(automations, connections, lambda _: RecordingAdapter(), catalog)

    async def run():
        await service.status()
        if change == "scope":
            targets.pop()
        elif change == "connection":
            connections.connection = lambda broker_id: BrokerConnection(
                broker_id, "TINVEST_SANDBOX", "sandbox", "rotated", True
            )
        else:
            catalog.get = lambda *_: SimpleNamespace(external_instrument_id="replacement")
        await service.status()
        assert len(calls) == 2 + len(targets)
        targets.clear()
        assert (await service.status()).status == "NO_ACTIVE"

    asyncio.run(run())


def test_session_status_retries_partial_failure_without_caching_open_aggregate() -> None:
    calls = []

    class RecoveringAdapter(Adapter):
        async def get_trading_status(self, instrument_id):
            calls.append(instrument_id)
            if instrument_id == "i2" and len(calls) == 2:
                raise TimeoutError("Temporary broker failure")
            return await super().get_trading_status(instrument_id)

    service = TradingSessionService(Automations(), Connections(), lambda _: RecoveringAdapter(), Catalog())

    async def run():
        initial = await service.status()
        assert initial.status == "OPEN"
        assert initial.unavailable == 1
        assert (await service.status()).unavailable == 0
        assert len(calls) == 4

    asyncio.run(run())


@pytest.mark.parametrize("change_scope", [False, True])
def test_queued_session_status_coalesces_reads_but_rechecks_scope(change_scope) -> None:
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        targets = Automations().list_active()
        automations = SimpleNamespace(list_active=lambda: targets)
        calls = []

        class PendingAdapter(Adapter):
            async def get_trading_status(self, instrument_id):
                calls.append(instrument_id)
                started.set()
                await release.wait()
                return await super().get_trading_status(instrument_id)

        service = TradingSessionService(automations, Connections(), lambda _: PendingAdapter(), Catalog())
        first = asyncio.create_task(service.status())
        await started.wait()
        queued = asyncio.create_task(service.status())
        await asyncio.sleep(0)
        if change_scope:
            targets.pop()
        release.set()
        initial, result = await asyncio.gather(first, queued)
        assert initial.total == 2
        assert result.total == len(targets)
        assert len(calls) == (3 if change_scope else 2)

    asyncio.run(run())


def test_aggregates_live_status_for_active_instruments() -> None:
    service = TradingSessionService(Automations(), Connections(), lambda _connection: Adapter(), Catalog())

    result = asyncio.run(service.status())

    assert result.status == "OPEN"
    assert (result.total, result.open, result.closed, result.unavailable) == (2, 1, 1, 0)


def test_missing_catalog_entry_is_unavailable_without_hiding_open_instrument() -> None:
    class IncompleteCatalog(Catalog):
        def get(self, user_broker_id: str, instrument_id: str):
            if instrument_id == "i2":
                raise LookupError("Not in broker scope")
            return super().get(user_broker_id, instrument_id)

    service = TradingSessionService(Automations(), Connections(), lambda _connection: Adapter(), IncompleteCatalog())

    result = asyncio.run(service.status())

    assert result.status == "OPEN"
    assert (result.total, result.open, result.closed, result.unavailable) == (2, 1, 0, 1)


def test_broker_failure_is_unavailable() -> None:
    class UnavailableAdapter:
        async def get_trading_status(self, instrument_id: str):
            raise TimeoutError("Broker unavailable")

    service = TradingSessionService(Automations(), Connections(), lambda _connection: UnavailableAdapter(), Catalog())

    result = asyncio.run(service.status())

    assert result.status == "UNAVAILABLE"
    assert (result.total, result.open, result.unavailable) == (2, 0, 2)


def test_composed_session_status_resolves_catalog_id_before_broker_request(monkeypatch) -> None:
    class ExternalIdAdapter:
        async def get_trading_status(self, instrument_id: str):
            if instrument_id != "external-local-instrument":
                raise LookupError("Unknown broker instrument")
            return BrokerTradingStatus("NORMAL", True, True, True)

    engine = create_database_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with Session(engine) as session:
            broker = user_broker_model("broker", "account")
            broker.api_slug = "TINVEST_SANDBOX"
            session.add(broker)
            session.flush()
            session.add(instrument_model("local-instrument", "broker"))
            session.commit()
        AutomationRepository(factory).create(broker_id="broker", account_id="account", instrument_id="local-instrument")
        monkeypatch.setattr(
            "moex_sentinel.composition.TInvestOrderExecutionAdapter", lambda *_args: ExternalIdAdapter()
        )

        result = asyncio.run(build_application_usecases(factory).view_trading_sessions_status.execute())

        assert result.status == "OPEN"
        assert (result.total, result.open, result.unavailable) == (1, 1, 0)
    finally:
        engine.dispose()
