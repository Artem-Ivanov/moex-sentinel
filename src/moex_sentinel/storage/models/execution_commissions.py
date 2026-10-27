"""Append-only broker commission evidence and resumable account scans."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from moex_sentinel.storage.models.base import MONEY_PRECISION, MONEY_SCALE, Base, UuidPrimaryKeyMixin
from moex_sentinel.storage.types import UTCDateTime


class BrokerOrderFeeObservationModel(UuidPrimaryKeyMixin, Base):
    """One amount observed for an exact broker parent during one full scan pass."""

    __tablename__ = "broker_order_fee_observations"
    __table_args__ = (
        UniqueConstraint(
            "user_broker_id",
            "parent_operation_id",
            "operation_type",
            "currency",
            "scan_pass_id",
            "amount",
            name="uq_broker_fee_observations_pass_amount",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "broker_order_id"),
            ("broker_orders.user_broker_id", "broker_orders.id"),
            ondelete="RESTRICT",
            name="fk_broker_fee_observations_scoped_order",
        ),
        CheckConstraint("operation_type = 'BROKER_FEE'", name="ck_broker_fee_observations_type"),
        CheckConstraint("amount > 0", name="ck_broker_fee_observations_positive_amount"),
        CheckConstraint(
            "status IN ('CANDIDATE', 'KNOWN', 'UNKNOWN')",
            name="ck_broker_fee_observations_status",
        ),
        Index(
            "ix_broker_fee_observations_effective",
            "user_broker_id",
            "parent_operation_id",
            "operation_type",
            "currency",
            "status",
        ),
    )

    user_broker_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_brokers.id", ondelete="RESTRICT"), nullable=False
    )
    broker_order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    scan_pass_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class BrokerFeeScanStateModel(Base):
    """Fair per-account cursor and claim used to reject stale page responses."""

    __tablename__ = "broker_fee_scan_state"
    __table_args__ = (
        CheckConstraint(
            "status IN ('SCANNING', 'COMPLETE', 'ERROR')",
            name="ck_broker_fee_scan_state_status",
        ),
    )

    user_broker_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_brokers.id", ondelete="RESTRICT"), primary_key=True
    )
    scan_pass_id: Mapped[str | None] = mapped_column(String(36))
    completed_scan_pass_id: Mapped[str | None] = mapped_column(String(36))
    lower_bound: Mapped[datetime | None] = mapped_column(UTCDateTime())
    upper_bound: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cursor: Mapped[str | None] = mapped_column(String(1024))
    seen_cursors: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    claim_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="SCANNING")
    last_attempted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error_code: Mapped[str | None] = mapped_column(String(64))
