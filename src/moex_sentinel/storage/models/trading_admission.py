"""Durable runtime ownership, account admission, and submit reservations."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from moex_sentinel.storage.models.base import MONEY_PRECISION, MONEY_SCALE, Base, TimestampMixin
from moex_sentinel.storage.types import UTCDateTime


class WorkerRuntimeInstanceModel(Base):
    __tablename__ = "worker_runtime_instances"
    __table_args__ = (
        ForeignKeyConstraint(("user_broker_id",), ("user_brokers.id",), ondelete="RESTRICT"),
        UniqueConstraint("user_broker_id", "runtime_instance_id", name="uq_worker_runtime_scope_instance"),
        CheckConstraint("environment IN ('TEST', 'PROD')", name="ck_worker_runtime_environment"),
        CheckConstraint("access_mode IN ('READ_ONLY', 'TRADE')", name="ck_worker_runtime_access_mode"),
        CheckConstraint(
            "registration_status IN ('OWNER', 'PENDING', 'RETIRED')",
            name="ck_worker_runtime_registration_status",
        ),
        CheckConstraint(
            "(registration_status = 'PENDING' AND generation IS NULL AND lease_expires_at IS NULL) OR "
            "(registration_status IN ('OWNER', 'RETIRED') AND generation IS NOT NULL AND generation > 0 "
            "AND (registration_status = 'RETIRED' OR lease_expires_at IS NOT NULL))",
            name="ck_worker_runtime_generation_status",
        ),
        Index("ix_worker_runtime_scope_status", "user_broker_id", "registration_status"),
    )

    runtime_instance_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    environment: Mapped[str] = mapped_column(String(8), nullable=False)
    access_mode: Mapped[str] = mapped_column(String(9), nullable=False)
    strategy_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    secret_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    registration_status: Mapped[str] = mapped_column(String(8), nullable=False)
    generation: Mapped[int | None] = mapped_column()
    registered_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class TradingAccountAdmissionModel(TimestampMixin, Base):
    __tablename__ = "trading_account_admissions"
    __table_args__ = (
        ForeignKeyConstraint(("user_broker_id",), ("user_brokers.id",), ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ("user_broker_id", "owner_runtime_instance_id"),
            ("worker_runtime_instances.user_broker_id", "worker_runtime_instances.runtime_instance_id"),
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision > 0", name="ck_trading_admission_positive_revision"),
        CheckConstraint("owner_generation IS NULL OR owner_generation > 0", name="ck_trading_admission_owner_gen"),
        CheckConstraint(
            "(owner_runtime_instance_id IS NULL AND owner_generation IS NULL) OR "
            "(owner_runtime_instance_id IS NOT NULL AND owner_generation IS NOT NULL)",
            name="ck_trading_admission_owner_pair",
        ),
        CheckConstraint(
            "admission_status IN ('DISARMED', 'ARMED', 'STOP_REQUIRED')",
            name="ck_trading_admission_status",
        ),
    )

    user_broker_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    owner_runtime_instance_id: Mapped[str | None] = mapped_column(String(36))
    owner_generation: Mapped[int | None] = mapped_column()
    recovery_runtime_instance_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("worker_runtime_instances.runtime_instance_id", ondelete="RESTRICT")
    )
    admission_status: Mapped[str] = mapped_column(String(16), nullable=False, default="DISARMED")
    recovery_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    arm_revision: Mapped[int | None] = mapped_column()
    arm_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    limits: Mapped[dict[str, object] | None] = mapped_column(JSON)
    configuration_fingerprint: Mapped[str | None] = mapped_column(String(64))
    baseline_id: Mapped[str | None] = mapped_column(String(36))
    reconciliation_id: Mapped[str | None] = mapped_column(String(36))


class TradingAdmissionBaselineModel(Base):
    __tablename__ = "trading_admission_baselines"
    __table_args__ = (
        ForeignKeyConstraint(("user_broker_id",), ("user_brokers.id",), ondelete="RESTRICT"),
        UniqueConstraint("user_broker_id", "moscow_day", name="uq_trading_baseline_scope_day"),
        CheckConstraint("currency = 'RUB'", name="ck_trading_baseline_currency"),
        CheckConstraint("equity >= 0", name="ck_trading_baseline_equity"),
        CheckConstraint(
            "capture_started_at <= capture_finished_at AND operations_through <= capture_finished_at",
            name="ck_trading_baseline_capture_window",
        ),
    )

    baseline_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    moscow_day: Mapped[date] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    net_external_cash_flow: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    capture_started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    capture_finished_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    operations_through: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    accepted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)


class TradingReconciliationModel(Base):
    __tablename__ = "trading_admission_reconciliations"
    __table_args__ = (
        ForeignKeyConstraint(("user_broker_id",), ("user_brokers.id",), ondelete="RESTRICT"),
        CheckConstraint("scope IN ('ACCOUNT', 'CANARY_UID')", name="ck_trading_reconcile_scope"),
        CheckConstraint(
            "status IN ('ACCOUNT_ONLY', 'CANARY_READY', 'DATA_UNKNOWN')",
            name="ck_trading_reconcile_status",
        ),
        CheckConstraint(
            "position_anchor_units IS NULL OR position_anchor_units >= 0",
            name="ck_trading_reconcile_anchor",
        ),
        CheckConstraint("valid_until >= captured_at", name="ck_trading_reconcile_window"),
        CheckConstraint("active_ordinary_order_count >= 0", name="ck_trading_reconcile_ordinary_count"),
        CheckConstraint("active_stop_order_count >= 0", name="ck_trading_reconcile_stop_count"),
        CheckConstraint("unresolved_reservation_count >= 0", name="ck_trading_reconcile_reservation_count"),
    )

    reconciliation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_runtime_instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_generation: Mapped[int] = mapped_column(nullable=False)
    configuration_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    canary_uid: Mapped[str | None] = mapped_column(String(128))
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    can_arm: Mapped[bool] = mapped_column(Boolean, nullable=False)
    position_anchor_units: Mapped[Decimal | None] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE))
    active_ordinary_order_count: Mapped[int] = mapped_column(nullable=False)
    active_stop_order_count: Mapped[int] = mapped_column(nullable=False)
    unresolved_reservation_count: Mapped[int] = mapped_column(nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    risk_snapshot_id: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)


class TradingSubmitReservationModel(Base):
    __tablename__ = "trading_submit_reservations"
    __table_args__ = (
        ForeignKeyConstraint(("user_broker_id",), ("user_brokers.id",), ondelete="RESTRICT"),
        UniqueConstraint("user_broker_id", "idempotency_key", name="uq_trading_reservation_scope_key"),
        CheckConstraint(
            "state IN ('IN_FLIGHT', 'BROKER_ACTIVE', 'UNKNOWN', 'SETTLED', 'CONFIRMED_NOT_SENT')",
            name="ck_trading_reservation_state",
        ),
        CheckConstraint("generation > 0", name="ck_trading_reservation_generation"),
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_trading_reservation_side"),
        CheckConstraint("quantity_lots > 0", name="ck_trading_reservation_quantity"),
        CheckConstraint("limit_price > 0 AND requested_amount > 0", name="ck_trading_reservation_amount"),
        CheckConstraint("reserved_cash >= 0 AND reserved_position_units >= 0", name="ck_trading_reservation_reserves"),
        Index("ix_trading_reservation_scope_state", "user_broker_id", "state"),
    )

    reservation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    runtime_instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    generation: Mapped[int] = mapped_column(nullable=False)
    configuration_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    permit_id: Mapped[str | None] = mapped_column(String(36))
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    instrument_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity_lots: Mapped[int] = mapped_column(nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    requested_amount: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    reserved_cash: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    reserved_position_units: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class TradingAdmissionEventModel(Base):
    __tablename__ = "trading_admission_events"
    __table_args__ = (
        ForeignKeyConstraint(("user_broker_id",), ("user_brokers.id",), ondelete="RESTRICT"),
        Index("ix_trading_admission_events_scope_time", "user_broker_id", "occurred_at"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    runtime_instance_id: Mapped[str | None] = mapped_column(String(36))
    generation: Mapped[int | None] = mapped_column()
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
