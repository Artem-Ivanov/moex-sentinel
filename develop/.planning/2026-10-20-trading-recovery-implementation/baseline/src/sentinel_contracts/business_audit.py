"""SDK-neutral contracts for durable trading-process audit events."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class BusinessAuditLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class BusinessAuditStage(str, Enum):
    MARKET_DATA_RECEIVED = "MARKET_DATA_RECEIVED"
    TRADING_CYCLE_UPDATED = "TRADING_CYCLE_UPDATED"
    POSITION_RECONCILIATION_STARTED = "POSITION_RECONCILIATION_STARTED"
    POSITION_RECONCILED = "POSITION_RECONCILED"
    POSITION_RECONCILIATION_FAILED = "POSITION_RECONCILIATION_FAILED"
    POSITION_SNAPSHOT_RECEIVED = "POSITION_SNAPSHOT_RECEIVED"
    STRATEGY_CALCULATION_STARTED = "STRATEGY_CALCULATION_STARTED"
    STRATEGY_DECISION_MADE = "STRATEGY_DECISION_MADE"
    BROKER_INTENT_CREATED = "BROKER_INTENT_CREATED"
    BROKER_ORDER_DISPATCH_STARTED = "BROKER_ORDER_DISPATCH_STARTED"
    BROKER_ORDER_DISPATCH_RESULT = "BROKER_ORDER_DISPATCH_RESULT"
    BROKER_ORDER_TERMINAL_STATE = "BROKER_ORDER_TERMINAL_STATE"
    POSITION_STATE_UPDATED = "POSITION_STATE_UPDATED"
    TRADING_STEP_COMPLETED = "TRADING_STEP_COMPLETED"
    TRADING_STEP_FAILED = "TRADING_STEP_FAILED"
    AUTOMATION_MOVED_TO_HOLD = "AUTOMATION_MOVED_TO_HOLD"
    AUDIT_DELIVERY_RETRY_SCHEDULED = "AUDIT_DELIVERY_RETRY_SCHEDULED"
    WORKER_RECOVERY_STARTED = "WORKER_RECOVERY_STARTED"
    WORKER_RECOVERY_COMPLETED = "WORKER_RECOVERY_COMPLETED"
    CORE_BATCH_PUBLISH_STARTED = "CORE_BATCH_PUBLISH_STARTED"
    CORE_BATCH_PUBLISH_COMPLETED = "CORE_BATCH_PUBLISH_COMPLETED"
    CORE_BATCH_PUBLISH_FAILED = "CORE_BATCH_PUBLISH_FAILED"


class BusinessAuditEvent(PositionalModel):
    model_config = ConfigDict(frozen=True)
    event_id: str
    process_id: str
    parent_process_id: str | None
    automation_id: str
    broker_id: str
    account_id: str
    instrument_id: str
    level: BusinessAuditLevel
    stage: BusinessAuditStage
    message: str
    data: dict[str, Any]
    occurred_at: datetime
    critical: bool


class PublishAuditResult(PositionalModel):
    model_config = ConfigDict(frozen=True)
    accepted_event_ids: tuple[str, ...]
