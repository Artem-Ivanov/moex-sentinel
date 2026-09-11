"""Worker-private SQLAlchemy models; no Core ORM dependency is allowed here."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime, TypeDecorator

from sentinel_contracts.time import floor_utc_millisecond, utc_now_ms


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timezone-aware datetime required.")
        return floor_utc_millisecond(value).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """Shared creation/update timestamps for worker-local rows."""

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now_ms, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now_ms,
        onupdate=utc_now_ms,
        nullable=False,
    )


class CachedAutomationModel(Base, TimestampMixin):
    __tablename__ = "cached_automations"
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now_ms, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now_ms,
        onupdate=utc_now_ms,
        nullable=False,
    )

    automation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_broker_id: Mapped[str | None] = mapped_column(String(36))
    broker_id: Mapped[str | None] = mapped_column(String(36))
    account_id: Mapped[str | None] = mapped_column(String(128))
    instrument_id: Mapped[str | None] = mapped_column(String(128))
    fact_instrument_id: Mapped[str | None] = mapped_column(String(36))
    currency: Mapped[str | None] = mapped_column(String(8))
    position_cycle_id: Mapped[str | None] = mapped_column(String(36))
    lot_size: Mapped[int | None] = mapped_column(Integer)
    min_price_increment: Mapped[Decimal | None] = mapped_column(Numeric(28, 9))
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    last_sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    resume_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bootstrap_position_cycle_id: Mapped[str | None] = mapped_column(String(36))
    bootstrap_position_lot_id: Mapped[str | None] = mapped_column(String(36))
    bootstrap_quantity_lots: Mapped[int | None] = mapped_column(Integer)
    bootstrap_average_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 9))
    bootstrap_invested_amount: Mapped[Decimal | None] = mapped_column(Numeric(28, 9))
    bootstrap_currency: Mapped[str | None] = mapped_column(String(8))
    bootstrap_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class FactOutboxModel(Base, TimestampMixin):
    __tablename__ = "fact_outbox"
    __table_args__ = (
        UniqueConstraint("automation_id", "sequence_number", name="uq_fact_outbox_sequence"),
        Index("ix_fact_outbox_retry", "delivery_state", "next_retry_at"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    fact_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    safe_message: Mapped[str] = mapped_column(String(1000), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class WorkerRunModel(Base, TimestampMixin):
    __tablename__ = "worker_runs"
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now_ms, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now_ms,
        onupdate=utc_now_ms,
        nullable=False,
    )

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    clean_shutdown: Mapped[bool] = mapped_column(Boolean, nullable=False)


class AccountCommissionProfileModel(Base):
    __tablename__ = "account_commission_profiles"

    broker_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    instrument_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    currency: Mapped[str] = mapped_column(String(8), primary_key=True)
    buy_rate: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    sell_rate: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    service_rate: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    deal_rate: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class BusinessAuditEventModel(Base):
    __tablename__ = "business_audit_events"
    __table_args__ = (Index("ix_business_audit_automation_occurred", "automation_id", "occurred_at"),)

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    process_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parent_process_id: Mapped[str | None] = mapped_column(String(36))
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(128), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    event_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class LocalIntentModel(Base):
    __tablename__ = "broker_intents"

    idempotency_key: Mapped[str] = mapped_column(String(36), primary_key=True)
    fact_execution_id: Mapped[str | None] = mapped_column(String(36))
    position_cycle_id: Mapped[str | None] = mapped_column(String(36))
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    quantity_lots: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(28, 9), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(128))
    requested_amount: Mapped[Decimal] = mapped_column(Numeric(28, 9), default=0, nullable=False)
    executed_amount: Mapped[Decimal] = mapped_column(Numeric(28, 9), default=0, nullable=False)
    estimated_commission: Mapped[Decimal] = mapped_column(Numeric(28, 9), default=0, nullable=False)
    executed_commission: Mapped[Decimal] = mapped_column(Numeric(28, 9), default=0, nullable=False)
    executed_lots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    executed_price: Mapped[Decimal] = mapped_column(Numeric(28, 9), default=0, nullable=False)
    execution_currency: Mapped[str | None] = mapped_column(String(8))
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    dispatch_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    broker_responded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    terminal_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    execution_facts_emitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    strategy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class TradeDecisionModel(Base):
    __tablename__ = "trade_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    process_id: Mapped[str | None] = mapped_column(String(36), index=True)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    quantity_lots: Mapped[int] = mapped_column(Integer, nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    average_price: Mapped[Decimal] = mapped_column(Numeric(28, 9), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(28, 9), nullable=False)
    best_bid: Mapped[Decimal] = mapped_column(Numeric(28, 9), nullable=False)
    best_ask: Mapped[Decimal] = mapped_column(Numeric(28, 9), nullable=False)
    invested_amount: Mapped[Decimal] = mapped_column(Numeric(28, 9), nullable=False)
    estimated_commission: Mapped[Decimal] = mapped_column(Numeric(28, 9), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_quantity_lots: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 9))
    strategy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    intent_id: Mapped[str | None] = mapped_column(String(36))


class TradeLotModel(Base):
    __tablename__ = "trade_lots"
    __table_args__ = (UniqueConstraint("source_intent_id", name="uq_trade_lot_source_intent"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_intent_id: Mapped[str | None] = mapped_column(String(36))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    original_lots: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_lots: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(28, 9), nullable=False)
    entry_commission: Mapped[Decimal] = mapped_column(Numeric(28, 9), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class LotAllocationModel(Base):
    __tablename__ = "lot_allocations"
    __table_args__ = (UniqueConstraint("sell_intent_id", "lot_id", name="uq_sell_lot_allocation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    sell_intent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    lot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    quantity_lots: Mapped[int] = mapped_column(Integer, nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(28, 9), nullable=False)
    exit_commission: Mapped[Decimal] = mapped_column(Numeric(28, 9), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(28, 9), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class TradingCycleStateModel(Base):
    __tablename__ = "trading_cycle_states"

    automation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pending_low: Mapped[Decimal | None] = mapped_column(Numeric(28, 9))
    last_buy_candle_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sell_armed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_sell_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 9))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
