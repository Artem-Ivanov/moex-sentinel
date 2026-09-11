"""Typed values for the user-broker-scoped Core trading fact schema."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel
from sentinel_contracts.broker_execution import OrderSide
from sentinel_contracts.trading import AutomationState, DecisionKind


class FrozenFactModel(PositionalModel):
    """Strict immutable value crossing the persistence boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PositionCycleState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class OrderIntentKind(StrEnum):
    OPEN = "OPEN"
    BUY_MORE = "BUY_MORE"
    SELL_PART = "SELL_PART"
    SELL_ALL = "SELL_ALL"


class BrokerOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class BrokerOrderStatus(StrEnum):
    CREATED = "CREATED"
    DISPATCH_PENDING = "DISPATCH_PENDING"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNCERTAIN = "UNCERTAIN"
    FAILED = "FAILED"


class ExecutionSource(StrEnum):
    BROKER_FILL = "BROKER_FILL"


class PositionLotSource(StrEnum):
    BROKER_EXECUTION = "BROKER_EXECUTION"
    BROKER_POSITION_BOOTSTRAP = "BROKER_POSITION_BOOTSTRAP"


class TradeAuditLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class TradingFactErrorCode(StrEnum):
    NOT_FOUND = "TRADING_FACT_NOT_FOUND"
    CROSS_SCOPE = "CROSS_SCOPE_RELATION"
    FACT_ID_CONFLICT = "FACT_ID_CONFLICT"
    ORDER_IDEMPOTENCY_CONFLICT = "ORDER_IDEMPOTENCY_CONFLICT"
    SEQUENCE_CONFLICT = "AUTOMATION_SEQUENCE_CONFLICT"
    REVISION_CONFLICT = "AUTOMATION_REVISION_CONFLICT"
    INVALID_STATE = "INVALID_FACT_STATE"


class TradingFactPersistenceError(RuntimeError):
    """Safe stable persistence error without fact payloads or driver text."""

    def __init__(
        self,
        code: TradingFactErrorCode,
        *,
        entity_type: str,
        constraint_name: str | None = None,
    ) -> None:
        self.code = code
        self.entity_type = entity_type
        self.constraint_name = constraint_name
        detail = f" ({constraint_name})" if constraint_name is not None else ""
        super().__init__(f"{code.value}: {entity_type}{detail}")


class TradingAutomationDraft(FrozenFactModel):
    id: str
    user_broker_id: str
    instrument_id: str
    state: AutomationState
    suspended_from_state: AutomationState | None
    hold_reason: str | None
    revision: int
    last_sequence_number: int
    resume_requested: bool
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    bootstrap_position_cycle_id: str | None = None
    bootstrap_position_lot_id: str | None = None
    bootstrap_quantity_lots: int | None = None
    bootstrap_average_price: Decimal | None = None
    bootstrap_invested_amount: Decimal | None = None
    bootstrap_currency: str | None = None
    bootstrap_observed_at: datetime | None = None


class PositionCycleDraft(FrozenFactModel):
    id: str
    user_broker_id: str
    automation_id: str
    instrument_id: str
    state: PositionCycleState
    quantity_lots: int
    average_entry_price: Decimal
    invested_amount: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    net_pnl: Decimal
    accumulated_commissions: Decimal
    opened_at: datetime
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TradeDecisionDraft(FrozenFactModel):
    id: str
    fact_id: str
    process_id: str | None
    user_broker_id: str
    automation_id: str
    position_cycle_id: str | None
    instrument_id: str
    quantity_lots: int
    lot_size: int
    average_price: Decimal
    invested_amount: Decimal
    current_price: Decimal
    best_bid: Decimal
    best_ask: Decimal
    indicators: dict[str, object]
    estimated_commission: Decimal
    decision: DecisionKind
    reason_code: str
    requested_quantity_lots: int
    limit_price: Decimal | None
    strategy_snapshot: dict[str, object]
    decided_at: datetime
    created_at: datetime


class BrokerOrderDraft(FrozenFactModel):
    id: str
    fact_id: str
    user_broker_id: str
    automation_id: str
    decision_id: str
    position_cycle_id: str | None
    instrument_id: str
    idempotency_key: str
    external_order_id: str | None
    intent_kind: OrderIntentKind
    side: OrderSide
    order_type: BrokerOrderType
    state: BrokerOrderStatus
    quantity_lots: int
    limit_price: Decimal | None
    requested_amount: Decimal
    executed_amount: Decimal
    estimated_commission: Decimal
    executed_commission: Decimal
    strategy_snapshot: dict[str, object]
    created_at: datetime
    dispatch_started_at: datetime | None
    broker_responded_at: datetime | None
    executed_at: datetime | None
    terminal_at: datetime | None
    updated_at: datetime


class BrokerOrderEventDraft(FrozenFactModel):
    id: str
    fact_id: str
    user_broker_id: str
    automation_id: str
    broker_order_id: str
    from_state: BrokerOrderStatus | None
    to_state: BrokerOrderStatus
    safe_reason: str
    safe_message: str
    occurred_at: datetime
    created_at: datetime


class TradeExecutionDraft(FrozenFactModel):
    id: str
    fact_id: str
    user_broker_id: str
    automation_id: str
    broker_order_id: str
    position_cycle_id: str
    instrument_id: str
    external_execution_id: str | None
    side: OrderSide
    executed_lots: int
    price: Decimal
    value: Decimal
    broker_commission: Decimal
    other_fees: Decimal
    currency: str
    source: ExecutionSource
    executed_at: datetime
    created_at: datetime


class PositionLotDraft(FrozenFactModel):
    id: str
    user_broker_id: str
    automation_id: str
    position_cycle_id: str
    buy_execution_id: str | None
    source: PositionLotSource
    original_lots: int
    remaining_lots: int
    entry_price: Decimal
    entry_commission: Decimal
    opened_at: datetime
    created_at: datetime
    updated_at: datetime


class ExecutionLotAllocationDraft(FrozenFactModel):
    id: str
    user_broker_id: str
    automation_id: str
    position_cycle_id: str
    sell_execution_id: str
    position_lot_id: str
    allocated_lots: int
    entry_value: Decimal
    exit_value: Decimal
    entry_commission: Decimal
    exit_commission: Decimal
    realized_pnl: Decimal
    allocated_at: datetime
    created_at: datetime


class TradeAuditEventDraft(FrozenFactModel):
    event_id: str
    process_id: str
    parent_process_id: str | None
    user_broker_id: str
    automation_id: str
    decision_id: str | None
    broker_order_id: str | None
    execution_id: str | None
    instrument_id: str
    level: TradeAuditLevel
    stage: str
    safe_message: str
    data: dict[str, object]
    occurred_at: datetime
    created_at: datetime
    critical: bool


class AutomationEnvelopeDraft(FrozenFactModel):
    event_id: str
    automation_id: str
    user_broker_id: str
    sequence_number: int
    expected_revision: int
    fact_kind: str
    safe_message: str
    payload: dict[str, object]
    occurred_at: datetime
    received_at: datetime


class BrokerAccountFeeProfileDraft(FrozenFactModel):
    id: str
    user_broker_id: str
    instrument_type: str
    currency: str
    buy_rate: Decimal
    sell_rate: Decimal
    service_rate: Decimal
    deal_rate: Decimal
    source: str
    calculated_at: datetime
    valid_until: datetime
    created_at: datetime
    updated_at: datetime


class PositionValuationSnapshotDraft(FrozenFactModel):
    id: str
    user_broker_id: str
    automation_id: str
    position_cycle_id: str
    instrument_id: str
    quantity_lots: int
    average_price: Decimal
    current_price: Decimal
    invested_amount: Decimal
    market_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    net_pnl: Decimal
    actual_commissions: Decimal
    source: str
    captured_at: datetime
    created_at: datetime
