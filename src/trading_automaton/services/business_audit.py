"""Safe construction of durable worker business-process audit events."""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol, TypedDict
from uuid import uuid4

from sentinel_contracts.business_audit import (
    BusinessAuditEvent,
    BusinessAuditLevel,
    BusinessAuditStage,
)

_DECISION_FIELDS = {
    "decision",
    "reason_code",
    "current_price",
    "best_bid",
    "best_ask",
    "averaging_step_percent",
    "minimum_net_profit_percent",
    "order_book_age_ms",
    "trading_status",
    "free_cash",
    "reserved_cash",
    "required_order_cash",
    "estimated_buy_commission",
    "available_after_reserve",
    "cycle_pending_low_before",
    "cycle_pending_low_after",
}
_ORDER_FIELDS = {
    "side",
    "quantity_lots",
    "limit_price",
    "broker_order_state",
    "broker_order_id",
    "executed_lots",
    "executed_price",
    "executed_commission",
    "sdk_dispatch_latency_ms",
    "reason_code",
    "exception_type",
    "broker_error_code",
    "broker_error_details",
    "reconciliation_error",
    "timestamp_source",
    "position_snapshot",
    "recent_operations",
}
_RECONCILIATION_FIELDS = {
    "broker_quantity_lots",
    "worker_quantity_lots",
    "reason_code",
}


class BusinessAuditRepositoryPort(Protocol):
    def append_audit_events(self, events: list[BusinessAuditEvent]) -> None: ...


class _AuditCommon(TypedDict):
    process_id: str
    parent_process_id: None
    automation_id: str
    broker_id: str
    account_id: str
    instrument_id: str
    level: BusinessAuditLevel
    occurred_at: datetime
    critical: bool


class BusinessAuditService:
    def __init__(
        self,
        repository: BusinessAuditRepositoryPort,
        *,
        now: Callable[[], datetime],
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._repository = repository
        self._now = now
        self._id_factory = id_factory

    def record_decision_process(
        self,
        *,
        process_id: str,
        automation_id: str,
        broker_id: str,
        account_id: str,
        instrument_id: str,
        **data: object,
    ) -> None:
        safe_data = {key: value for key, value in data.items() if key in _DECISION_FIELDS}
        common: _AuditCommon = {
            "process_id": process_id,
            "parent_process_id": None,
            "automation_id": automation_id,
            "broker_id": broker_id,
            "account_id": account_id,
            "instrument_id": instrument_id,
            "level": BusinessAuditLevel.INFO,
            "occurred_at": self._now(),
            "critical": False,
        }
        events = [
            BusinessAuditEvent(
                event_id=self._id_factory(),
                stage=BusinessAuditStage.MARKET_DATA_RECEIVED,
                message="Fresh market data received",
                data={},
                **common,
            ),
            BusinessAuditEvent(
                event_id=self._id_factory(),
                stage=BusinessAuditStage.TRADING_CYCLE_UPDATED,
                message="Trading cycle updated",
                data={
                    key: safe_data[key]
                    for key in ("cycle_pending_low_before", "cycle_pending_low_after")
                    if key in safe_data
                },
                **common,
            ),
            BusinessAuditEvent(
                event_id=self._id_factory(),
                stage=BusinessAuditStage.POSITION_SNAPSHOT_RECEIVED,
                message="Position snapshot received",
                data={},
                **common,
            ),
            BusinessAuditEvent(
                event_id=self._id_factory(),
                stage=BusinessAuditStage.STRATEGY_CALCULATION_STARTED,
                message="Strategy calculation started",
                data={},
                **common,
            ),
            BusinessAuditEvent(
                event_id=self._id_factory(),
                stage=BusinessAuditStage.STRATEGY_DECISION_MADE,
                message="Strategy decision calculated",
                data=safe_data,
                **common,
            ),
        ]
        if safe_data.get("decision") in {"BUY_MORE", "SELL_PART", "SELL_ALL"}:
            events.append(
                BusinessAuditEvent(
                    event_id=self._id_factory(),
                    stage=BusinessAuditStage.BROKER_INTENT_CREATED,
                    message="Broker intent created",
                    data={"decision": safe_data["decision"]},
                    **common,
                )
            )
        else:
            events.append(
                BusinessAuditEvent(
                    event_id=self._id_factory(),
                    stage=BusinessAuditStage.TRADING_STEP_COMPLETED,
                    message="Trading step completed",
                    data={},
                    **common,
                )
            )
        self._repository.append_audit_events(events)

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
    ) -> None:
        self._repository.append_audit_events(
            [
                BusinessAuditEvent(
                    event_id=self._id_factory(),
                    process_id=process_id,
                    parent_process_id=None,
                    automation_id=automation_id,
                    broker_id=broker_id,
                    account_id=account_id,
                    instrument_id=instrument_id,
                    level=BusinessAuditLevel.ERROR if critical else BusinessAuditLevel.INFO,
                    stage=stage,
                    message=message,
                    data={key: value for key, value in data.items() if key in _ORDER_FIELDS},
                    occurred_at=self._now(),
                    critical=critical,
                )
            ]
        )

    def record_reconciliation(
        self,
        *,
        stage: BusinessAuditStage,
        process_id: str,
        automation_id: str,
        broker_id: str,
        account_id: str,
        instrument_id: str,
        **data: object,
    ) -> None:
        failed = stage is BusinessAuditStage.POSITION_RECONCILIATION_FAILED
        self._repository.append_audit_events(
            [
                BusinessAuditEvent(
                    event_id=self._id_factory(),
                    process_id=process_id,
                    parent_process_id=None,
                    automation_id=automation_id,
                    broker_id=broker_id,
                    account_id=account_id,
                    instrument_id=instrument_id,
                    level=BusinessAuditLevel.ERROR if failed else BusinessAuditLevel.INFO,
                    stage=stage,
                    message=stage.value.replace("_", " ").title(),
                    data={key: value for key, value in data.items() if key in _RECONCILIATION_FIELDS},
                    occurred_at=self._now(),
                    critical=failed,
                )
            ]
        )
