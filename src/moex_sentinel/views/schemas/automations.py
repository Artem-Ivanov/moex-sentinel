"""HTTP schemas for baseline trading automation lifecycle."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ConfigDict, Field, StrictInt, ValidationError, model_validator

from moex_sentinel.domain.repository_records import AutomationRecord
from moex_sentinel.views.schemas.market_data import HistoricCandleSchema
from moex_sentinel.views.schemas.portfolio import OperationSchema
from sentinel_contracts.base import PositionalModel
from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.business_audit import (
    BusinessAuditEvent,
    BusinessAuditLevel,
    BusinessAuditStage,
    PublishAuditResult,
)
from sentinel_contracts.runtime_versions import RuntimeVersion
from sentinel_contracts.time import utc_now_ms
from sentinel_contracts.tinvest import BrokerAccessMode, BrokerEnvironment
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import OrderQuotaPercent
from sentinel_contracts.worker_diagnostics import WorkerDiagnostics, WorkerExecutionDiagnostics

if TYPE_CHECKING:
    from moex_sentinel.domain.automations import TradingAutomationDetails


class StrictSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")


class CreateAutomationSchema(StrictSchema):
    account_id: str


class UpdateAutomationSettingsSchema(StrictSchema):
    expected_revision: StrictInt = Field(gt=0)
    expected_settings_revision: StrictInt = Field(ge=0)
    order_quota_percent: OrderQuotaPercent | None


class AutomationSchema(StrictSchema):
    id: str
    broker_id: str
    account_id: str
    instrument_id: str
    state: AutomationState
    suspended_from_state: AutomationState | None
    revision: int
    last_sequence_number: int
    resume_requested: bool
    currency: str
    strategy_code: str
    strategy_version: str
    quantity_lots: int
    average_price: Decimal
    invested_amount: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    net_pnl: Decimal
    actual_commissions: Decimal
    financial_status: Literal["COMPLETE", "INCOMPLETE", "UNKNOWN"] = "INCOMPLETE"
    broker_name: str
    ticker: str
    instrument_name: str
    bootstrap_pending: bool = False
    order_quota_percent: Decimal | None
    settings_revision: int = 0
    hold_reason: str | None = None

    @classmethod
    def from_domain(cls, value: AutomationRecord) -> AutomationSchema:
        return cls.model_validate(value, from_attributes=True)


class AutomationListSchema(StrictSchema):
    items: list[AutomationSchema]


class DecisionReasonItemSchema(StrictSchema):
    event_id: str
    occurred_at: datetime
    decision: str | None
    reason_code: str | None
    historical: bool


class DecisionReasonsSchema(StrictSchema):
    captured_at: datetime
    items: list[DecisionReasonItemSchema]


class TradingSessionsStatusSchema(StrictSchema):
    status: str
    total: int
    open: int
    closed: int
    unavailable: int


class PositionDetailsErrorSchema(StrictSchema):
    source: str
    code: str
    message: str


class TradingAutomationDetailsSchema(StrictSchema):
    captured_at: datetime
    automation: AutomationSchema
    operations: list[OperationSchema]
    candles: list[HistoricCandleSchema]
    errors: list[PositionDetailsErrorSchema]

    @classmethod
    def from_domain(cls, value: TradingAutomationDetails) -> TradingAutomationDetailsSchema:
        captured_at = utc_now_ms()
        return cls(
            captured_at=captured_at,
            automation=AutomationSchema.from_domain(value.automation),
            operations=[OperationSchema.from_domain(item, captured_at) for item in value.operations],
            candles=[HistoricCandleSchema.model_validate(item, from_attributes=True) for item in value.candles],
            errors=[PositionDetailsErrorSchema.model_validate(item, from_attributes=True) for item in value.errors],
        )


class BusinessAuditEventSchema(StrictSchema):
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
    occurred_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)
    critical: bool = False

    def to_domain(self) -> BusinessAuditEvent:
        return BusinessAuditEvent(**self.model_dump())

    @classmethod
    def from_domain(cls, value: BusinessAuditEvent) -> BusinessAuditEventSchema:
        return cls.model_validate(value, from_attributes=True)


class PublishAuditRequestSchema(StrictSchema):
    events: list[BusinessAuditEventSchema]


class PublishAuditResponseSchema(StrictSchema):
    accepted_event_ids: tuple[str, ...]

    @classmethod
    def from_domain(cls, value: PublishAuditResult) -> PublishAuditResponseSchema:
        return cls.model_validate(value, from_attributes=True)


class BusinessAuditListSchema(StrictSchema):
    items: list[BusinessAuditEventSchema]


class HeartbeatRequestSchema(StrictSchema):
    worker_id: str
    occurred_at: datetime
    runtime_version: RuntimeVersion | None = None
    worker_diagnostics: WorkerDiagnostics | None = None

    @model_validator(mode="before")
    @classmethod
    def contain_invalid_execution_projection(cls, value):
        if not isinstance(value, dict):
            return value
        diagnostics = value.get("worker_diagnostics")
        if not isinstance(diagnostics, dict) or "execution_diagnostics" not in diagnostics:
            return value
        raw_execution = diagnostics["execution_diagnostics"]
        if raw_execution is None:
            return value
        oversized = False
        if isinstance(raw_execution, dict):
            rows = raw_execution.get("rows")
            if isinstance(rows, (list, tuple)):
                oversized = len(rows) > 100
                if not oversized:
                    oversized = any(
                        isinstance(row, dict)
                        and isinstance(row.get("intents"), (list, tuple))
                        and len(row["intents"]) > 5
                        for row in rows
                    )
        if oversized:
            value = dict(value)
            diagnostics = dict(diagnostics)
            diagnostics["execution_diagnostics"] = {
                "observation": "UNKNOWN",
                "reason": "INVALID_PROJECTION",
            }
            value["worker_diagnostics"] = diagnostics
            return value
        try:
            WorkerExecutionDiagnostics.model_validate(raw_execution)
        except ValidationError:
            value = dict(value)
            diagnostics = dict(diagnostics)
            diagnostics["execution_diagnostics"] = {
                "observation": "UNKNOWN",
                "reason": "INVALID_PROJECTION",
            }
            value["worker_diagnostics"] = diagnostics
        return value

    @model_validator(mode="after")
    def require_diagnostic_identity(self):
        if self.worker_diagnostics is not None and self.runtime_version is None:
            raise ValueError("Worker diagnostics requires runtime identity")
        return self


class HeartbeatResponseSchema(StrictSchema):
    worker_id: str
    occurred_at: datetime


class BrokerConnectionSchema(StrictSchema):
    broker_id: str
    adapter_code: str
    target: str
    token: str
    is_test: bool
    environment: BrokerEnvironment | None = None
    access_mode: BrokerAccessMode = "READ_ONLY"
    account_id: str = ""
    configuration_fingerprint: str | None = None

    @classmethod
    def from_domain(cls, value: BrokerConnection) -> BrokerConnectionSchema:
        return cls.model_validate(value, from_attributes=True)
