"""Regression matrix for the environment-owned trading strategy."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel_contracts.trading import DecisionKind
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import MarketIndicators
from trading_automaton.domain.storage_dtos import TradeLotRecord
from trading_automaton.services.account_commission_profile import CommissionSchedule
from trading_automaton.services.decision import DecisionContext, TradeDecisionService
from trading_automaton.storage.repository import TradingCycleState

NOW = datetime(2026, 8, 11, 10, tzinfo=UTC)


def _base_cycle() -> TradingCycleState:
    return TradingCycleState(
        automation_id="a",
        pending_low=Decimal("99"),
        last_buy_candle_at=None,
        sell_armed=True,
        last_sell_price=Decimal("100"),
        updated_at=NOW,
    )


def _base_indicators() -> MarketIndicators:
    return MarketIndicators(
        averaging_step_percent=Decimal("0.5"),
        minimum_net_profit_percent=Decimal("0.5"),
        source="ADAPTIVE",
        mean_5=Decimal("100"),
        mean_20=Decimal("100"),
        change_10_percent=Decimal("0"),
        last_candle_at=NOW,
        last_candle_open=Decimal("100"),
        last_candle_close=Decimal("99"),
        range_low=Decimal("90"),
        range_high=Decimal("110"),
    )


def _base_context(**changes: object) -> DecisionContext:
    values = {
        "core_available": True,
        "has_active_intent": False,
        "quantity_lots": 4,
        "lot_size": 10,
        "average_price": Decimal("100"),
        "current_price": Decimal("99.3"),
        "best_bid": Decimal("99.3"),
        "best_ask": Decimal("99.4"),
        "last_buy_price": Decimal("100"),
        "invested_amount": Decimal("400"),
        "commission_schedule": CommissionSchedule(Decimal("0.001"), Decimal("0.001")),
        "buy_commission_per_lot": Decimal("0"),
        "averaging_step_percent": Decimal("0.5"),
        "minimum_net_profit_percent": Decimal("0.5"),
        "completed_partial_sell_steps": 0,
        "settings": StrategySettings(),
        "currency": "RUB",
        "min_price_increment": Decimal("0.01"),
        "lots": (),
        "cycle": _base_cycle(),
        "indicators": _base_indicators(),
        "free_cash": Decimal("10000"),
        "reserved_cash": Decimal(),
        "available_free_cash": Decimal("10000"),
        "required_order_cash": Decimal(),
    }
    values.update(changes)
    return DecisionContext(**values)


def _flat_context(**changes: object) -> DecisionContext:
    return _base_context(
        quantity_lots=0,
        average_price=Decimal(),
        invested_amount=Decimal(),
        **changes,
    )


def _buy_more_context(**changes: object) -> DecisionContext:
    values = {
        "current_price": Decimal("100.0"),
        "best_ask": Decimal("100.0"),
        "lots": (
            TradeLotRecord(
                "lot-1",
                "a",
                "i",
                "EXECUTED",
                1,
                1,
                Decimal("99"),
                Decimal("0.1"),
                NOW,
                None,
            ),
        ),
    }
    values["best_bid"] = Decimal("100.0")
    values.update(changes)
    return _base_context(**values)


@pytest.mark.parametrize(
    (
        "case_id",
        "context_factory",
        "expected_kind",
        "expected_reason",
    ),
    [
        (
            "S-01",
            lambda: _base_context(core_available=False),
            DecisionKind.WAIT,
            "CORE_UNAVAILABLE",
        ),
        (
            "S-02",
            lambda: _base_context(has_active_intent=True),
            DecisionKind.WAIT,
            "ACTIVE_INTENT",
        ),
        (
            "S-03",
            lambda: _base_context(quantity_lots=0, cycle=None, indicators=None),
            DecisionKind.WAIT,
            "BUY_WINDOW_UNCONFIRMED",
        ),
        (
            "S-04",
            lambda: _flat_context(cycle=_base_cycle().model_copy(update={"sell_armed": False})),
            DecisionKind.WAIT,
            "ENTRY_PULLBACK_WAIT",
        ),
        (
            "S-05",
            lambda: _flat_context(cycle=_base_cycle(), current_price=Decimal("99.10"), best_ask=Decimal("99.10")),
            DecisionKind.WAIT,
            "ENTRY_REVERSAL_WAIT",
        ),
        (
            "S-06",
            lambda: _flat_context(cycle=_base_cycle(), current_price=Decimal("99.90"), best_ask=Decimal("99.90")),
            DecisionKind.BUY_MORE,
            "ENTRY_REVERSAL_CONFIRMED",
        ),
        (
            "S-07",
            lambda: _base_context(current_price=Decimal("94"), best_bid=Decimal("93.9"), best_ask=Decimal("93.8")),
            DecisionKind.SELL_ALL,
            "STOP_LOSS",
        ),
        (
            "S-08",
            lambda: _base_context(current_price=Decimal("108"), best_bid=Decimal("107.9"), best_ask=Decimal("108.0")),
            DecisionKind.SELL_ALL,
            "TAKE_PROFIT",
        ),
        (
            "S-09",
            lambda: _base_context(cycle=_base_cycle().model_copy(update={"sell_armed": False})),
            DecisionKind.WAIT,
            "SELL_CYCLE_DISARMED",
        ),
        (
            "S-10",
            lambda: _base_context(
                cycle=_base_cycle(),
                settings=StrategySettings(STRATEGY_BUY_ORDER_LOTS=2),
                indicators=_base_indicators(),
            ),
            DecisionKind.BUY_MORE,
            "AVERAGING_LEVEL",
        ),
        (
            "S-11",
            lambda: _base_context(
                invested_amount=Decimal("999999"),
                commission_schedule=CommissionSchedule(Decimal("0.005"), Decimal("0.001")),
            ),
            DecisionKind.BUY_MORE,
            "AVERAGING_LEVEL",
        ),
        (
            "S-12",
            lambda: _base_context(
                available_free_cash=Decimal("1000"),
                invested_amount=Decimal("900"),
                commission_schedule=CommissionSchedule(Decimal("0.005"), Decimal("0.001")),
            ),
            DecisionKind.BUY_MORE,
            "AVERAGING_LEVEL",
        ),
        (
            "S-13",
            lambda: _buy_more_context(
                best_bid=Decimal("99.7"),
                commission_schedule=CommissionSchedule(Decimal("0.005"), Decimal("0.005")),
                lot_size=1,
            ),
            DecisionKind.WAIT,
            "COMMISSION_EXCEEDS_PARTIAL_PROFIT",
        ),
        (
            "S-14",
            lambda: _buy_more_context(
                best_bid=Decimal("101.1"),
                commission_schedule=CommissionSchedule(Decimal("0.001"), Decimal("0.001")),
            ),
            DecisionKind.SELL_PART,
            "LIFO_LOT_TAKE_PROFIT",
        ),
        (
            "S-15",
            lambda: _base_context(
                commission_schedule=CommissionSchedule(Decimal("0.005"), Decimal("0.005")),
                current_price=Decimal("100.2"),
                best_bid=Decimal("100.2"),
            ),
            DecisionKind.WAIT,
            "COMMISSION_EXCEEDS_PARTIAL_PROFIT",
        ),
        (
            "S-16",
            lambda: _base_context(
                current_price=Decimal("100.81"),
                best_bid=Decimal("100.81"),
                commission_schedule=CommissionSchedule(Decimal("0.001"), Decimal("0.001")),
            ),
            DecisionKind.SELL_PART,
            "PARTIAL_TAKE_PROFIT",
        ),
        (
            "S-17",
            _base_context,
            DecisionKind.BUY_MORE,
            "AVERAGING_LEVEL",
        ),
        (
            "S-18",
            lambda: _flat_context(
                cycle=_base_cycle().model_copy(update={"last_sell_price": None}),
                best_ask=Decimal("2000"),
            ),
            DecisionKind.WAIT,
            "BUY_UPPER_RANGE_FILTER",
        ),
        (
            "S-19",
            lambda: _flat_context(
                current_price=Decimal("105.5"),
                best_ask=Decimal("105.5"),
                indicators=_base_indicators().model_copy(
                    update={"range_high": Decimal("110"), "range_low": Decimal("90"), "mean_20": Decimal("100")}
                ),
            ),
            DecisionKind.WAIT,
            "BUY_UPPER_RANGE_FILTER",
        ),
        (
            "S-20",
            lambda: _base_context(
                cycle=_base_cycle().model_copy(update={"last_buy_candle_at": NOW}),
                indicators=_base_indicators().model_copy(update={"last_candle_at": NOW}),
                current_price=Decimal("99.5"),
                best_ask=Decimal("99.5"),
            ),
            DecisionKind.WAIT,
            "BUY_CANDLE_COOLDOWN",
        ),
        (
            "S-21",
            lambda: _base_context(
                indicators=_base_indicators().model_copy(
                    update={"mean_5": Decimal("98"), "mean_20": Decimal("100"), "change_10_percent": Decimal("-2")}
                ),
                current_price=Decimal("99.3"),
                best_bid=Decimal("99.3"),
            ),
            DecisionKind.WAIT,
            "DOWNTREND_FILTER",
        ),
        (
            "S-22",
            lambda: _base_context(current_price=Decimal("100"), best_bid=Decimal("100"), best_ask=Decimal("100")),
            DecisionKind.WAIT,
            "NO_THRESHOLD",
        ),
    ],
)
def test_strategy_case_registry(
    case_id: str,
    context_factory,
    expected_kind: DecisionKind,
    expected_reason: str,
) -> None:
    context = context_factory()
    decision = TradeDecisionService().decide(context)

    assert decision.kind is expected_kind, f"{case_id} -> wrong kind"
    if expected_reason:
        assert decision.reason_code == expected_reason, f"{case_id} -> wrong reason"
