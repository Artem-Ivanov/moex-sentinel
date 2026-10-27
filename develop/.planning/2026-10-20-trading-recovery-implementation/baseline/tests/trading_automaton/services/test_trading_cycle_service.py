from datetime import UTC, datetime
from decimal import Decimal

from trading_automaton.services.trading_cycle import TradingCycleService
from trading_automaton.storage.repository import TradingCycleState

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def state(**changes: object) -> TradingCycleState:
    values = {
        "automation_id": "automation-1",
        "pending_low": None,
        "last_buy_candle_at": None,
        "sell_armed": True,
        "last_sell_price": None,
        "updated_at": NOW,
    }
    values.update(changes)
    return TradingCycleState(**values)  # type: ignore[arg-type]


def test_complete_cycle_starts_flat_entry_from_exit_price() -> None:
    completed = TradingCycleService().complete_cycle(state(pending_low=Decimal("90")), Decimal("100"), now=NOW)

    assert completed.pending_low is None
    assert completed.last_sell_price == Decimal("100")
    assert completed.sell_armed is False


def test_green_entry_tracks_peak_then_requires_pullback() -> None:
    service = TradingCycleService()
    cycle = service.complete_cycle(state(), Decimal("100"), now=NOW)

    rising = service.observe_flat_entry(cycle, Decimal("101"), "GREEN", Decimal("0.5"), now=NOW)
    shallow = service.observe_flat_entry(rising, Decimal("100.8"), "GREEN", Decimal("0.5"), now=NOW)
    pulled_back = service.observe_flat_entry(shallow, Decimal("100.4"), "GREEN", Decimal("0.5"), now=NOW)

    assert rising.last_sell_price == Decimal("101")
    assert shallow.sell_armed is False
    assert shallow.pending_low is None
    assert pulled_back.sell_armed is True
    assert pulled_back.pending_low == Decimal("100.4")


def test_red_or_flat_entry_observes_low_immediately() -> None:
    service = TradingCycleService()
    cycle = service.complete_cycle(state(), Decimal("100"), now=NOW)

    red = service.observe_flat_entry(cycle, Decimal("99"), "RED", Decimal("0.5"), now=NOW)
    lower = service.observe_flat_entry(red, Decimal("98.5"), "FLAT", Decimal("0.5"), now=NOW)

    assert red.sell_armed is True
    assert lower.pending_low == Decimal("98.5")


def test_sell_cycle_rearms_on_next_upward_profit_level() -> None:
    service = TradingCycleService()
    disarmed = state(
        pending_low=Decimal("95"),
        sell_armed=False,
        last_sell_price=Decimal("100"),
    )

    below_level = service.rearm_for_next_sell(
        disarmed,
        Decimal("100.49"),
        retracement_percent=Decimal("0.5"),
        progression_percent=Decimal("0.5"),
        now=NOW,
    )
    at_level = service.rearm_for_next_sell(
        below_level,
        Decimal("100.50"),
        retracement_percent=Decimal("0.5"),
        progression_percent=Decimal("0.5"),
        now=NOW,
    )

    assert below_level.sell_armed is False
    assert at_level.sell_armed is True
    assert at_level.pending_low is None


def test_completed_buy_and_sell_clear_previous_pending_low() -> None:
    service = TradingCycleService()
    pending = state(pending_low=Decimal("95"))

    bought = service.mark_buy(pending, NOW, now=NOW)
    sold = service.mark_sell(pending, Decimal("101"), now=NOW)

    assert bought.pending_low is None
    assert sold.pending_low is None
