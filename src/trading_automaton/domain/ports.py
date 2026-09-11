"""Shared domain-facing ports used by three or more trading services."""

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sentinel_contracts.business_audit import BusinessAuditStage
from trading_automaton.domain.storage_dtos import LocalIntentRecord


class IntentUpdatePort(Protocol):
    def update_intent(
        self,
        idempotency_key: str,
        *,
        state: str,
        occurred_at: datetime,
        broker_order_id: str | None = None,
        requested_amount: Decimal | None = None,
        executed_amount: Decimal | None = None,
        estimated_commission: Decimal | None = None,
        executed_commission: Decimal | None = None,
        executed_lots: int | None = None,
        executed_price: Decimal | None = None,
        executed_at: datetime | None = None,
        dispatch_started_at: datetime | None = None,
        broker_responded_at: datetime | None = None,
        terminal_at: datetime | None = None,
        process_id: str | None = None,
    ) -> LocalIntentRecord: ...


class OrderStageAuditPort(Protocol):
    def record_order_stage(
        self,
        *,
        stage: BusinessAuditStage,
        process_id: str,
        automation_id: str,
        broker_id: str,
        account_id: str,
        instrument_id: str,
        message: str,
        critical: bool = False,
        **data: object,
    ) -> None: ...
