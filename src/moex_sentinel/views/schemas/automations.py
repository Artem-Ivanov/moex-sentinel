"""HTTP schemas for baseline trading automation lifecycle."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, Field

from moex_sentinel.domain.repository_records import AutomationRecord
from moex_sentinel.views.schemas.market_data import HistoricCandleSchema
from moex_sentinel.views.schemas.portfolio import MoneySchema, OperationSchema
from sentinel_contracts.base import PositionalModel
from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.business_audit import (
    BusinessAuditEvent,
    BusinessAuditLevel,
    BusinessAuditStage,
    PublishAuditResult,
)
from sentinel_contracts.trading import AutomationState

if TYPE_CHECKING:
    from moex_sentinel.domain.automations import TradingAutomationDetails


class StrictSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")


class CreateAutomationSchema(StrictSchema):
    account_id: str


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
    broker_name: str
    ticker: str
    instrument_name: str

    @classmethod
    def from_domain(cls, value: AutomationRecord) -> AutomationSchema:
        return cls.model_validate(value, from_attributes=True)


class AutomationListSchema(StrictSchema):
    items: list[AutomationSchema]


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
    automation: AutomationSchema
    operations: list[OperationSchema]
    candles: list[HistoricCandleSchema]
    errors: list[PositionDetailsErrorSchema]

    @classmethod
    def from_domain(cls, value: TradingAutomationDetails) -> TradingAutomationDetailsSchema:
        return cls(
            automation=AutomationSchema.from_domain(value.automation),
            operations=[
                OperationSchema(
                    broker_id=item.broker_id,
                    broker_name=item.broker_name,
                    operation_id=item.operation.operation_id,
                    account_id=item.operation.account_id,
                    operation_type=item.operation.operation_type,
                    state=item.operation.state,
                    occurred_at=item.operation.occurred_at,
                    payment=MoneySchema.from_domain(item.operation.payment),
                    price=MoneySchema.from_domain(item.operation.price),
                    quantity=item.operation.quantity,
                    commission=MoneySchema.from_domain(item.operation.commission),
                    instrument_id=item.operation.instrument_id,
                    ticker=item.operation.ticker,
                )
                for item in value.operations
            ],
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


class HeartbeatResponseSchema(StrictSchema):
    worker_id: str
    occurred_at: datetime


class BrokerConnectionSchema(StrictSchema):
    broker_id: str
    adapter_code: str
    target: str
    token: str
    is_test: bool

    @classmethod
    def from_domain(cls, value: BrokerConnection) -> BrokerConnectionSchema:
        return cls.model_validate(value, from_attributes=True)
