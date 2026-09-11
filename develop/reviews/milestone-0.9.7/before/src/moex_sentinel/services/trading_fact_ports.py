"""Application ports for the Core trading fact schema."""

from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from moex_sentinel.domain.trading_facts import (
    AutomationEnvelopeDraft,
    BrokerAccountFeeProfileDraft,
    BrokerOrderDraft,
    BrokerOrderEventDraft,
    ExecutionLotAllocationDraft,
    PositionCycleDraft,
    PositionLotDraft,
    PositionValuationSnapshotDraft,
    TradeAuditEventDraft,
    TradeDecisionDraft,
    TradeExecutionDraft,
    TradingAutomationDraft,
)
from sentinel_contracts.trading import AutomationState


class AutomationFactsPort(Protocol):
    def create(
        self,
        user_broker_id: str,
        automation: TradingAutomationDraft,
    ) -> TradingAutomationDraft: ...

    def get(self, user_broker_id: str, automation_id: str) -> TradingAutomationDraft: ...

    def compare_and_set_state(
        self,
        user_broker_id: str,
        automation_id: str,
        *,
        expected_revision: int,
        state: AutomationState,
        hold_reason: str | None,
        closed_at: datetime | None,
    ) -> TradingAutomationDraft: ...

    def accept_state_fact(
        self,
        user_broker_id: str,
        automation_id: str,
        *,
        expected_revision: int,
        expected_sequence: int,
        sequence_number: int,
        state: AutomationState,
        suspended_from_state: AutomationState | None,
        hold_reason: str | None,
        closed_at: datetime | None,
    ) -> TradingAutomationDraft: ...

    def accept_supporting_fact(
        self,
        user_broker_id: str,
        automation_id: str,
        *,
        expected_revision: int,
        expected_sequence: int,
        sequence_number: int,
    ) -> TradingAutomationDraft: ...


class OrderFactsPort(Protocol):
    def append_decision(self, user_broker_id: str, value: TradeDecisionDraft) -> TradeDecisionDraft: ...

    def append_order(self, user_broker_id: str, value: BrokerOrderDraft) -> BrokerOrderDraft: ...

    def append_order_event(self, user_broker_id: str, value: BrokerOrderEventDraft) -> BrokerOrderEventDraft: ...

    def append_execution(self, user_broker_id: str, value: TradeExecutionDraft) -> TradeExecutionDraft: ...

    def replace_order_aggregate(self, user_broker_id: str, value: BrokerOrderDraft) -> BrokerOrderDraft: ...

    def get_decision(self, user_broker_id: str, decision_id: str) -> TradeDecisionDraft: ...

    def get_order(self, user_broker_id: str, order_id: str) -> BrokerOrderDraft: ...

    def list_order_events(self, user_broker_id: str, order_id: str) -> tuple[BrokerOrderEventDraft, ...]: ...

    def list_executions(self, user_broker_id: str, order_id: str) -> tuple[TradeExecutionDraft, ...]: ...


class PositionLedgerPort(Protocol):
    def open_cycle(self, user_broker_id: str, value: PositionCycleDraft) -> PositionCycleDraft: ...

    def replace_cycle_aggregate(self, user_broker_id: str, value: PositionCycleDraft) -> PositionCycleDraft: ...

    def append_lot(self, user_broker_id: str, value: PositionLotDraft) -> PositionLotDraft: ...

    def append_allocation(
        self,
        user_broker_id: str,
        value: ExecutionLotAllocationDraft,
    ) -> ExecutionLotAllocationDraft: ...

    def append_allocation_and_decrement(
        self,
        user_broker_id: str,
        value: ExecutionLotAllocationDraft,
        *,
        expected_remaining_lots: int,
        remaining_lots_after: int,
    ) -> ExecutionLotAllocationDraft: ...

    def get_cycle(self, user_broker_id: str, cycle_id: str) -> PositionCycleDraft: ...

    def list_open_lots(self, user_broker_id: str, cycle_id: str) -> tuple[PositionLotDraft, ...]: ...

    def list_allocations(
        self,
        user_broker_id: str,
        cycle_id: str,
    ) -> tuple[ExecutionLotAllocationDraft, ...]: ...


class TradingAuditPort(Protocol):
    def append_audit(self, user_broker_id: str, value: TradeAuditEventDraft) -> TradeAuditEventDraft: ...

    def append_envelope(self, user_broker_id: str, value: AutomationEnvelopeDraft) -> AutomationEnvelopeDraft: ...

    def get_envelope_by_event_id(
        self,
        user_broker_id: str,
        event_id: str,
    ) -> AutomationEnvelopeDraft | None: ...

    def get_envelope_by_sequence(
        self,
        user_broker_id: str,
        automation_id: str,
        sequence_number: int,
    ) -> AutomationEnvelopeDraft | None: ...


class TradingAnalyticsPort(Protocol):
    def upsert_fee_profile(
        self,
        user_broker_id: str,
        value: BrokerAccountFeeProfileDraft,
    ) -> BrokerAccountFeeProfileDraft: ...

    def append_position_valuation(
        self,
        user_broker_id: str,
        value: PositionValuationSnapshotDraft,
    ) -> PositionValuationSnapshotDraft: ...


class TradingFactsUnitOfWorkPort(Protocol):
    @property
    def automations(self) -> AutomationFactsPort: ...

    @property
    def orders(self) -> OrderFactsPort: ...

    @property
    def positions(self) -> PositionLedgerPort: ...

    @property
    def audit(self) -> TradingAuditPort: ...

    @property
    def analytics(self) -> TradingAnalyticsPort: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
