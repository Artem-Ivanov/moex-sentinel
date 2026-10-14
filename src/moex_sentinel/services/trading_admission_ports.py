"""Ports and broker read snapshots used by Core trade admission."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from moex_sentinel.domain.trading_admission import AdmissionInstrument, AdmissionOrderState
from sentinel_contracts.broker_execution import BrokerConnection, OrderSide


@dataclass(frozen=True)
class AdmissionPosition:
    instrument_uid: str
    quantity_units: Decimal
    blocked_units: Decimal
    mark_price: Decimal | None
    currency: str | None


@dataclass(frozen=True)
class AdmissionOrder:
    broker_order_id: str
    instrument_uid: str
    request_id: str | None
    status: str
    side: OrderSide | None
    quantity_lots: int | None


@dataclass(frozen=True)
class AdmissionOperation:
    operation_id: str
    occurred_at: datetime
    operation_type: str
    state: str
    payment: Decimal | None
    currency: str | None
    account_id: str


@dataclass(frozen=True)
class AdmissionRiskSnapshot:
    snapshot_id: str
    account_id: str
    capture_started_at: datetime
    captured_at: datetime
    valid_until: datetime
    equity: Decimal | None
    equity_currency: str | None
    money: Decimal | None
    money_currency: str | None
    blocked_cash: Decimal | None
    blocked_cash_currency: str | None
    money_loaded: bool
    positions: tuple[AdmissionPosition, ...]
    ordinary_orders: tuple[AdmissionOrder, ...]
    stop_orders: tuple[AdmissionOrder, ...]
    order_states: tuple[AdmissionOrderState, ...]
    operations: tuple[AdmissionOperation, ...]
    operations_complete: bool
    operations_through: datetime | None
    instrument: AdmissionInstrument | None
    unknown_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdmissionOrderEstimate:
    initial_order_amount: Decimal
    total_commission: Decimal
    currency: str
    captured_at: datetime


class AdmissionRiskDataPort(Protocol):
    async def collect(
        self,
        connection: BrokerConnection,
        *,
        instrument_uid: str | None,
        operations_from: datetime,
        request_ids: tuple[str, ...] = (),
    ) -> AdmissionRiskSnapshot: ...

    async def estimate_limit_order(
        self,
        connection: BrokerConnection,
        *,
        instrument_uid: str,
        side: OrderSide,
        quantity_lots: int,
        limit_price: Decimal,
    ) -> AdmissionOrderEstimate: ...
