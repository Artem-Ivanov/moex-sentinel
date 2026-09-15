import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from sentinel_contracts.broker_execution import BrokerPosition, OrderBookLevel
from sentinel_contracts.streaming_market import (
    InstrumentMarketState,
    MarketBatchSnapshot,
    StreamOrderBook,
    StreamTradingStatus,
)
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from tests.trading_automaton.services.test_streaming_runtime_coordinator_service import bootstrap_command
from trading_automaton.services.broker_tick_preparation import BrokerTickPreparationService
from trading_automaton.services.position_bootstrap import PositionBootstrapService
from trading_automaton.services.position_state_hydration import PositionStateCacheService, PositionStateHydrationService
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base, FactOutboxModel, LocalIntentModel, TradeLotModel
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def command() -> AutomationCommand:
    return baseline_command()


class Broker:
    def __init__(self) -> None:
        self.position_calls = 0
        self.free_cash_calls = 0

    async def get_positions(self, account_id):
        self.position_calls += 1
        return ()

    async def get_free_cash(self, account_id, currency):
        self.free_cash_calls += 1
        return Decimal("5000")


class Cash:
    def __init__(self) -> None:
        self.values = {}

    async def replace_snapshot(self, account_id, currency, free_cash):
        self.values[(account_id, currency)] = free_cash


class Portfolio:
    def __init__(self) -> None:
        self.positions = {}

    async def replace_snapshot(self, account_id, positions):
        self.positions.update({(account_id, item.instrument_id): item for item in positions})

    async def position(self, account_id, instrument_id):
        return self.positions.get((account_id, instrument_id))

    async def apply_position_event(self, account_id, position):
        self.positions[(account_id, position.instrument_id)] = position


class Bootstrap:
    def __init__(self) -> None:
        self.calls = []

    async def bootstrap(self, instrument_id):
        self.calls.append(instrument_id)


class Commissions:
    def __init__(self) -> None:
        self.calls = []

    async def refresh_if_due(self, request, provider, *, now):
        self.calls.append(request)


class Hydration:
    def __init__(self) -> None:
        self.calls = []

    async def hydrate(self, commands):
        self.calls.append(commands)


class Reconciliation:
    def __init__(self) -> None:
        self.calls = []

    async def reconcile(self, commands):
        self.calls.append(commands)


def test_prepares_zero_position_and_external_caches_before_sla_snapshot() -> None:
    async def scenario():
        broker = Broker()
        portfolio = Portfolio()
        bootstrap = Bootstrap()
        commissions = Commissions()
        hydration = Hydration()
        reconciliation = Reconciliation()
        cash = Cash()
        service = BrokerTickPreparationService(
            broker,
            portfolio,
            bootstrap,
            commissions,
            hydration,
            reconciliation=reconciliation,
            cash=cash,
            now=lambda: NOW,
        )
        snapshot = MarketBatchSnapshot.immutable(
            "preliminary",
            NOW,
            {
                "instrument": InstrumentMarketState(
                    "instrument",
                    order_book=StreamOrderBook(
                        "instrument",
                        (OrderBookLevel(Decimal("100"), 1),),
                        (OrderBookLevel(Decimal("100.1"), 1),),
                        NOW,
                        True,
                    ),
                )
            },
        )
        await service.prepare((command(),), snapshot)
        await service.prepare((command(),), snapshot)
        return broker, portfolio, bootstrap, commissions, hydration, cash, reconciliation

    broker, portfolio, bootstrap, commissions, hydration, cash, reconciliation = asyncio.run(scenario())

    assert broker.position_calls == 2
    assert portfolio.positions[("account", "instrument")].quantity_lots == 0
    assert bootstrap.calls == ["instrument", "instrument"]
    assert len(commissions.calls) == 2
    assert commissions.calls[0].price == Decimal("100.1")
    assert len(hydration.calls) == 2
    assert broker.free_cash_calls == 2
    assert cash.values[("account", "RUB")] == Decimal("5000")
    assert reconciliation.calls == [(command(),), (command(),)]


def test_refreshes_one_portfolio_per_unique_account_in_each_cycle() -> None:
    async def scenario():
        broker = Broker()
        service = BrokerTickPreparationService(
            broker,
            Portfolio(),
            Bootstrap(),
            Commissions(),
            Hydration(),
            now=lambda: NOW,
        )
        second = baseline_command(automation="automation-2", instrument="instrument-2")
        snapshot = MarketBatchSnapshot.immutable(
            "preliminary",
            NOW,
            {
                instrument_id: InstrumentMarketState(
                    instrument_id,
                    order_book=StreamOrderBook(
                        instrument_id,
                        (OrderBookLevel(Decimal("100"), 1),),
                        (OrderBookLevel(Decimal("100.1"), 1),),
                        NOW,
                        True,
                    ),
                )
                for instrument_id in ("instrument", "instrument-2")
            },
        )

        await service.prepare((command(), second), snapshot)
        await service.prepare((command(), second), snapshot)
        return broker

    broker = asyncio.run(scenario())

    assert broker.position_calls == 2


class AdoptedBroker(Broker):
    async def get_positions(self, account_id):
        self.position_calls += 1
        return (BrokerPosition("instrument", Decimal(2), Decimal(100), Decimal(101), "RUB"),)


class ReadyCommissions(Commissions):
    def schedule(self, key, *, snapshot_at):
        return SimpleNamespace(buy_rate=Decimal("0.001"), sell_rate=Decimal("0.001"))


class PositionBootstrap:
    def __init__(self, *, invalid=False):
        self.calls = []
        self.invalid = invalid

    def ensure(self, command, settings):
        if self.invalid:
            raise ValueError("Invalid recovery ledger")
        self.calls.append(command)


def bootstrap_market(**changes):
    state = InstrumentMarketState(
        "instrument",
        order_book=StreamOrderBook(
            "instrument",
            (OrderBookLevel(Decimal(100), 10),),
            (OrderBookLevel(Decimal(101), 10),),
            NOW,
            True,
        ),
        trading_status=StreamTradingStatus("instrument", "NORMAL_TRADING", True, True, NOW),
    ).model_copy(update=changes)
    return MarketBatchSnapshot.immutable("bootstrap", NOW, {"instrument": state})


def test_bootstrap_preparation_keeps_order_recovery_and_allows_regular_commands() -> None:
    async def scenario():
        bootstrap = PositionBootstrap()
        hydration = Hydration()
        reconciliation = Reconciliation()
        service = BrokerTickPreparationService(
            AdoptedBroker(),
            Portfolio(),
            Bootstrap(),
            ReadyCommissions(),
            hydration,
            reconciliation=reconciliation,
            position_bootstrap=bootstrap,
            now=lambda: NOW,
        )
        regular = baseline_command(automation="regular", instrument="regular")
        await service.prepare((bootstrap_command(), regular), bootstrap_market())
        await service.prepare((bootstrap_command(), regular), bootstrap_market())
        return bootstrap, hydration, reconciliation, regular

    bootstrap, hydration, reconciliation, regular = asyncio.run(scenario())
    assert bootstrap.calls == [bootstrap_command(), bootstrap_command()]
    assert hydration.calls == [(regular,), (regular,)]
    assert reconciliation.calls == [(regular,), (bootstrap_command(),), (regular,), (bootstrap_command(),)]


@pytest.mark.parametrize(
    "failure",
    [
        "missing_market",
        "missing_book",
        "stale_book",
        "expired_envelope",
        "crossed_book",
        "nonfinite_book",
        "missing_status",
        "closed_status",
        "portfolio",
        "commission",
        "unknown_commission",
        "recovery",
    ],
)
def test_bootstrap_requires_matching_portfolio_but_not_market_or_commission_readiness(failure) -> None:
    class FailingCommissions(ReadyCommissions):
        async def refresh_if_due(self, request, provider, *, now):
            if failure == "commission":
                raise TInvestAdapterError("COMMISSION_UNAVAILABLE", "Unavailable", retryable=True)
            await super().refresh_if_due(request, provider, now=now)

        def schedule(self, key, *, snapshot_at):
            return None if failure == "unknown_commission" else super().schedule(key, snapshot_at=snapshot_at)

    async def scenario():
        bootstrap = PositionBootstrap(invalid=failure == "recovery")
        hydration = Hydration()
        reconciliation = Reconciliation()
        service = BrokerTickPreparationService(
            Broker() if failure == "portfolio" else AdoptedBroker(),
            Portfolio(),
            Bootstrap(),
            FailingCommissions(),
            hydration,
            reconciliation=reconciliation,
            position_bootstrap=bootstrap,
            now=lambda: NOW,
        )
        snapshot = bootstrap_market()
        state = snapshot.instruments["instrument"]
        if failure == "missing_market":
            snapshot = MarketBatchSnapshot.immutable("missing", NOW, {})
        elif failure == "missing_book":
            snapshot = bootstrap_market(order_book=None)
        elif failure == "stale_book":
            snapshot = bootstrap_market(
                order_book=state.order_book.model_copy(update={"captured_at": NOW - timedelta(seconds=3)})
            )
        elif failure == "expired_envelope":
            snapshot = snapshot.model_copy(update={"expires_at": NOW - timedelta(milliseconds=1)})
        elif failure == "crossed_book":
            snapshot = bootstrap_market(order_book=state.order_book.model_copy(update={"asks": state.order_book.bids}))
        elif failure == "nonfinite_book":
            invalid_level = state.order_book.asks[0].model_copy(update={"price": Decimal("NaN")})
            snapshot = bootstrap_market(order_book=state.order_book.model_copy(update={"asks": (invalid_level,)}))
        elif failure == "missing_status":
            snapshot = bootstrap_market(trading_status=None)
        elif failure == "closed_status":
            snapshot = bootstrap_market(
                trading_status=state.trading_status.model_copy(update={"api_trade_available": False})
            )
        await service.prepare((bootstrap_command(),), snapshot)
        return bootstrap, hydration, reconciliation

    bootstrap, hydration, reconciliation = asyncio.run(scenario())
    assert bootstrap.calls == ([] if failure in {"portfolio", "recovery"} else [bootstrap_command()])
    assert hydration.calls == []
    assert reconciliation.calls == [(bootstrap_command(),)]


@pytest.mark.parametrize("state", [AutomationState.HOLD, AutomationState.IN_QUEUE])
def test_ready_bootstrap_is_atomic_idempotent_and_hydrates_only_after_ack(tmp_path, state) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repository = LocalAutomationRepository(factory, fact_writer=FactOutboxWriter(clock=lambda: NOW))
    value = bootstrap_command().model_copy(update={"state": state})
    repository.cache_command(value)

    class Candles(Bootstrap):
        async def completed(self, instrument_id):
            return ()

    async def scenario():
        portfolio = Portfolio()
        candles = Candles()
        cache = PositionStateCacheService()
        hydration = PositionStateHydrationService(repository, portfolio, candles, cache, now=lambda: NOW)
        service = BrokerTickPreparationService(
            AdoptedBroker(),
            portfolio,
            candles,
            ReadyCommissions(),
            hydration,
            position_bootstrap=PositionBootstrapService(repository),
            now=lambda: NOW,
        )
        await service.prepare((value,), bootstrap_market())
        first = repository.ready_fact_outbox(10, now=NOW, deadline_ms=0)
        await service.prepare((value,), bootstrap_market())
        second = repository.ready_fact_outbox(10, now=NOW, deadline_ms=0)
        assert [row.event_id for row in first] == [row.event_id for row in second]
        assert [row.sequence_number for row in second] == [1, 2, 3, 4]
        assert second[-1].payload["state"] == "IN_WORK"
        active = value.model_copy(update={"state": AutomationState.IN_WORK})
        await service.prepare((active,), bootstrap_market())
        assert await cache.get(str(value.automation_id)) is None
        repository.acknowledge_fact_outbox(str(value.automation_id), accepted_through_sequence=4, current_revision=2)
        await service.prepare((active,), bootstrap_market())
        assert await cache.get(str(value.automation_id)) is not None

    asyncio.run(scenario())
    with factory() as session:
        lots = session.scalars(select(TradeLotModel)).all()
        assert len(lots) == 1
        assert lots[0].source == "BROKER_POSITION_BOOTSTRAP"
        assert session.scalar(select(func.count()).select_from(LocalIntentModel)) == 0
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0
    engine.dispose()


def test_held_adopted_position_still_reconciles_pending_broker_execution() -> None:
    async def scenario():
        reconciliation = Reconciliation()
        service = BrokerTickPreparationService(
            Broker(), Portfolio(), Bootstrap(), Commissions(), Hydration(),
            reconciliation=reconciliation, position_bootstrap=PositionBootstrap(), now=lambda: NOW,
        )
        held = bootstrap_command()
        await service.prepare((held,), MarketBatchSnapshot.immutable("held", NOW, {}))
        return reconciliation, held

    reconciliation, held = asyncio.run(scenario())
    assert reconciliation.calls == [(held,)]
