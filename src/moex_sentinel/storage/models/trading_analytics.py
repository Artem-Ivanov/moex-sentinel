"""Fee-profile and analytical snapshot persistence models."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from moex_sentinel.storage.models.base import MONEY_PRECISION, MONEY_SCALE, Base, TimestampMixin, UuidPrimaryKeyMixin
from moex_sentinel.storage.types import UTCDateTime


class BrokerAccountFeeProfileModel(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """Current scoped fee rates for an instrument type and currency."""

    __tablename__ = "broker_account_fee_profiles"
    __table_args__ = (
        UniqueConstraint(
            "user_broker_id",
            "instrument_type",
            "currency",
            name="uq_broker_account_fee_profiles_scope_market",
        ),
        CheckConstraint("buy_rate >= 0", name="ck_broker_account_fee_profiles_non_negative_buy_rate"),
        CheckConstraint("sell_rate >= 0", name="ck_broker_account_fee_profiles_non_negative_sell_rate"),
        CheckConstraint(
            "service_rate >= 0",
            name="ck_broker_account_fee_profiles_non_negative_service_rate",
        ),
        CheckConstraint("deal_rate >= 0", name="ck_broker_account_fee_profiles_non_negative_deal_rate"),
    )

    user_broker_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("user_brokers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    instrument_type: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    buy_rate: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    sell_rate: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    service_rate: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    deal_rate: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class PortfolioSnapshotRunModel(UuidPrimaryKeyMixin, Base):
    """One completed portfolio collection run."""

    __tablename__ = "portfolio_snapshot_runs"
    __table_args__ = (UniqueConstraint("bucket_start", name="uq_portfolio_snapshot_runs_bucket"),)

    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    bucket_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    safe_errors: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class PortfolioSnapshotModel(UuidPrimaryKeyMixin, Base):
    """Immutable account and currency portfolio snapshot."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "user_broker_id",
            "account_id",
            "currency",
            "bucket_start",
            name="uq_portfolio_snapshots_account_currency_bucket",
        ),
        Index(
            "ix_portfolio_snapshots_account_currency_captured",
            "user_broker_id",
            "account_id",
            "currency",
            "captured_at",
        ),
        CheckConstraint("total_value >= 0", name="ck_portfolio_snapshots_non_negative_total_value"),
        CheckConstraint("free_cash >= 0", name="ck_portfolio_snapshots_non_negative_free_cash"),
    )

    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("portfolio_snapshot_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_broker_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("user_brokers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    total_value: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    free_cash: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    cumulative_pnl: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    bucket_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class PositionValuationSnapshotModel(UuidPrimaryKeyMixin, Base):
    """Immutable position valuation that is not an execution fact."""

    __tablename__ = "position_valuation_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ("user_broker_id", "automation_id"),
            ("trading_automations.user_broker_id", "trading_automations.id"),
            ondelete="RESTRICT",
            name="fk_position_valuation_snapshots_scoped_automation",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "position_cycle_id"),
            ("position_cycles.user_broker_id", "position_cycles.id"),
            ondelete="RESTRICT",
            name="fk_position_valuation_snapshots_scoped_cycle",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "instrument_id"),
            ("broker_instruments.user_broker_id", "broker_instruments.id"),
            ondelete="RESTRICT",
            name="fk_position_valuation_snapshots_scoped_instrument",
        ),
        CheckConstraint(
            "quantity_lots >= 0",
            name="ck_position_valuation_snapshots_non_negative_quantity",
        ),
        CheckConstraint(
            "average_price >= 0",
            name="ck_position_valuation_snapshots_non_negative_average_price",
        ),
        CheckConstraint("current_price > 0", name="ck_position_valuation_snapshots_positive_current_price"),
        CheckConstraint(
            "invested_amount >= 0",
            name="ck_position_valuation_snapshots_non_negative_invested_amount",
        ),
        CheckConstraint(
            "market_value >= 0",
            name="ck_position_valuation_snapshots_non_negative_market_value",
        ),
        CheckConstraint(
            "actual_commissions >= 0",
            name="ck_position_valuation_snapshots_non_negative_commissions",
        ),
    )

    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    position_cycle_id: Mapped[str] = mapped_column(String(36), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(36), nullable=False)
    quantity_lots: Mapped[int] = mapped_column(nullable=False)
    average_price: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    invested_amount: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    market_value: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    actual_commissions: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
