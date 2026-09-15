import asyncio
from types import SimpleNamespace

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
