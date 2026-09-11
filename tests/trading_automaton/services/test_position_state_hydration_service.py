import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from moex_sentinel.domain.market_data import HistoricCandle
from sentinel_contracts.broker_execution import BrokerPosition
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.services.position_state_hydration import (
    PositionStateCacheService,
    PositionStateHydrationService,
)
from trading_automaton.storage.repository import IntentHistory, TradingCycleState

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def command() -> AutomationCommand:
    return baseline_command()


class Portfolio:
    async def position(self, account_id, instrument_id):
        return BrokerPosition(instrument_id, Decimal("2"), Decimal("100"), Decimal("101"), "RUB")


class Candles:
    async def completed(self, instrument_id):
        return (
            HistoricCandle(
                instrument_id,
                Decimal("100"),
                Decimal("102"),
                Decimal("99"),
                Decimal("101"),
                10,
                NOW,
                True,
            ),
        )


class Repository:
    pending = False

    def intent_history(self, automation_id):
        return IntentHistory(Decimal("100"), 0, Decimal(), 2, Decimal())

    def list_open_lots(self, automation_id):
        return []

    def get_cycle_state(self, automation_id, *, now):
        return TradingCycleState(automation_id, None, None, True, None, now)

    def get_active_intent(self, automation_id):
        return object()

    def realized_pnl(self, automation_id):
        return Decimal("3")

    def has_pending_fact_outbox(self, automation_id):
        return self.pending


class InconsistentPositions:
    def reconcile(self, **values):
        return type("Result", (), {"consistent": False, "lots": (), "reason_code": "MISMATCH"})()


def test_hydrates_all_durable_and_market_state_before_snapshot() -> None:
    async def scenario():
        cache = PositionStateCacheService()
        service = PositionStateHydrationService(
            Repository(),
            Portfolio(),
            Candles(),
            cache,
            now=lambda: NOW,
        )
        await service.hydrate((command(),))
        return await cache.get(str(command().automation_id))

    result = asyncio.run(scenario())

    assert result is not None
    assert result.position.quantity_lots == Decimal("2")
    assert result.history.total_bought_lots == 2
    assert result.indicators.last_candle_at == NOW
    assert result.has_active_intent is True


def test_excludes_position_from_hot_state_when_consistency_check_fails() -> None:
    async def scenario():
        cache = PositionStateCacheService()
        service = PositionStateHydrationService(
            Repository(),
            Portfolio(),
            Candles(),
            cache,
            consistency=InconsistentPositions(),
            now=lambda: NOW,
        )
        await service.hydrate((command(),))
        return await cache.get(str(command().automation_id))

    assert asyncio.run(scenario()) is None


def test_excludes_position_until_previous_core_snapshot_is_acknowledged() -> None:
    async def scenario():
        cache = PositionStateCacheService()
        repository = Repository()
        repository.pending = True
        service = PositionStateHydrationService(
            repository,
            Portfolio(),
            Candles(),
            cache,
            now=lambda: NOW,
        )
        await service.hydrate((command(),))
        return await cache.get(str(command().automation_id))

    assert asyncio.run(scenario()) is None
