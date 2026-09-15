"""Domain DTOs for trading_automaton storage-level payloads and records."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import ConfigDict, Field

from sentinel_contracts.base import PositionalModel


class AccountCommissionProfileKey(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_id: str
    account_id: str
    instrument_type: str
    currency: str


class AccountCommissionProfile(PositionalModel):
    model_config = ConfigDict(frozen=True)
    key: AccountCommissionProfileKey
    buy_rate: Decimal
    sell_rate: Decimal
    service_rate: Decimal
    deal_rate: Decimal
    source: str
    calculated_at: datetime
    valid_until: datetime


class LocalAutomationRecord(PositionalModel):
    model_config = ConfigDict(frozen=True)
    automation_id: str
    state: str
    revision: int
    last_sequence_number: int


class FactOutboxRecord(PositionalModel):
    model_config = ConfigDict(frozen=True)
    event_id: str
    user_broker_id: str
    automation_id: str
    sequence_number: int
    expected_revision: int
    fact_kind: str
    payload: dict[str, object]
    safe_message: str
    occurred_at: datetime
    delivery_state: str
    retry_count: int
    next_retry_at: datetime | None
    created_at: datetime
    updated_at: datetime


class LocalIntentRecord(PositionalModel):
    model_config = ConfigDict(frozen=True)
    idempotency_key: str
    automation_id: str
    kind: str
    side: str
    state: str
    quantity_lots: int
    limit_price: Decimal
    broker_order_id: str | None
    requested_amount: Decimal
    executed_amount: Decimal
    estimated_commission: Decimal
    executed_commission: Decimal
    executed_lots: int
    executed_price: Decimal
    execution_currency: str | None
    executed_at: datetime | None
    dispatch_started_at: datetime | None
    broker_responded_at: datetime | None
    terminal_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ActiveBuyIntentReservation(PositionalModel):
    model_config = ConfigDict(frozen=True)
    intent_id: str
    broker_id: str
    account_id: str
    currency: str
    amount: Decimal
    side: str
    state: str


class DecisionRecord(PositionalModel):
    model_config = ConfigDict(frozen=True)
    id: str
    process_id: str | None
    automation_id: str
    reason_code: str
    decision: str
    intent_id: str | None


class IntentBatchItem(PositionalModel):
    model_config = ConfigDict(frozen=True)
    idempotency_key: str
    kind: str
    side: str
    quantity_lots: int
    limit_price: Decimal


class DecisionBatchItem(PositionalModel):
    model_config = ConfigDict(frozen=True)
    automation_id: str
    broker_id: str
    account_id: str
    instrument_id: str
    instrument_type: str
    quantity_lots: int
    lot_size: int
    average_price: Decimal
    current_price: Decimal
    best_bid: Decimal
    best_ask: Decimal
    invested_amount: Decimal
    estimated_commission: Decimal
    decision: str
    reason_code: str
    decision_quantity_lots: int
    limit_price: Decimal | None
    strategy_snapshot: dict[str, Any]
    process_id: str | None
    intent: IntentBatchItem | None = None
    position_snapshot: dict[str, int | str] | None = None
    iteration_id: str | None = None
    cycle_state: dict[str, Any] | None = None
    indicators: dict[str, object] = Field(default_factory=dict)
    currency: str = "RUB"
    position_snapshot_at: datetime | None = None


class BatchPersistResult(PositionalModel):
    model_config = ConfigDict(frozen=True)
    decisions: tuple[DecisionRecord, ...]
    intents: tuple[LocalIntentRecord, ...]


class IntentHistory(PositionalModel):
    model_config = ConfigDict(frozen=True)
    last_buy_price: Decimal
    completed_partial_sell_steps: int
    actual_commissions: Decimal
    total_bought_lots: int
    buy_commissions: Decimal


class TradeLotRecord(PositionalModel):
    model_config = ConfigDict(frozen=True)
    id: str
    automation_id: str
    source_intent_id: str | None
    source: str
    original_lots: int
    remaining_lots: int
    entry_price: Decimal
    entry_commission: Decimal
    opened_at: datetime
    source_intent_kind: str | None = None


class ExecutionFinalization(PositionalModel):
    model_config = ConfigDict(frozen=True)
    """All durable facts produced by one terminal broker response."""

    intent_id: str
    automation_id: str
    broker_id: str
    account_id: str
    instrument_id: str
    side: str
    quantity_lots: int
    requested_price: Decimal
    currency: str
    state: str
    occurred_at: datetime
    broker_order_id: str | None
    requested_amount: Decimal
    executed_amount: Decimal
    estimated_commission: Decimal
    executed_commission: Decimal
    executed_lots: int
    executed_price: Decimal
    executed_at: datetime | None
    terminal_at: datetime
    lot_size: int
    process_id: str | None


class ExecutionFinalizationResult(PositionalModel):
    model_config = ConfigDict(frozen=True)
    intent: LocalIntentRecord | None
    applied: bool
    authoritative_position_snapshot: dict[str, int | str] | None


class TradingCycleState(PositionalModel):
    model_config = ConfigDict(frozen=True)
    automation_id: str
    pending_low: Decimal | None
    last_buy_candle_at: datetime | None
    sell_armed: bool
    last_sell_price: Decimal | None
    updated_at: datetime
