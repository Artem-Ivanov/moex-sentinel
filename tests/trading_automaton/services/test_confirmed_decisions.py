"""Regression cases for confirmed entry and executable net-profit thresholds."""

from decimal import Decimal

import pytest

from sentinel_contracts.trading import DecisionKind
from tests.trading_automaton.services.test_adaptive_scalping_strategy import NOW, context
from trading_automaton.domain.dtos import CommissionSchedule, DecisionContext
from trading_automaton.services.decision import TakeProfitRule, TradeDecisionService
from trading_automaton.services.strategies import AdaptiveScalpingStrategy
from trading_automaton.storage.repository import TradeLotRecord


def confirmed_context(**changes: object) -> DecisionContext:
    base = context()
    indicators = base.indicators.model_copy(update={"range_low": Decimal("90"), "range_high": Decimal("110")})
    return base.model_copy(update={"indicators": indicators, **changes})


@pytest.mark.parametrize("quantity", [0, 4])
@pytest.mark.parametrize(
    "missing", ["mean_5", "mean_20", "change_10_percent", "last_candle_at", "range_low", "range_high"]
)
def test_buy_waits_when_confirmation_window_is_incomplete(quantity, missing) -> None:
    ready = confirmed_context(quantity_lots=quantity)
    incomplete = ready.indicators.model_copy(update={missing: None})

    decision = AdaptiveScalpingStrategy().decide(ready.model_copy(update={"indicators": incomplete}))

    assert (decision.kind, decision.reason_code) == (DecisionKind.WAIT, "BUY_WINDOW_UNCONFIRMED")


@pytest.mark.parametrize("quantity", [0, 4])
@pytest.mark.parametrize(
    ("trend", "expected"),
    [("sideways", DecisionKind.BUY_MORE), ("rising", DecisionKind.BUY_MORE), ("falling", DecisionKind.WAIT)],
)
def test_confirmed_buy_trend_matrix(quantity, trend, expected) -> None:
    ready = confirmed_context(quantity_lots=quantity)
    means = {"sideways": ("100", "0"), "rising": ("101", "0.6"), "falling": ("99", "-0.6")}
    mean_5, change = means[trend]
    indicators = ready.indicators.model_copy(update={"mean_5": Decimal(mean_5), "change_10_percent": Decimal(change)})

    decision = AdaptiveScalpingStrategy().decide(ready.model_copy(update={"indicators": indicators}))

    assert decision.kind is expected
    if expected is DecisionKind.WAIT:
        assert decision.reason_code == "DOWNTREND_FILTER"


@pytest.mark.parametrize("quantity", [0, 4])
@pytest.mark.parametrize(("ask", "expected"), [("99.39", DecisionKind.BUY_MORE), ("99.4", DecisionKind.WAIT)])
def test_buy_uses_existing_upper_quarter_boundary_for_entry_and_averaging(quantity, ask, expected) -> None:
    ready = confirmed_context(quantity_lots=quantity, best_ask=Decimal(ask))
    indicators = ready.indicators.model_copy(
        update={"mean_20": Decimal("99"), "range_low": Decimal("97"), "range_high": Decimal("100.2")}
    )

    decision = AdaptiveScalpingStrategy().decide(ready.model_copy(update={"indicators": indicators}))

    assert decision.kind is expected
    if expected is DecisionKind.WAIT:
        assert decision.reason_code == "BUY_UPPER_RANGE_FILTER"


@pytest.mark.parametrize(("ask", "expected"), [("99.5", DecisionKind.BUY_MORE), ("99.51", DecisionKind.WAIT)])
def test_averaging_threshold_uses_executable_ask(ask, expected) -> None:
    decision = AdaptiveScalpingStrategy().decide(confirmed_context(best_ask=Decimal(ask)))
    assert decision.kind is expected


@pytest.mark.parametrize("quantity", [0, 4])
def test_missing_cycle_does_not_allow_buy(quantity) -> None:
    decision = AdaptiveScalpingStrategy().decide(confirmed_context(quantity_lots=quantity, cycle=None))
    assert decision.kind is DecisionKind.WAIT


@pytest.mark.parametrize(("bid", "expected"), [("107.19", False), ("107.2", True)])
def test_full_take_profit_compares_net_proceeds_including_both_commissions(bid, expected) -> None:
    # Four shares cost 400 plus 0.512 entry commission; 1% sell fee.
    # At 107.2 proceeds are 424.512; less cost and entry fee = 24 (6%).
    decision = TakeProfitRule().evaluate(
        confirmed_context(
            current_price=Decimal("110"),
            best_bid=Decimal(bid),
            buy_commission_per_lot=Decimal("0.128"),
            commission_schedule=CommissionSchedule(Decimal(), Decimal("0.01")),
        )
    )
    assert (decision is not None) is expected


def test_full_take_profit_uses_only_commission_of_remaining_lots() -> None:
    lot = TradeLotRecord("remaining", "a", "buy", "EXECUTED", 10, 4, Decimal("100"), Decimal("1.28"), NOW)
    decision = TakeProfitRule().evaluate(
        confirmed_context(
            current_price=Decimal("110"),
            best_bid=Decimal("107.19"),
            lots=(lot,),
            buy_commission_per_lot=Decimal(),
            commission_schedule=CommissionSchedule(Decimal(), Decimal("0.01")),
        )
    )
    assert decision is None
    admitted = TakeProfitRule().evaluate(
        confirmed_context(
            current_price=Decimal("110"),
            best_bid=Decimal("107.2"),
            lots=(lot,),
            buy_commission_per_lot=Decimal("100"),
            commission_schedule=CommissionSchedule(Decimal(), Decimal("0.01")),
        )
    )
    assert admitted is not None
    assert admitted.quantity_lots == 4


def test_net_full_profit_shortfall_still_allows_profitable_partial_sale() -> None:
    decision = TradeDecisionService().decide(confirmed_context(current_price=Decimal("106"), best_bid=Decimal("106")))
    assert decision.kind is DecisionKind.SELL_PART


def test_stop_loss_does_not_wait_for_confirmation_window_or_commission_profit() -> None:
    decision = TradeDecisionService().decide(
        confirmed_context(
            current_price=Decimal("94"),
            best_bid=Decimal("94"),
            indicators=None,
            commission_schedule=CommissionSchedule(Decimal("1"), Decimal("1")),
        )
    )
    assert (decision.kind, decision.reason_code) == (DecisionKind.SELL_ALL, "STOP_LOSS")


@pytest.mark.parametrize("quantity", [0, 4])
def test_repeated_pure_decision_preserves_observed_cycle(quantity) -> None:
    ready = confirmed_context(quantity_lots=quantity)
    before = ready.model_copy(deep=True)
    service = TradeDecisionService()

    decisions = [service.decide(ready) for _ in range(3)]

    assert all(item.kind is DecisionKind.BUY_MORE for item in decisions)
    assert decisions[0] == decisions[1] == decisions[2]
    assert ready == before
