from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sentinel_contracts.trading import DecisionKind
from trading_automaton.config import StrategySettings
from trading_automaton.services.account_commission_profile import CommissionSchedule
from trading_automaton.services.decision import DecisionContext
from trading_automaton.services.market_indicators import MarketIndicators
from trading_automaton.services.strategies import AdaptiveScalpingStrategy
from trading_automaton.storage.repository import TradeLotRecord, TradingCycleState

NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


def context(**changes: object) -> DecisionContext:
    cycle = TradingCycleState(
        automation_id="a",
        pending_low=Decimal("99"),
        last_buy_candle_at=None,
        sell_armed=True,
        last_sell_price=None,
        updated_at=NOW,
    )
    indicators = MarketIndicators(
        averaging_step_percent=Decimal("0.5"),
        minimum_net_profit_percent=Decimal("0.5"),
        source="ADAPTIVE",
        mean_5=Decimal("100"),
        mean_20=Decimal("100"),
        change_10_percent=Decimal("0"),
        last_candle_at=NOW,
        range_low=Decimal("90"),
        range_high=Decimal("110"),
    )
    values = {
        "core_available": True,
        "has_active_intent": False,
        "quantity_lots": 4,
        "lot_size": 1,
        "average_price": Decimal("100"),
        "current_price": Decimal("99.3"),
        "best_bid": Decimal("99.3"),
        "best_ask": Decimal("99.4"),
        "last_buy_price": Decimal("100"),
        "invested_amount": Decimal("400"),
        "commission_schedule": CommissionSchedule(buy_rate=Decimal("0.001"), sell_rate=Decimal("0.001")),
        "buy_commission_per_lot": Decimal(),
        "averaging_step_percent": Decimal("0.5"),
        "minimum_net_profit_percent": Decimal("0.5"),
        "completed_partial_sell_steps": 0,
        "settings": StrategySettings(),
        "currency": "RUB",
        "min_price_increment": Decimal("0.1"),
        "lots": (),
        "cycle": cycle,
        "indicators": indicators,
        "free_cash": Decimal("10000"),
        "reserved_cash": Decimal(),
        "available_free_cash": Decimal("10000"),
        "required_order_cash": Decimal(),
    }
    values.update(changes)
    return DecisionContext(**values)


def test_buy_requires_reversal_and_blocks_downtrend_and_same_candle() -> None:
    strategy = AdaptiveScalpingStrategy()
    assert strategy.decide(context(current_price=Decimal("99.1"))).reason_code == ("REVERSAL_NOT_CONFIRMED")
    assert strategy.decide(context()).kind is DecisionKind.BUY_MORE

    downtrend = context().indicators.model_copy(
        update={
            "mean_5": Decimal("98"),
            "mean_20": Decimal("99"),
            "change_10_percent": Decimal("-0.6"),
        }
    )
    assert strategy.decide(context(indicators=downtrend)).reason_code == "DOWNTREND_FILTER"

    same_candle = context().cycle.model_copy(update={"last_buy_candle_at": NOW})
    assert strategy.decide(context(cycle=same_candle)).reason_code == "BUY_CANDLE_COOLDOWN"


def test_entry_and_averaging_use_the_same_global_buy_lot_count() -> None:
    settings = StrategySettings(STRATEGY_BUY_ORDER_LOTS=2)
    entry = AdaptiveScalpingStrategy().decide(
        context(
            quantity_lots=0,
            average_price=Decimal(),
            invested_amount=Decimal(),
            settings=settings,
        )
    )
    averaging = AdaptiveScalpingStrategy().decide(context(settings=settings))

    assert (entry.kind, entry.quantity_lots) == (DecisionKind.BUY_MORE, 2)
    assert (averaging.kind, averaging.quantity_lots) == (DecisionKind.BUY_MORE, 2)


def test_averaging_and_partial_sale_boundaries_include_candidate_commission() -> None:
    buy_schedule = CommissionSchedule(buy_rate=Decimal("0.01"), sell_rate=Decimal())
    admitted = AdaptiveScalpingStrategy().decide(
        context(
            best_ask=Decimal("99.4"),
            invested_amount=Decimal("2797.99"),
            commission_schedule=buy_schedule,
        )
    )
    still_admitted = AdaptiveScalpingStrategy().decide(
        context(
            best_ask=Decimal("99.4"),
            invested_amount=Decimal("999999"),
            commission_schedule=buy_schedule,
        )
    )
    lot = TradeLotRecord(
        id="lot",
        automation_id="a",
        source_intent_id="i",
        source="EXECUTED",
        original_lots=1,
        remaining_lots=1,
        entry_price=Decimal("100"),
        entry_commission=Decimal(),
        opened_at=NOW,
    )
    lifo_blocked = AdaptiveScalpingStrategy().decide(
        context(
            best_bid=Decimal("101.00"),
            lots=(lot,),
            commission_schedule=CommissionSchedule(buy_rate=Decimal(), sell_rate=Decimal("0.005")),
        )
    )
    lifo_admitted = AdaptiveScalpingStrategy().decide(
        context(
            best_bid=Decimal("101.01"),
            lots=(lot,),
            commission_schedule=CommissionSchedule(buy_rate=Decimal(), sell_rate=Decimal("0.005")),
        )
    )
    aggregate_blocked = AdaptiveScalpingStrategy().decide(
        context(
            best_bid=Decimal("100.80"),
            current_price=Decimal("100.80"),
            commission_schedule=CommissionSchedule(buy_rate=Decimal(), sell_rate=Decimal("0.003")),
        )
    )
    aggregate_admitted = AdaptiveScalpingStrategy().decide(
        context(
            best_bid=Decimal("100.81"),
            current_price=Decimal("100.81"),
            commission_schedule=CommissionSchedule(buy_rate=Decimal(), sell_rate=Decimal("0.003")),
        )
    )

    assert (admitted.kind, admitted.reason_code) == (DecisionKind.BUY_MORE, "AVERAGING_LEVEL")
    assert (still_admitted.kind, still_admitted.reason_code) == (DecisionKind.BUY_MORE, "AVERAGING_LEVEL")
    assert (lifo_blocked.kind, lifo_blocked.reason_code) == (DecisionKind.WAIT, "COMMISSION_EXCEEDS_PARTIAL_PROFIT")
    assert (lifo_admitted.kind, lifo_admitted.reason_code) == (DecisionKind.SELL_PART, "LIFO_LOT_TAKE_PROFIT")
    assert (aggregate_blocked.kind, aggregate_blocked.reason_code) == (
        DecisionKind.WAIT,
        "COMMISSION_EXCEEDS_PARTIAL_PROFIT",
    )
    assert (aggregate_admitted.kind, aggregate_admitted.reason_code) == (DecisionKind.SELL_PART, "PARTIAL_TAKE_PROFIT")


def test_eligible_buy_has_no_historical_averaging_limit() -> None:
    decision = AdaptiveScalpingStrategy().decide(
        context(
            available_free_cash=Decimal("10000"),
        )
    )

    assert decision.kind is DecisionKind.BUY_MORE


def test_newest_profitable_lifo_lot_is_sold_independently() -> None:
    old = TradeLotRecord(
        id="old",
        automation_id="a",
        source_intent_id="i1",
        source="EXECUTED",
        original_lots=2,
        remaining_lots=2,
        entry_price=Decimal("110"),
        entry_commission=Decimal(),
        opened_at=NOW,
    )
    new = TradeLotRecord(
        id="new",
        automation_id="a",
        source_intent_id="i2",
        source="EXECUTED",
        original_lots=2,
        remaining_lots=2,
        entry_price=Decimal("98"),
        entry_commission=Decimal("0.1"),
        opened_at=NOW + timedelta(minutes=1),
    )
    decision = AdaptiveScalpingStrategy().decide(
        context(
            current_price=Decimal("100"),
            best_bid=Decimal("100"),
            average_price=Decimal("104"),
            lots=(new, old),
        )
    )
    assert decision.kind is DecisionKind.SELL_PART
    assert decision.target_lot_id == "new"


def test_disarmed_sell_cycle_waits() -> None:
    lot = TradeLotRecord(
        id="new",
        automation_id="a",
        source_intent_id="i",
        source="EXECUTED",
        original_lots=2,
        remaining_lots=2,
        entry_price=Decimal("98"),
        entry_commission=Decimal(),
        opened_at=NOW,
    )
    cycle = context().cycle.model_copy(update={"sell_armed": False, "last_sell_price": Decimal("100")})
    decision = AdaptiveScalpingStrategy().decide(
        context(
            current_price=Decimal("101"),
            best_bid=Decimal("101"),
            lots=(lot,),
            cycle=cycle,
        )
    )
    assert decision.reason_code == "SELL_CYCLE_DISARMED"


def test_flat_green_entry_waits_for_pullback_and_reversal() -> None:
    strategy = AdaptiveScalpingStrategy()
    indicators = context().indicators.model_copy(
        update={
            "last_candle_open": Decimal("99"),
            "last_candle_close": Decimal("100"),
        }
    )
    waiting_pullback = context().cycle.model_copy(
        update={"pending_low": None, "sell_armed": False, "last_sell_price": Decimal("100")}
    )
    waiting_reversal = waiting_pullback.model_copy(update={"pending_low": Decimal("99"), "sell_armed": True})

    pullback = strategy.decide(
        context(
            quantity_lots=0,
            average_price=Decimal(),
            invested_amount=Decimal(),
            indicators=indicators,
            cycle=waiting_pullback,
        )
    )
    reversal = strategy.decide(
        context(
            quantity_lots=0,
            average_price=Decimal(),
            invested_amount=Decimal(),
            current_price=Decimal("99.1"),
            best_ask=Decimal("99.2"),
            indicators=indicators,
            cycle=waiting_reversal,
        )
    )
    confirmed = strategy.decide(
        context(
            quantity_lots=0,
            average_price=Decimal(),
            invested_amount=Decimal(),
            current_price=Decimal("99.3"),
            best_ask=Decimal("99.4"),
            indicators=indicators,
            cycle=waiting_reversal,
        )
    )

    assert pullback.reason_code == "ENTRY_PULLBACK_WAIT"
    assert reversal.reason_code == "ENTRY_REVERSAL_WAIT"
    assert confirmed.kind is DecisionKind.BUY_MORE
    assert confirmed.reason_code == "ENTRY_REVERSAL_CONFIRMED"


def test_flat_red_entry_uses_observed_low_and_fixed_lot_count() -> None:
    cycle = context().cycle.model_copy(update={"pending_low": Decimal("99"), "sell_armed": True})
    red = context().indicators.model_copy(
        update={
            "last_candle_open": Decimal("100"),
            "last_candle_close": Decimal("99"),
        }
    )
    confirmed = AdaptiveScalpingStrategy().decide(
        context(
            quantity_lots=0,
            average_price=Decimal(),
            invested_amount=Decimal(),
            current_price=Decimal("99.3"),
            best_ask=Decimal("99.4"),
            cycle=cycle,
            indicators=red,
        )
    )
    expensive = AdaptiveScalpingStrategy().decide(
        context(
            quantity_lots=0,
            average_price=Decimal(),
            invested_amount=Decimal(),
            current_price=Decimal("160.8"),
            best_ask=Decimal("160.8"),
            lot_size=10,
            cycle=cycle.model_copy(update={"pending_low": Decimal("160")}),
            indicators=red.model_copy(
                update={"mean_20": Decimal("160"), "range_low": Decimal("150"), "range_high": Decimal("180")}
            ),
        )
    )

    assert confirmed.kind is DecisionKind.BUY_MORE
    assert confirmed.quantity_lots == 1
    assert expensive.kind is DecisionKind.BUY_MORE
    assert expensive.quantity_lots == 1


def test_repeat_entry_waits_in_upper_quarter_of_two_hour_range() -> None:
    cycle = context().cycle.model_copy(
        update={
            "pending_low": Decimal("99"),
            "sell_armed": True,
            "last_sell_price": Decimal("100"),
        }
    )
    indicators = context().indicators.model_copy(
        update={
            "mean_20": Decimal("100"),
            "range_low": Decimal("90"),
            "range_high": Decimal("110"),
        }
    )

    decision = AdaptiveScalpingStrategy().decide(
        context(
            quantity_lots=0,
            average_price=Decimal(),
            invested_amount=Decimal(),
            current_price=Decimal("105"),
            best_ask=Decimal("105"),
            cycle=cycle,
            indicators=indicators,
        )
    )

    assert decision.reason_code == "BUY_UPPER_RANGE_FILTER"


def test_repeat_entry_is_allowed_below_upper_quarter() -> None:
    cycle = context().cycle.model_copy(
        update={
            "pending_low": Decimal("99"),
            "sell_armed": True,
            "last_sell_price": Decimal("100"),
        }
    )
    indicators = context().indicators.model_copy(
        update={
            "mean_20": Decimal("100"),
            "range_low": Decimal("90"),
            "range_high": Decimal("110"),
        }
    )

    decision = AdaptiveScalpingStrategy().decide(
        context(
            quantity_lots=0,
            average_price=Decimal(),
            invested_amount=Decimal(),
            current_price=Decimal("104.9"),
            best_ask=Decimal("104.9"),
            cycle=cycle,
            indicators=indicators,
        )
    )

    assert decision.reason_code == "ENTRY_REVERSAL_CONFIRMED"


def test_upper_range_filter_applies_to_initial_entry() -> None:
    cycle = context().cycle.model_copy(update={"pending_low": Decimal("99"), "sell_armed": True})
    indicators = context().indicators.model_copy(
        update={
            "mean_20": Decimal("100"),
            "range_low": Decimal("90"),
            "range_high": Decimal("110"),
        }
    )

    decision = AdaptiveScalpingStrategy().decide(
        context(
            quantity_lots=0,
            average_price=Decimal(),
            invested_amount=Decimal(),
            current_price=Decimal("105"),
            best_ask=Decimal("105"),
            cycle=cycle,
            indicators=indicators,
        )
    )

    assert (decision.kind, decision.reason_code) == (DecisionKind.WAIT, "BUY_UPPER_RANGE_FILTER")


def test_repeat_entry_ignores_zero_range() -> None:
    cycle = context().cycle.model_copy(
        update={
            "pending_low": Decimal("99"),
            "sell_armed": True,
            "last_sell_price": Decimal("100"),
        }
    )
    indicators = context().indicators.model_copy(
        update={
            "mean_20": Decimal("100"),
            "range_low": Decimal("105"),
            "range_high": Decimal("105"),
        }
    )

    decision = AdaptiveScalpingStrategy().decide(
        context(
            quantity_lots=0,
            average_price=Decimal(),
            invested_amount=Decimal(),
            current_price=Decimal("105"),
            best_ask=Decimal("105"),
            cycle=cycle,
            indicators=indicators,
        )
    )

    assert decision.reason_code == "ENTRY_REVERSAL_CONFIRMED"
