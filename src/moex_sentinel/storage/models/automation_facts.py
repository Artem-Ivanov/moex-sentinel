"""Automation and position-cycle persistence models."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from moex_sentinel.storage.models.base import MONEY_PRECISION, MONEY_SCALE, Base, TimestampMixin, UuidPrimaryKeyMixin
from moex_sentinel.storage.types import UTCDateTime


class TradingAutomationModel(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """User intent and lifecycle for one scoped instrument automation."""

    __tablename__ = "trading_automations"
    __table_args__ = (
        UniqueConstraint("user_broker_id", "id", name="uq_trading_automations_scope_id"),
        ForeignKeyConstraint(
            ("user_broker_id", "instrument_id"),
            ("broker_instruments.user_broker_id", "broker_instruments.id"),
            ondelete="RESTRICT",
            name="fk_trading_automations_scoped_instrument",
        ),
        CheckConstraint(
            "state IN ('IN_QUEUE', 'OPENING', 'IN_WORK', 'HOLD', 'CLOSED')",
            name="ck_trading_automations_state",
        ),
        CheckConstraint(
            "(bootstrap_position_cycle_id IS NULL AND bootstrap_position_lot_id IS NULL "
            "AND bootstrap_quantity_lots IS NULL AND bootstrap_average_price IS NULL "
            "AND bootstrap_invested_amount IS NULL AND bootstrap_currency IS NULL "
            "AND bootstrap_observed_at IS NULL) OR "
            "(bootstrap_position_cycle_id IS NOT NULL AND bootstrap_position_lot_id IS NOT NULL "
            "AND bootstrap_quantity_lots > 0 AND bootstrap_average_price > 0 "
            "AND bootstrap_invested_amount > 0 AND bootstrap_currency IS NOT NULL "
            "AND bootstrap_observed_at IS NOT NULL)",
            name="ck_trading_automations_complete_bootstrap_snapshot",
        ),
        CheckConstraint("revision > 0", name="ck_trading_automations_positive_revision"),
        CheckConstraint("last_sequence_number >= 0", name="ck_trading_automations_non_negative_sequence"),
        CheckConstraint(
            "(closed_at IS NULL AND state != 'CLOSED') OR (closed_at IS NOT NULL AND state = 'CLOSED')",
            name="ck_trading_automations_terminal_timestamp",
        ),
        Index(
            "uq_trading_automations_active_scope_instrument",
            "user_broker_id",
            "instrument_id",
            unique=True,
            sqlite_where=text("closed_at IS NULL"),
            postgresql_where=text("closed_at IS NULL"),
        ),
    )

    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    suspended_from_state: Mapped[str | None] = mapped_column(String(16))
    hold_reason: Mapped[str | None] = mapped_column(String(1000))
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    last_sequence_number: Mapped[int] = mapped_column(default=0, nullable=False)
    resume_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    bootstrap_position_cycle_id: Mapped[str | None] = mapped_column(String(36))
    bootstrap_position_lot_id: Mapped[str | None] = mapped_column(String(36))
    bootstrap_quantity_lots: Mapped[int | None] = mapped_column()
    bootstrap_average_price: Mapped[Decimal | None] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE))
    bootstrap_invested_amount: Mapped[Decimal | None] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE))
    bootstrap_currency: Mapped[str | None] = mapped_column(String(8))
    bootstrap_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class PositionCycleModel(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """Rebuildable aggregate of executions for one position lifecycle."""

    __tablename__ = "position_cycles"
    __table_args__ = (
        UniqueConstraint("user_broker_id", "id", name="uq_position_cycles_scope_id"),
        ForeignKeyConstraint(
            ("user_broker_id", "automation_id"),
            ("trading_automations.user_broker_id", "trading_automations.id"),
            ondelete="RESTRICT",
            name="fk_position_cycles_scoped_automation",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "instrument_id"),
            ("broker_instruments.user_broker_id", "broker_instruments.id"),
            ondelete="RESTRICT",
            name="fk_position_cycles_scoped_instrument",
        ),
        CheckConstraint("state IN ('OPEN', 'CLOSED')", name="ck_position_cycles_state"),
        CheckConstraint("quantity_lots >= 0", name="ck_position_cycles_non_negative_quantity"),
        CheckConstraint("average_entry_price >= 0", name="ck_position_cycles_non_negative_average_price"),
        CheckConstraint("invested_amount >= 0", name="ck_position_cycles_non_negative_invested_amount"),
        CheckConstraint(
            "accumulated_commissions >= 0",
            name="ck_position_cycles_non_negative_commissions",
        ),
        CheckConstraint(
            "(state = 'OPEN' AND closed_at IS NULL) OR (state = 'CLOSED' AND closed_at IS NOT NULL)",
            name="ck_position_cycles_state_timestamp",
        ),
        Index(
            "uq_position_cycles_open_scope_automation",
            "user_broker_id",
            "automation_id",
            unique=True,
            sqlite_where=text("closed_at IS NULL"),
            postgresql_where=text("closed_at IS NULL"),
        ),
    )

    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity_lots: Mapped[int] = mapped_column(nullable=False)
    average_entry_price: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    invested_amount: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    accumulated_commissions: Mapped[Decimal] = mapped_column(
        Numeric(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
    )
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
