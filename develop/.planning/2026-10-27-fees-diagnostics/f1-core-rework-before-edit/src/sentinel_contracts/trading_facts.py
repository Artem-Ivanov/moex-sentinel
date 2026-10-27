"""Strict typed Worker-to-Core runtime trading-fact contract."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import AfterValidator, BeforeValidator, ConfigDict, Field, PlainSerializer, model_validator

from sentinel_contracts.base import PositionalModel
from sentinel_contracts.broker_execution import BrokerOrderStatus, OrderSide
from sentinel_contracts.time import require_utc_millisecond
from sentinel_contracts.trading import AutomationState, DecisionKind
from sentinel_contracts.trading_admission import PreRpcAbortProof

MillisecondUtc = Annotated[
    datetime,
    AfterValidator(require_utc_millisecond),
    PlainSerializer(
        lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        return_type=str,
        when_used="json",
    ),
]


class StrictFrozenModel(PositionalModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


def _reject_boolean_quota(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("Order quota percent must be a decimal number.")  # noqa: TRY004
    return value


def _validate_quota(value: Decimal) -> Decimal:
    if not value.is_finite() or value <= 0 or value > 100:
        raise ValueError("Order quota percent must be finite and between 0 and 100.")
    _, digits, exponent = value.as_tuple()
    trailing_zeroes = 0
    while trailing_zeroes < len(digits) and digits[-trailing_zeroes - 1] == 0:
        trailing_zeroes += 1
    fractional_digits = max(0, -exponent - trailing_zeroes)
    if fractional_digits > 9:
        raise ValueError("Order quota percent must have at most 9 decimal places.")
    return value


OrderQuotaPercent = Annotated[
    Decimal,
    BeforeValidator(_reject_boolean_quota),
    AfterValidator(_validate_quota),
    PlainSerializer(str, return_type=str, when_used="json"),
]


class FactKind(StrEnum):
    AUTOMATION_STATE_CHANGED = "AUTOMATION_STATE_CHANGED"
    TRADE_DECISION_RECORDED = "TRADE_DECISION_RECORDED"
    BROKER_ORDER_RECORDED = "BROKER_ORDER_RECORDED"
    BROKER_ORDER_STATE_CHANGED = "BROKER_ORDER_STATE_CHANGED"
    TRADE_EXECUTION_RECORDED = "TRADE_EXECUTION_RECORDED"
    POSITION_CYCLE_UPDATED = "POSITION_CYCLE_UPDATED"
    POSITION_LOT_OPENED = "POSITION_LOT_OPENED"
    EXECUTION_LOT_ALLOCATED = "EXECUTION_LOT_ALLOCATED"
    TRADE_AUDIT_RECORDED = "TRADE_AUDIT_RECORDED"


class FactPositionCycleState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class FactOrderIntentKind(StrEnum):
    OPEN = "OPEN"
    BUY_MORE = "BUY_MORE"
    SELL_PART = "SELL_PART"
    SELL_ALL = "SELL_ALL"


class FactBrokerOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


FactBrokerOrderStatus = BrokerOrderStatus


class FactExecutionSource(StrEnum):
    BROKER_FILL = "BROKER_FILL"


class PositionLotSource(StrEnum):
    BROKER_EXECUTION = "BROKER_EXECUTION"
    BROKER_POSITION_BOOTSTRAP = "BROKER_POSITION_BOOTSTRAP"


class FactTradeAuditLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class AutomationStateChangedPayload(StrictFrozenModel):
    state: AutomationState
    suspended_from_state: AutomationState | None
    hold_reason: str | None
    closed_at: MillisecondUtc | None


class TradeDecisionRecordedPayload(StrictFrozenModel):
    decision_id: UUID
    process_id: UUID | None
    position_cycle_id: UUID | None
    instrument_id: UUID
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
    decided_at: MillisecondUtc
    created_at: MillisecondUtc


class BrokerOrderAggregatePayload(StrictFrozenModel):
    order_id: UUID
    decision_id: UUID
    position_cycle_id: UUID | None
    instrument_id: UUID
    idempotency_key: str
    external_order_id: str | None
    intent_kind: FactOrderIntentKind
    side: OrderSide
    order_type: FactBrokerOrderType
    state: FactBrokerOrderStatus
    quantity_lots: int
    limit_price: Decimal | None
    requested_amount: Decimal
    executed_amount: Decimal
    estimated_commission: Decimal
    executed_commission: Decimal
    strategy_snapshot: dict[str, object]
    dispatch_started_at: MillisecondUtc | None
    broker_responded_at: MillisecondUtc | None
    executed_at: MillisecondUtc | None
    terminal_at: MillisecondUtc | None
    updated_at: MillisecondUtc


class BrokerOrderRecordedPayload(BrokerOrderAggregatePayload):
    created_at: MillisecondUtc


class BrokerOrderStateChangedPayload(BrokerOrderAggregatePayload):
    order_event_id: UUID
    broker_order_id: UUID
    from_state: FactBrokerOrderStatus | None
    to_state: FactBrokerOrderStatus
    safe_reason: str
    safe_message: str = Field(max_length=1000)
    occurred_at: MillisecondUtc
    created_at: MillisecondUtc
    pre_rpc_abort_proof: PreRpcAbortProof | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_pre_rpc_abort(self):
        proof = self.pre_rpc_abort_proof
        if proof is None:
            return self
        if (
            str(proof.idempotency_key) != self.idempotency_key
            or self.state is not FactBrokerOrderStatus.CANCELLED
            or self.to_state is not FactBrokerOrderStatus.CANCELLED
            or self.external_order_id is not None
            or self.broker_responded_at is not None
            or self.quantity_lots <= 0
            or self.requested_amount <= 0
            or self.executed_amount != 0
            or self.executed_commission != 0
            or self.executed_at is not None
        ):
            raise ValueError("Pre-RPC abort proof requires a matching, zero-fill cancelled order.")
        return self


class TradeExecutionRecordedPayload(StrictFrozenModel):
    execution_id: UUID
    broker_order_id: UUID
    position_cycle_id: UUID
    instrument_id: UUID
    external_execution_id: str | None
    side: OrderSide
    executed_lots: int
    price: Decimal
    value: Decimal
    broker_commission: Decimal
    other_fees: Decimal
    currency: str
    source: FactExecutionSource
    executed_at: MillisecondUtc
    created_at: MillisecondUtc


class PositionCycleUpdatedPayload(StrictFrozenModel):
    position_cycle_id: UUID
    instrument_id: UUID
    state: FactPositionCycleState
    quantity_lots: int
    average_entry_price: Decimal
    invested_amount: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    net_pnl: Decimal
    accumulated_commissions: Decimal
    opened_at: MillisecondUtc
    closed_at: MillisecondUtc | None
    created_at: MillisecondUtc
    updated_at: MillisecondUtc


class PositionLotOpenedPayload(StrictFrozenModel):
    position_lot_id: UUID
    position_cycle_id: UUID
    buy_execution_id: UUID | None
    source: PositionLotSource
    original_lots: int
    remaining_lots: int
    entry_price: Decimal
    entry_commission: Decimal
    opened_at: MillisecondUtc
    created_at: MillisecondUtc
    updated_at: MillisecondUtc


class ExecutionLotAllocatedPayload(StrictFrozenModel):
    allocation_id: UUID
    position_cycle_id: UUID
    sell_execution_id: UUID
    position_lot_id: UUID
    allocated_lots: int
    remaining_lots_after: int
    entry_value: Decimal
    exit_value: Decimal
    entry_commission: Decimal
    exit_commission: Decimal
    realized_pnl: Decimal
    allocated_at: MillisecondUtc
    created_at: MillisecondUtc


class TradeAuditRecordedPayload(StrictFrozenModel):
    audit_event_id: UUID
    process_id: UUID
    parent_process_id: UUID | None
    decision_id: UUID | None
    broker_order_id: UUID | None
    execution_id: UUID | None
    instrument_id: UUID
    level: FactTradeAuditLevel
    stage: str
    safe_message: str = Field(max_length=1000)
    data: dict[str, object]
    occurred_at: MillisecondUtc
    created_at: MillisecondUtc
    critical: bool


FactPayload: TypeAlias = (
    AutomationStateChangedPayload
    | TradeDecisionRecordedPayload
    | BrokerOrderRecordedPayload
    | BrokerOrderStateChangedPayload
    | TradeExecutionRecordedPayload
    | PositionCycleUpdatedPayload
    | PositionLotOpenedPayload
    | ExecutionLotAllocatedPayload
    | TradeAuditRecordedPayload
)


class FactEnvelopeBase(StrictFrozenModel):
    event_id: UUID
    user_broker_id: UUID
    automation_id: UUID
    sequence_number: int = Field(gt=0)
    expected_revision: int = Field(gt=0)
    safe_message: str = Field(max_length=1000)
    occurred_at: MillisecondUtc


class AutomationStateChangedEnvelope(FactEnvelopeBase):
    fact_kind: Literal[FactKind.AUTOMATION_STATE_CHANGED]
    payload: AutomationStateChangedPayload


class TradeDecisionRecordedEnvelope(FactEnvelopeBase):
    fact_kind: Literal[FactKind.TRADE_DECISION_RECORDED]
    payload: TradeDecisionRecordedPayload


class BrokerOrderRecordedEnvelope(FactEnvelopeBase):
    fact_kind: Literal[FactKind.BROKER_ORDER_RECORDED]
    payload: BrokerOrderRecordedPayload


class BrokerOrderStateChangedEnvelope(FactEnvelopeBase):
    fact_kind: Literal[FactKind.BROKER_ORDER_STATE_CHANGED]
    payload: BrokerOrderStateChangedPayload


class TradeExecutionRecordedEnvelope(FactEnvelopeBase):
    fact_kind: Literal[FactKind.TRADE_EXECUTION_RECORDED]
    payload: TradeExecutionRecordedPayload


class PositionCycleUpdatedEnvelope(FactEnvelopeBase):
    fact_kind: Literal[FactKind.POSITION_CYCLE_UPDATED]
    payload: PositionCycleUpdatedPayload


class PositionLotOpenedEnvelope(FactEnvelopeBase):
    fact_kind: Literal[FactKind.POSITION_LOT_OPENED]
    payload: PositionLotOpenedPayload


class ExecutionLotAllocatedEnvelope(FactEnvelopeBase):
    fact_kind: Literal[FactKind.EXECUTION_LOT_ALLOCATED]
    payload: ExecutionLotAllocatedPayload


class TradeAuditRecordedEnvelope(FactEnvelopeBase):
    fact_kind: Literal[FactKind.TRADE_AUDIT_RECORDED]
    payload: TradeAuditRecordedPayload


FactEnvelope: TypeAlias = Annotated[
    AutomationStateChangedEnvelope
    | TradeDecisionRecordedEnvelope
    | BrokerOrderRecordedEnvelope
    | BrokerOrderStateChangedEnvelope
    | TradeExecutionRecordedEnvelope
    | PositionCycleUpdatedEnvelope
    | PositionLotOpenedEnvelope
    | ExecutionLotAllocatedEnvelope
    | TradeAuditRecordedEnvelope,
    Field(discriminator="fact_kind"),
]


class FactBatchRequest(StrictFrozenModel):
    facts: list[FactEnvelope]


class BrokerPositionBootstrap(StrictFrozenModel):
    position_cycle_id: UUID
    position_lot_id: UUID
    quantity_lots: int = Field(gt=0)
    average_price: Decimal = Field(gt=0)
    invested_amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=1)
    observed_at: MillisecondUtc


class AutomationCommand(StrictFrozenModel):
    automation_id: UUID
    user_broker_id: UUID
    broker_id: UUID
    account_id: str = Field(min_length=1)
    external_instrument_id: str = Field(min_length=1)
    instrument_id: UUID
    currency: str = Field(min_length=1)
    lot_size: int = Field(gt=0)
    min_price_increment: Decimal = Field(gt=0)
    state: AutomationState
    revision: int = Field(gt=0)
    last_sequence_number: int = Field(ge=0)
    resume_requested: bool
    bootstrap: BrokerPositionBootstrap | None = None
    settings_revision: int = Field(ge=0, default=0)
    order_quota_percent: OrderQuotaPercent | None = None


class AutomationStatus(StrictFrozenModel):
    automation_id: UUID
    user_broker_id: UUID
    state: AutomationState
    revision: int = Field(gt=0)
    last_sequence_number: int = Field(ge=0)
    resume_requested: bool
    settings_revision: int = Field(ge=0, default=0)
    order_quota_percent: OrderQuotaPercent | None = None


class AutomationStatusesResult(StrictFrozenModel):
    automations: tuple[AutomationStatus, ...]
    missing_automation_ids: tuple[UUID, ...] = ()
    commission_statuses: tuple["AutomationCommissionStatus", ...] = ()


class AutomationCommissionStatus(StrictFrozenModel):
    intent_id: UUID
    automation_id: UUID
    user_broker_id: UUID
    currency: str
    commission_status: Literal["KNOWN", "MISSING", "UNKNOWN"]
    effective_broker_commission: Decimal | None = None

    @model_validator(mode="after")
    def validate_fee_projection(self) -> "AutomationCommissionStatus":
        currency = self.currency.strip().upper()
        if not currency or len(currency) > 8:
            raise ValueError("Commission currency is invalid.")
        object.__setattr__(self, "currency", currency)
        amount = self.effective_broker_commission
        if self.commission_status == "KNOWN":
            if amount is None or not amount.is_finite() or amount < 0 or amount >= Decimal("1e19"):
                raise ValueError("Known commission must have a finite supported amount.")
            try:
                if amount.quantize(Decimal("1e-9")) != amount:
                    raise ValueError("Known commission has more than 9 decimal places.")
            except ArithmeticError as error:
                raise ValueError("Known commission is outside the supported range.") from error
        elif amount is not None:
            raise ValueError("Non-known commission status cannot expose an amount.")
        return self


class FactIngressErrorCode(StrEnum):
    AUTOMATION_NOT_FOUND = "AUTOMATION_NOT_FOUND"
    CROSS_SCOPE_RELATION = "CROSS_SCOPE_RELATION"
    AUTOMATION_SEQUENCE_GAP = "AUTOMATION_SEQUENCE_GAP"
    AUTOMATION_SEQUENCE_CONFLICT = "AUTOMATION_SEQUENCE_CONFLICT"
    AUTOMATION_REVISION_CONFLICT = "AUTOMATION_REVISION_CONFLICT"
    FACT_ID_CONFLICT = "FACT_ID_CONFLICT"
    FACT_LINEAGE_CONFLICT = "FACT_LINEAGE_CONFLICT"
    INVALID_FACT_STATE = "INVALID_FACT_STATE"
    TEMPORARY_CORE_FAILURE = "TEMPORARY_CORE_FAILURE"


class FactGroupAcknowledgement(StrictFrozenModel):
    automation_id: UUID
    accepted_through_sequence: int = Field(ge=0)
    current_revision: int = Field(gt=0)
    accepted_event_ids: tuple[UUID, ...]


class FactGroupFailure(StrictFrozenModel):
    automation_id: UUID
    code: FactIngressErrorCode
    event_ids: tuple[UUID, ...]
    sequence_numbers: tuple[int, ...]
    retryable: bool


class FactBatchResult(StrictFrozenModel):
    results: tuple[FactGroupAcknowledgement, ...]
    failures: tuple[FactGroupFailure, ...] = ()
