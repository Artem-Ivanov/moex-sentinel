"""Domain DTOs shared by trading-automaton services."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict

from sentinel_contracts.analytics import AdaptiveThresholds, MarketIndicators
from sentinel_contracts.base import PositionalModel
from sentinel_contracts.broker_execution import BrokerPosition, OrderSide
from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.config import StrategySettings
from trading_automaton.domain.storage_dtos import (
    AccountCommissionProfileKey,
    IntentHistory,
    TradeLotRecord,
    TradingCycleState,
)


class ActiveCashReservation(PositionalModel):
    model_config = ConfigDict(frozen=True)
    intent_id: str
    broker_id: str
    account_id: str
    currency: str
    amount: Decimal
    side: str
    state: str


class _Reservation(PositionalModel):
    model_config = ConfigDict(frozen=True)
    account_id: str
    currency: str
    amount: Decimal


class CommittedCashReservation(PositionalModel):
    model_config = ConfigDict(frozen=True)
    account_id: str
    currency: str
    intent_id: str
    amount: Decimal


class CommissionQuote(PositionalModel):
    model_config = ConfigDict(frozen=True)
    order_amount: Decimal
    total_commission: Decimal
    service_commission: Decimal = Decimal()
    deal_commission: Decimal = Decimal()

    def rate(self, amount: Decimal) -> Decimal:
        if self.order_amount <= 0 or amount < 0:
            raise ValueError("Commission quote amounts must be positive.")
        return amount / self.order_amount


class CommissionSchedule(PositionalModel):
    model_config = ConfigDict(frozen=True)

    buy_rate: Decimal
    sell_rate: Decimal

    def estimate(self, side: str, order_amount: Decimal) -> Decimal:
        if order_amount < 0:
            raise ValueError("Order amount must be non-negative.")
        if side == "BUY":
            return order_amount * self.buy_rate
        if side == "SELL":
            return order_amount * self.sell_rate
        raise ValueError(f"Unsupported order side: {side}")


class CommissionRefreshRequest(PositionalModel):
    model_config = ConfigDict(frozen=True)
    key: AccountCommissionProfileKey
    instrument_id: str
    quantity_lots: int
    price: Decimal


class AuditDeliveryResult(PositionalModel):
    model_config = ConfigDict(frozen=True)
    delivered: int
    failed: int


class DecisionContext(PositionalModel):
    model_config = ConfigDict(frozen=True)
    core_available: bool
    has_active_intent: bool
    quantity_lots: int
    lot_size: int
    average_price: Decimal
    current_price: Decimal
    best_bid: Decimal
    best_ask: Decimal
    last_buy_price: Decimal
    invested_amount: Decimal
    commission_schedule: CommissionSchedule
    buy_commission_per_lot: Decimal
    averaging_step_percent: Decimal
    minimum_net_profit_percent: Decimal
    completed_partial_sell_steps: int
    settings: StrategySettings = StrategySettings()
    currency: str = "RUB"
    min_price_increment: Decimal = Decimal()
    lots: tuple[TradeLotRecord, ...] = ()
    cycle: TradingCycleState | None = None
    indicators: MarketIndicators | None = None
    free_cash: Decimal = Decimal()
    reserved_cash: Decimal = Decimal()
    available_free_cash: Decimal = Decimal()
    required_order_cash: Decimal = Decimal()


class TradeDecision(PositionalModel):
    model_config = ConfigDict(frozen=True)
    kind: DecisionKind
    quantity_lots: int
    limit_price: Decimal | None
    reason_code: str
    target_lot_id: str | None = None


class OrderBookValidationResult(PositionalModel):
    model_config = ConfigDict(frozen=True)
    valid: bool
    reason_code: str | None
    age_ms: int


class DispatchRequest(PositionalModel):
    model_config = ConfigDict(frozen=True)
    idempotency_key: str
    account_id: str
    instrument_id: str
    side: OrderSide
    quantity_lots: int
    limit_price: Decimal
    instrument_type: str = "SHARE"
    automation_id: str = ""
    lot_size: int = 1
    process_id: str | None = None
    broker_id: str = ""
    reservation_currency: str = ""
    required_cash: Decimal = Decimal()

    market_valid_until: datetime | None = None


class PostCommitCleanupContext(PositionalModel):
    model_config = ConfigDict(frozen=True)
    intent_id: str
    terminal_state: str
    request: DispatchRequest


class PositionWorkItem(PositionalModel):
    model_config = ConfigDict(frozen=True)
    command: AutomationCommand
    has_active_intent: bool
    commission_profile_available: bool = True
    instrument_type: str = "SHARE"
    state: HydratedPositionState | None = None
    snapshot_at: datetime | None = None


class PositionEvaluationResult(PositionalModel):
    model_config = ConfigDict(frozen=True)
    decision: TradeDecision
    state: HydratedPositionState | None
    estimated_commission: Decimal


class PreparedDecision(PositionalModel):
    model_config = ConfigDict(frozen=True)
    command: AutomationCommand
    snapshot_id: str
    snapshot_at: datetime
    evaluation: PositionEvaluationResult

    @property
    def decision(self) -> TradeDecision:
        return self.evaluation.decision


class CalculatedPosition(PositionalModel):
    model_config = ConfigDict(frozen=True)
    quantity_units: int
    average_price: Decimal
    invested_amount: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    actual_commissions: Decimal
    net_pnl: Decimal


class PositionConsistencyResult(PositionalModel):
    model_config = ConfigDict(frozen=True)
    consistent: bool
    lots: tuple[object, ...]
    reason_code: str | None = None


class HydratedPositionState(PositionalModel):
    model_config = ConfigDict(frozen=True)
    position: BrokerPosition
    history: IntentHistory
    lots: tuple[TradeLotRecord, ...]
    cycle: TradingCycleState
    indicators: MarketIndicators
    has_active_intent: bool = False
    realized_pnl: Decimal = Decimal()
    process_id: str | None = None


class SlaResult(PositionalModel):
    model_config = ConfigDict(frozen=True)
    code: str
    latency_ms: float


class ReconciliationResult(PositionalModel):
    model_config = ConfigDict(frozen=True)
    resolved: int = 0
    unresolved: int = 0


class _CacheEntry(PositionalModel):
    model_config = ConfigDict(frozen=True)
    value: AdaptiveThresholds
    loaded_at: datetime


class BatchTickResult(PositionalModel):
    model_config = ConfigDict(frozen=True)
    persisted: object
    sla: tuple[SlaResult, ...]
