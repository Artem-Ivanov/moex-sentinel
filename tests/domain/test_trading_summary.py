from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from moex_sentinel.domain.portfolio import BrokerReadError, ExternalOperation, Money
from moex_sentinel.domain.trading_summary import (
    CashFlowTotals,
    CurrencyTradingSummary,
    PortfolioSnapshotValue,
    TradingPnlPeriod,
    TradingSummaryView,
    advance_cumulative_pnl,
    cash_flows,
    period_from_snapshots,
)

NOW = datetime(2026, 8, 15, 10, tzinfo=UTC)


def operation(
    operation_id: str,
    operation_type: str,
    amount: str,
    *,
    currency: str = "RUB",
    state: str = "OPERATION_STATE_EXECUTED",
) -> ExternalOperation:
    return ExternalOperation(
        operation_id=operation_id,
        account_id="account-1",
        operation_type=operation_type,
        state=state,
        occurred_at=NOW,
        payment=Money(Decimal(amount), currency),
        price=None,
        quantity=Decimal(),
        commission=None,
    )


def test_advance_cumulative_pnl_excludes_deposits_and_withdrawals() -> None:
    result = advance_cumulative_pnl(
        previous_cumulative=Decimal("12"),
        previous_value=Decimal("100"),
        current_value=Decimal("145"),
        deposits=Decimal("50"),
        withdrawals=Decimal("5"),
    )

    assert result == Decimal("12")


def test_cash_flows_only_count_executed_money_movements_in_currency() -> None:
    operations = (
        operation("deposit", "OPERATION_TYPE_INPUT", "100"),
        operation("withdrawal", "OPERATION_TYPE_OUTPUT", "-25"),
        operation("rejected", "OPERATION_TYPE_INPUT", "40", state="OPERATION_STATE_CANCELED"),
        operation("security", "OPERATION_TYPE_INPUT_SECURITIES", "10"),
        operation("other-currency", "OPERATION_TYPE_INPUT", "50", currency="USD"),
        operation("commission", "OPERATION_TYPE_BROKER_FEE", "-2"),
    )

    result = cash_flows(operations, currency="rub")

    assert result == CashFlowTotals(deposits=Decimal("100"), withdrawals=Decimal("25"))


def snapshot(snapshot_id: str, cumulative_pnl: str, captured_at: datetime) -> PortfolioSnapshotValue:
    return PortfolioSnapshotValue(
        id=snapshot_id,
        run_id="run-1",
        user_broker_id="broker-1",
        account_id="account-1",
        currency="RUB",
        total_value=Decimal("100"),
        free_cash=Decimal("25"),
        cumulative_pnl=Decimal(cumulative_pnl),
        captured_at=captured_at,
        bucket_start=captured_at.replace(second=0, microsecond=0),
        created_at=captured_at,
    )


def test_period_marks_earliest_snapshot_as_incomplete() -> None:
    latest = snapshot("latest", "12", NOW)
    earliest = snapshot("earliest", "5", NOW - timedelta(days=2))

    period = period_from_snapshots(latest, earliest, requested_from=NOW - timedelta(days=7))

    assert period.value == Decimal("7")
    assert period.from_at == NOW - timedelta(days=2)
    assert period.to_at == NOW
    assert period.complete is False


def test_snapshot_times_are_normalized_to_utc_milliseconds() -> None:
    captured_at = datetime(2026, 8, 15, 13, 0, 0, 123456, tzinfo=timezone(timedelta(hours=3)))

    value = snapshot("snapshot-1", "0", captured_at)

    assert value.captured_at == datetime(2026, 8, 15, 10, 0, 0, 123000, tzinfo=UTC)
    assert value.created_at == datetime(2026, 8, 15, 10, 0, 0, 123000, tzinfo=UTC)


def test_trading_summary_keeps_currency_periods_and_safe_errors() -> None:
    period = TradingPnlPeriod(Decimal("7"), NOW - timedelta(days=1), NOW, True)
    currency = CurrencyTradingSummary("RUB", Decimal("100"), Decimal("25"), period, period, period)
    error = BrokerReadError("broker-1", "Broker", "account-2", "BROKER_UNAVAILABLE", "Недоступно")
    captured_at = datetime(2026, 8, 15, 13, 0, 0, 123456, tzinfo=timezone(timedelta(hours=3)))

    result = TradingSummaryView(captured_at, (currency,), (error,))

    assert result.captured_at == datetime(2026, 8, 15, 10, 0, 0, 123000, tzinfo=UTC)
    assert result.currencies == (currency,)
    assert result.errors == (error,)
