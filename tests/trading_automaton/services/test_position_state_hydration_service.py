import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sentinel_contracts.analytics import MarketIndicators
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


class PreparedMetrics:
    def __init__(self, at=NOW):
        self.at = at

    async def get(self, instrument_id):
        return MarketIndicators(Decimal("0.5"), Decimal("0.5"), "ANALYTICS", None, None, None, self.at)


class Repository:
    pending = False

    def intent_history(self, automation_id):
        return IntentHistory(Decimal("100"), 0, Decimal(), 2, Decimal())

    def list_open_lots(self, automation_id):
        return []

    def get_cycle_state(self, automation_id, *, now):
        return TradingCycleState(automation_id, None, None, True, None, now)

    def get_position_cycle_id(self, automation_id):
        return None

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
            cache,
            now=lambda: NOW,
            prepared_metrics=PreparedMetrics(),
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
            cache,
            consistency=InconsistentPositions(),
            now=lambda: NOW,
            prepared_metrics=PreparedMetrics(),
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
            cache,
            now=lambda: NOW,
            prepared_metrics=PreparedMetrics(),
        )
        await service.hydrate((command(),))
        return await cache.get(str(command().automation_id))

    assert asyncio.run(scenario()) is None


def test_worker_hydration_and_planner_import_without_analytics_implementation():

    code = """
import builtins
original = builtins.__import__
def isolated(name, *args, **kwargs):
    if name == "market_analytics" or name.startswith("market_analytics."):
        raise AssertionError("Worker imported Analytics implementation")
    return original(name, *args, **kwargs)
builtins.__import__ = isolated
import trading_automaton.services.position_state_hydration
import trading_automaton.services.runtime_decision_planner
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
    )
    assert result.returncode == 0, result.stderr
