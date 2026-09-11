"""Position lot and execution allocation persistence models."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from moex_sentinel.storage.models.base import MONEY_PRECISION, MONEY_SCALE, Base, TimestampMixin, UuidPrimaryKeyMixin
from moex_sentinel.storage.types import UTCDateTime


class PositionLotModel(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """Open-inventory attribution created by one BUY execution."""

    __tablename__ = "position_lots"
    __table_args__ = (
        UniqueConstraint("user_broker_id", "id", name="uq_position_lots_scope_id"),
        UniqueConstraint(
            "user_broker_id",
            "buy_execution_id",
            name="uq_position_lots_scope_buy_execution",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "automation_id"),
            ("trading_automations.user_broker_id", "trading_automations.id"),
            ondelete="RESTRICT",
            name="fk_position_lots_scoped_automation",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "position_cycle_id"),
            ("position_cycles.user_broker_id", "position_cycles.id"),
            ondelete="RESTRICT",
            name="fk_position_lots_scoped_cycle",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "buy_execution_id"),
            ("trade_executions.user_broker_id", "trade_executions.id"),
            ondelete="RESTRICT",
            name="fk_position_lots_scoped_buy_execution",
        ),
        CheckConstraint("original_lots > 0", name="ck_position_lots_positive_original_lots"),
        CheckConstraint("remaining_lots >= 0", name="ck_position_lots_non_negative_remaining_lots"),
        CheckConstraint(
            "remaining_lots <= original_lots",
            name="ck_position_lots_remaining_within_original",
        ),
        CheckConstraint("entry_price > 0", name="ck_position_lots_positive_entry_price"),
        CheckConstraint("entry_commission >= 0", name="ck_position_lots_non_negative_commission"),
        CheckConstraint(
            "(source = 'BROKER_EXECUTION' AND buy_execution_id IS NOT NULL) OR "
            "(source = 'BROKER_POSITION_BOOTSTRAP' AND buy_execution_id IS NULL)",
            name="ck_position_lots_source_execution",
        ),
    )

    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    position_cycle_id: Mapped[str] = mapped_column(String(36), nullable=False)
    buy_execution_id: Mapped[str | None] = mapped_column(String(36))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    original_lots: Mapped[int] = mapped_column(nullable=False)
    remaining_lots: Mapped[int] = mapped_column(nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    entry_commission: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class ExecutionLotAllocationModel(UuidPrimaryKeyMixin, Base):
    """Immutable LIFO allocation of a SELL execution to a source lot."""

    __tablename__ = "execution_lot_allocations"
    __table_args__ = (
        UniqueConstraint("user_broker_id", "id", name="uq_execution_lot_allocations_scope_id"),
        UniqueConstraint(
            "sell_execution_id",
            "position_lot_id",
            name="uq_execution_lot_allocations_execution_lot",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "automation_id"),
            ("trading_automations.user_broker_id", "trading_automations.id"),
            ondelete="RESTRICT",
            name="fk_execution_lot_allocations_scoped_automation",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "position_cycle_id"),
            ("position_cycles.user_broker_id", "position_cycles.id"),
            ondelete="RESTRICT",
            name="fk_execution_lot_allocations_scoped_cycle",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "sell_execution_id"),
            ("trade_executions.user_broker_id", "trade_executions.id"),
            ondelete="RESTRICT",
            name="fk_execution_lot_allocations_scoped_sell_execution",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "position_lot_id"),
            ("position_lots.user_broker_id", "position_lots.id"),
            ondelete="RESTRICT",
            name="fk_execution_lot_allocations_scoped_position_lot",
        ),
        CheckConstraint("allocated_lots > 0", name="ck_execution_lot_allocations_positive_lots"),
        CheckConstraint("entry_value > 0", name="ck_execution_lot_allocations_positive_entry_value"),
        CheckConstraint("exit_value > 0", name="ck_execution_lot_allocations_positive_exit_value"),
        CheckConstraint(
            "entry_commission >= 0",
            name="ck_execution_lot_allocations_non_negative_entry_commission",
        ),
        CheckConstraint(
            "exit_commission >= 0",
            name="ck_execution_lot_allocations_non_negative_exit_commission",
        ),
    )

    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    position_cycle_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sell_execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    position_lot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    allocated_lots: Mapped[int] = mapped_column(nullable=False)
    entry_value: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    exit_value: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    entry_commission: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    exit_commission: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    allocated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
