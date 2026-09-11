"""Trading summary values and deterministic calculations."""

from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict, field_validator

from moex_sentinel.domain.portfolio import BrokerReadError, ExternalOperation
from sentinel_contracts.base import PositionalModel
from sentinel_contracts.time import floor_utc_millisecond

INPUT_OPERATION_TYPES = frozenset(
    {
        "OPERATION_TYPE_INPUT",
        "OPERATION_TYPE_INPUT_SWIFT",
        "OPERATION_TYPE_INPUT_ACQUIRING",
        "OPERATION_TYPE_INP_MULTI",
    }
)
OUTPUT_OPERATION_TYPES = frozenset(
    {
        "OPERATION_TYPE_OUTPUT",
        "OPERATION_TYPE_OUTPUT_SWIFT",
        "OPERATION_TYPE_OUTPUT_ACQUIRING",
        "OPERATION_TYPE_OUT_MULTI",
    }
)
EXECUTED_OPERATION_STATES = frozenset({"EXECUTED", "OPERATION_STATE_EXECUTED"})


class CashFlowTotals(PositionalModel):
    """Executed deposits and withdrawals for one currency."""

    model_config = ConfigDict(frozen=True)

    deposits: Decimal
    withdrawals: Decimal


class PortfolioSnapshotValue(PositionalModel):
    """One persisted account and currency valuation."""

    model_config = ConfigDict(frozen=True)

    id: str
    run_id: str
    user_broker_id: str
    account_id: str
    currency: str
    total_value: Decimal
    free_cash: Decimal
    cumulative_pnl: Decimal
    captured_at: datetime
    bucket_start: datetime
    created_at: datetime

    @field_validator("captured_at", "bucket_start", "created_at", mode="before")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return floor_utc_millisecond(value)


class PortfolioSnapshotRunValue(PositionalModel):
    """One completed collection run and its account-isolated errors."""

    model_config = ConfigDict(frozen=True)

    id: str
    captured_at: datetime
    bucket_start: datetime
    errors: tuple[BrokerReadError, ...]
    created_at: datetime

    @field_validator("captured_at", "bucket_start", "created_at", mode="before")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return floor_utc_millisecond(value)


class TradingPnlPeriod(PositionalModel):
    """P&L delta and the actual history interval used to calculate it."""

    model_config = ConfigDict(frozen=True)

    value: Decimal | None
    from_at: datetime | None
    to_at: datetime | None
    complete: bool

    @field_validator("from_at", "to_at", mode="before")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else floor_utc_millisecond(value)


class CurrencyTradingSummary(PositionalModel):
    """Current valuation and rolling P&L for one currency."""

    model_config = ConfigDict(frozen=True)

    currency: str
    portfolio_value: Decimal
    free_cash: Decimal
    pnl_24h: TradingPnlPeriod
    pnl_7d: TradingPnlPeriod
    pnl_30d: TradingPnlPeriod


class TradingSummaryView(PositionalModel):
    """Persisted trading summary returned to API consumers."""

    model_config = ConfigDict(frozen=True)

    captured_at: datetime | None
    currencies: tuple[CurrencyTradingSummary, ...]
    errors: tuple[BrokerReadError, ...]

    @field_validator("captured_at", mode="before")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else floor_utc_millisecond(value)


def advance_cumulative_pnl(
    *,
    previous_cumulative: Decimal,
    previous_value: Decimal,
    current_value: Decimal,
    deposits: Decimal,
    withdrawals: Decimal,
) -> Decimal:
    """Advance trading P&L without treating cash movements as profit or loss."""
    return previous_cumulative + current_value - previous_value - deposits + withdrawals


def cash_flows(operations: tuple[ExternalOperation, ...], *, currency: str) -> CashFlowTotals:
    """Sum executed monetary transfers in one currency."""
    normalized_currency = currency.upper()
    deposits = Decimal()
    withdrawals = Decimal()
    for operation in operations:
        payment = operation.payment
        if (
            operation.state not in EXECUTED_OPERATION_STATES
            or payment is None
            or payment.currency.upper() != normalized_currency
        ):
            continue
        if operation.operation_type in INPUT_OPERATION_TYPES:
            deposits += abs(payment.amount)
        elif operation.operation_type in OUTPUT_OPERATION_TYPES:
            withdrawals += abs(payment.amount)
    return CashFlowTotals(deposits, withdrawals)


def period_from_snapshots(
    latest: PortfolioSnapshotValue,
    baseline: PortfolioSnapshotValue | None,
    *,
    requested_from: datetime,
) -> TradingPnlPeriod:
    """Calculate one rolling period from persisted cumulative P&L values."""
    requested = floor_utc_millisecond(requested_from)
    if baseline is None:
        return TradingPnlPeriod(None, None, latest.captured_at, False)
    return TradingPnlPeriod(
        latest.cumulative_pnl - baseline.cumulative_pnl,
        baseline.captured_at,
        latest.captured_at,
        baseline.captured_at <= requested,
    )
