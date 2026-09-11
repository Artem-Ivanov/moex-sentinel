from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel_contracts.broker_execution import BrokerPosition, OrderBookLevel
from sentinel_contracts.streaming_market import InstrumentMarketState, StreamOrderBook
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.services.market_indicators import MarketIndicators
from trading_automaton.services.streaming_cycle_transition import StreamingCycleTransitionService
from trading_automaton.services.streaming_position_decision import HydratedPositionState
from trading_automaton.storage.repository import IntentHistory, TradingCycleState

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def command() -> AutomationCommand:
    return baseline_command()


def market(price: str) -> InstrumentMarketState:
    value = Decimal(price)
    return InstrumentMarketState(
        "instrument",
        order_book=StreamOrderBook(
            "instrument",
            (OrderBookLevel(value, 10),),
            (OrderBookLevel(value + Decimal("0.01"), 10),),
            NOW,
            True,
        ),
    )


def state(*, quantity: str, pending_low: Decimal | None) -> HydratedPositionState:
    return HydratedPositionState(
        BrokerPosition("instrument", Decimal(quantity), Decimal("100"), Decimal("100"), "RUB"),
        IntentHistory(Decimal("100"), 0, Decimal(), 1, Decimal()),
        (),
        TradingCycleState("automation", pending_low, None, True, Decimal("101"), NOW),
        MarketIndicators(
            Decimal("0.5"),
            Decimal("0.5"),
            "TEST",
            Decimal("99"),
            Decimal("100"),
            Decimal("-1"),
            NOW,
            Decimal("100"),
            Decimal("99"),
        ),
    )


def test_flat_position_observes_first_pending_low() -> None:
    result = StreamingCycleTransitionService(now=lambda: NOW).apply(
        command(),
        state(quantity="0", pending_low=None),
        market("99"),
    )

    assert result.cycle.pending_low == Decimal("99")
    assert result.cycle.sell_armed is True


def test_open_position_moves_pending_low_with_market() -> None:
    result = StreamingCycleTransitionService(now=lambda: NOW).apply(
        command(),
        state(quantity="2", pending_low=Decimal("99")),
        market("98"),
    )

    assert result.cycle.pending_low == Decimal("98")


@pytest.mark.parametrize("invalid", ["stale", "crossed", "empty", "inconsistent", "future"])
def test_invalid_market_cannot_seed_a_false_low_before_fresh_reversal(invalid: str) -> None:
    initial = state(quantity="0", pending_low=None)
    observed = market("90")
    book = observed.order_book
    assert book is not None
    changes = {
        "stale": {"captured_at": NOW - timedelta(seconds=3)},
        "crossed": {"asks": book.bids},
        "empty": {"bids": ()},
        "inconsistent": {"is_consistent": False},
        "future": {"captured_at": NOW + timedelta(seconds=1)},
    }
    observed = observed.model_copy(update={"order_book": book.model_copy(update=changes[invalid])})
    service = StreamingCycleTransitionService(now=lambda: NOW)

    rejected = service.apply(command(), initial, observed)
    recovered = service.apply(command(), rejected, market("99"))

    assert rejected == initial
    assert recovered.cycle.pending_low == Decimal("99")
