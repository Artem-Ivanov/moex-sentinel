"""Priority and position-limit rules for deterministic MVP trading decisions."""

from datetime import UTC, datetime
from decimal import Decimal

from sentinel_contracts.trading import DecisionKind
from trading_automaton.config import StrategySettings
from trading_automaton.services.account_commission_profile import CommissionSchedule
from trading_automaton.services.decision import (
    CoreAvailableRule,
    DecisionContext,
    DecisionRulePipeline,
    TradeDecision,
    TradeDecisionService,
)
from trading_automaton.services.market_indicators import MarketIndicators
from trading_automaton.storage.repository import TradingCycleState

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


class SpyStrategy:
    code = "SPY"
    version = "1"

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, context: DecisionContext) -> TradeDecision:  # noqa: ARG002
        self.calls += 1
        return TradeDecision(
            kind=DecisionKind.NO_ACTION,
            quantity_lots=context.quantity_lots,
            limit_price=context.current_price,
            reason_code="STRATEGY_SELECTED",
        )


class SpyRule:
    def __init__(self, result: TradeDecision) -> None:
        self._result = result
        self.calls = 0

    def evaluate(self, context: DecisionContext) -> TradeDecision | None:  # noqa: ARG002
        self.calls += 1
        return self._result


class NoopRule:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, context: DecisionContext) -> None:  # noqa: ARG002
        self.calls += 1
        # intentionally pass through to next decision stage


def test_pipeline_calls_rules_in_order_and_stops_on_first_match() -> None:
    service = TradeDecisionService(
        strategy=SpyStrategy(),
        rules=(
            CoreAvailableRule(),
            CoreAvailableRule(),
        ),
    )
    decision = service.decide(context(core_available=False))

    assert decision.kind is DecisionKind.WAIT
    assert decision.reason_code == "CORE_UNAVAILABLE"


def test_explicit_decision_pipeline_can_be_injected() -> None:
    class WaitRule:
        def __init__(self) -> None:
            self.calls = 0

        def evaluate(self, context: DecisionContext) -> TradeDecision | None:  # noqa: ARG002
            self.calls += 1
            return TradeDecision(
                kind=DecisionKind.WAIT,
                quantity_lots=0,
                limit_price=None,
                reason_code="PIPELINE_MATCH",
            )

    class BlockedStrategy:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def code(self) -> str:
            return "BLOCKED"

        @property
        def version(self) -> str:
            return "1"

        def decide(self, context: DecisionContext) -> TradeDecision:  # noqa: ARG002
            self.calls += 1
            return TradeDecision(
                kind=DecisionKind.NO_ACTION,
                quantity_lots=context.quantity_lots,
                limit_price=None,
                reason_code="SHOULD_NOT_CALL",
            )

    rule = WaitRule()
    strategy = BlockedStrategy()
    service = TradeDecisionService(strategy=strategy, pipeline=DecisionRulePipeline((rule,)))
    decision = service.decide(context())

    assert decision.reason_code == "PIPELINE_MATCH"
    assert strategy.calls == 0
    assert rule.calls == 1


def context(**changes: object) -> DecisionContext:
    values: dict[str, object] = {
        "core_available": True,
        "has_active_intent": False,
        "quantity_lots": 10,
        "lot_size": 1,
        "average_price": Decimal("100"),
        "current_price": Decimal("100"),
        "best_bid": Decimal("99.9"),
        "best_ask": Decimal("100.1"),
        "last_buy_price": Decimal("100"),
        "invested_amount": Decimal("1000"),
        "commission_schedule": CommissionSchedule(buy_rate=Decimal("0.005"), sell_rate=Decimal("0.002")),
        "buy_commission_per_lot": Decimal("0.1"),
        "averaging_step_percent": Decimal("0.5"),
        "minimum_net_profit_percent": Decimal("0.5"),
        "completed_partial_sell_steps": 0,
        "settings": StrategySettings(),
        "currency": "RUB",
        "available_free_cash": Decimal("10000"),
        "cycle": TradingCycleState("automation-1", Decimal("98"), None, True, None, NOW),
        "indicators": MarketIndicators(
            Decimal("0.5"),
            Decimal("0.5"),
            "FALLBACK",
            Decimal("100"),
            Decimal("100"),
            Decimal(),
            NOW,
            range_low=Decimal("90"),
            range_high=Decimal("110"),
        ),
    }
    values.update(changes)
    return DecisionContext(**values)  # type: ignore[arg-type]


def test_active_intent_or_core_outage_has_highest_wait_priority() -> None:
    service = TradeDecisionService()

    assert service.decide(context(has_active_intent=True)).kind is DecisionKind.WAIT
    assert service.decide(context(core_available=False)).kind is DecisionKind.WAIT


def test_decision_pipeline_respects_guard_order() -> None:
    strategy = SpyStrategy()
    active_rule = SpyRule(
        TradeDecision(
            kind=DecisionKind.WAIT,
            quantity_lots=0,
            limit_price=None,
            reason_code="ACTIVE_INTENT_TEST",
        )
    )
    service = TradeDecisionService(
        strategy=strategy,
        rules=(CoreAvailableRule(), active_rule),
    )
    decision = service.decide(context(core_available=False, has_active_intent=True))

    assert decision.reason_code == "CORE_UNAVAILABLE"
    assert strategy.calls == 0
    assert active_rule.calls == 0


def test_active_intent_short_circuits_strategy_and_rule_chain() -> None:
    strategy = SpyStrategy()
    active_rule = SpyRule(
        TradeDecision(
            kind=DecisionKind.WAIT,
            quantity_lots=0,
            limit_price=None,
            reason_code="ACTIVE_INTENT_TEST",
        )
    )
    service = TradeDecisionService(strategy=strategy, rules=(CoreAvailableRule(), active_rule))
    decision = service.decide(context(has_active_intent=True))

    assert decision.reason_code == "ACTIVE_INTENT_TEST"
    assert strategy.calls == 0


def test_zero_position_is_delegated_to_strategy_once() -> None:
    strategy = SpyStrategy()
    noop = NoopRule()
    service = TradeDecisionService(strategy=strategy, rules=(noop,))
    decision = service.decide(context(quantity_lots=0, average_price=Decimal(), invested_amount=Decimal()))

    assert strategy.calls == 1
    assert noop.calls == 1
    assert decision.reason_code == "STRATEGY_SELECTED"


def test_stop_loss_sells_everything_before_averaging() -> None:
    decision = TradeDecisionService().decide(context(current_price=Decimal("94"), best_bid=Decimal("93.9")))

    assert decision.kind is DecisionKind.SELL_ALL
    assert decision.quantity_lots == 10
    assert decision.reason_code == "STOP_LOSS"


def test_final_take_profit_sells_everything() -> None:
    decision = TradeDecisionService().decide(context(current_price=Decimal("106.4"), best_bid=Decimal("106.4")))

    assert decision.kind is DecisionKind.SELL_ALL
    assert decision.reason_code == "TAKE_PROFIT"


def test_each_profitable_partial_level_fires_only_once() -> None:
    first = TradeDecisionService().decide(
        context(
            current_price=Decimal("100.75"),
            best_bid=Decimal("100.75"),
            commission_schedule=CommissionSchedule(buy_rate=Decimal("0.005"), sell_rate=Decimal("0.0005")),
            buy_commission_per_lot=Decimal("0.05"),
        )
    )
    repeated = TradeDecisionService().decide(
        context(
            current_price=Decimal("100.75"),
            best_bid=Decimal("100.75"),
            completed_partial_sell_steps=1,
            commission_schedule=CommissionSchedule(buy_rate=Decimal("0.005"), sell_rate=Decimal("0.0005")),
            buy_commission_per_lot=Decimal("0.05"),
        )
    )

    assert first.kind is DecisionKind.SELL_PART
    assert first.quantity_lots == 2
    assert repeated.kind is DecisionKind.NO_ACTION


def test_decline_from_last_buy_anchor_buys_configured_lots() -> None:
    decision = TradeDecisionService().decide(
        context(
            current_price=Decimal("99.5"),
            best_ask=Decimal("99.5"),
            settings=StrategySettings(STRATEGY_BUY_ORDER_LOTS=2),
        )
    )

    assert decision.kind is DecisionKind.BUY_MORE
    assert decision.quantity_lots == 2
    assert decision.reason_code == "AVERAGING_LEVEL"


def test_runtime_averaging_threshold_overrides_strategy_snapshot() -> None:
    decision = TradeDecisionService().decide(
        context(
            current_price=Decimal("99.9"),
            best_ask=Decimal("99.9"),
            averaging_step_percent=Decimal("0.1"),
        )
    )

    assert decision.kind is DecisionKind.BUY_MORE


def test_partial_sale_requires_half_percent_profit_plus_both_commissions() -> None:
    service = TradeDecisionService()

    at_threshold = service.decide(
        context(
            current_price=Decimal("100.8"),
            best_bid=Decimal("100.8"),
            commission_schedule=CommissionSchedule(buy_rate=Decimal("0.005"), sell_rate=Decimal("0.002")),
            buy_commission_per_lot=Decimal("0.1"),
        )
    )
    above_threshold = service.decide(
        context(
            current_price=Decimal("100.81"),
            best_bid=Decimal("100.81"),
            commission_schedule=CommissionSchedule(buy_rate=Decimal("0.005"), sell_rate=Decimal("0.002")),
            buy_commission_per_lot=Decimal("0.1"),
        )
    )

    assert at_threshold.kind is DecisionKind.WAIT
    assert at_threshold.reason_code == "COMMISSION_EXCEEDS_PARTIAL_PROFIT"
    assert above_threshold.kind is DecisionKind.SELL_PART


def test_large_existing_position_does_not_block_signal_eligible_averaging() -> None:
    service = TradeDecisionService()
    decision = service.decide(
        context(
            current_price=Decimal("99.5"),
            best_ask=Decimal("99.5"),
            invested_amount=Decimal("999999"),
            commission_schedule=CommissionSchedule(buy_rate=Decimal("0.005"), sell_rate=Decimal("0.002")),
        )
    )

    assert decision.kind is DecisionKind.BUY_MORE
    assert decision.reason_code == "AVERAGING_LEVEL"


def test_disabled_global_strategy_returns_wait_without_sell_signal() -> None:
    decision = TradeDecisionService().decide(
        context(
            current_price=Decimal("90"),
            best_bid=Decimal("90"),
            settings=StrategySettings(STRATEGY_ENABLED=False),
        )
    )

    assert decision.kind is DecisionKind.WAIT
    assert decision.reason_code == "STRATEGY_DISABLED"


def test_commission_can_block_a_profitable_partial_sale() -> None:
    service = TradeDecisionService()
    no_profit = service.decide(
        context(
            current_price=Decimal("102"),
            best_bid=Decimal("102"),
            commission_schedule=CommissionSchedule(buy_rate=Decimal("0.005"), sell_rate=Decimal("0.15")),
        )
    )
    assert no_profit.kind is DecisionKind.WAIT


def test_zero_position_is_delegated_to_entry_strategy() -> None:
    cycle = TradingCycleState(
        automation_id="automation-1",
        pending_low=Decimal("99"),
        last_buy_candle_at=None,
        sell_armed=True,
        last_sell_price=Decimal("100"),
        updated_at=NOW,
    )
    indicators = MarketIndicators(
        averaging_step_percent=Decimal("0.5"),
        minimum_net_profit_percent=Decimal("0.5"),
        source="ADAPTIVE",
        mean_5=Decimal("100"),
        mean_20=Decimal("100"),
        change_10_percent=Decimal(),
        last_candle_at=NOW,
        range_low=Decimal("90"),
        range_high=Decimal("110"),
        last_candle_open=Decimal("100"),
        last_candle_close=Decimal("99"),
    )

    decision = TradeDecisionService().decide(
        context(
            quantity_lots=0,
            average_price=Decimal(),
            invested_amount=Decimal(),
            current_price=Decimal("99.3"),
            best_ask=Decimal("99.4"),
            cycle=cycle,
            indicators=indicators,
        )
    )

    assert decision.kind is DecisionKind.BUY_MORE
    assert decision.reason_code == "ENTRY_REVERSAL_CONFIRMED"
