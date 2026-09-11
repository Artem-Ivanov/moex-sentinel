"""Decision, broker-order and execution persistence models."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
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

ORDER_STATES = (
    "'CREATED', 'DISPATCH_PENDING', 'SUBMITTING', 'SUBMITTED', 'ACCEPTED', "
    "'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'UNCERTAIN', 'FAILED'"
)


class TradeDecisionModel(UuidPrimaryKeyMixin, Base):
    """Immutable result of one strategy evaluation."""

    __tablename__ = "trade_decisions"
    __table_args__ = (
        UniqueConstraint("fact_id", name="uq_trade_decisions_fact_id"),
        UniqueConstraint("user_broker_id", "id", name="uq_trade_decisions_scope_id"),
        ForeignKeyConstraint(
            ("user_broker_id", "automation_id"),
            ("trading_automations.user_broker_id", "trading_automations.id"),
            ondelete="RESTRICT",
            name="fk_trade_decisions_scoped_automation",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "position_cycle_id"),
            ("position_cycles.user_broker_id", "position_cycles.id"),
            ondelete="RESTRICT",
            name="fk_trade_decisions_scoped_cycle",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "instrument_id"),
            ("broker_instruments.user_broker_id", "broker_instruments.id"),
            ondelete="RESTRICT",
            name="fk_trade_decisions_scoped_instrument",
        ),
        CheckConstraint("quantity_lots >= 0", name="ck_trade_decisions_non_negative_quantity"),
        CheckConstraint("lot_size > 0", name="ck_trade_decisions_positive_lot_size"),
        CheckConstraint("average_price >= 0", name="ck_trade_decisions_non_negative_average_price"),
        CheckConstraint("invested_amount >= 0", name="ck_trade_decisions_non_negative_invested_amount"),
        CheckConstraint("current_price > 0", name="ck_trade_decisions_positive_current_price"),
        CheckConstraint("best_bid > 0", name="ck_trade_decisions_positive_best_bid"),
        CheckConstraint("best_ask > 0", name="ck_trade_decisions_positive_best_ask"),
        CheckConstraint("estimated_commission >= 0", name="ck_trade_decisions_non_negative_commission"),
        CheckConstraint(
            "decision IN ('BUY_MORE', 'WAIT', 'SELL_PART', 'SELL_ALL', 'NO_ACTION')",
            name="ck_trade_decisions_decision",
        ),
        CheckConstraint(
            "requested_quantity_lots >= 0",
            name="ck_trade_decisions_non_negative_requested_quantity",
        ),
        CheckConstraint("limit_price IS NULL OR limit_price > 0", name="ck_trade_decisions_positive_limit_price"),
    )

    fact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    process_id: Mapped[str | None] = mapped_column(String(36))
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    position_cycle_id: Mapped[str | None] = mapped_column(String(36))
    instrument_id: Mapped[str] = mapped_column(String(36), nullable=False)
    quantity_lots: Mapped[int] = mapped_column(nullable=False)
    lot_size: Mapped[int] = mapped_column(nullable=False)
    average_price: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    invested_amount: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    best_bid: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    best_ask: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    indicators: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    estimated_commission: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_quantity_lots: Mapped[int] = mapped_column(nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE))
    strategy_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class BrokerOrderModel(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One idempotent order intent sent or intended for the broker."""

    __tablename__ = "broker_orders"
    __table_args__ = (
        UniqueConstraint("fact_id", name="uq_broker_orders_fact_id"),
        UniqueConstraint("user_broker_id", "id", name="uq_broker_orders_scope_id"),
        UniqueConstraint(
            "user_broker_id",
            "decision_id",
            name="uq_broker_orders_scope_decision",
        ),
        UniqueConstraint(
            "user_broker_id",
            "idempotency_key",
            name="uq_broker_orders_scope_idempotency",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "automation_id"),
            ("trading_automations.user_broker_id", "trading_automations.id"),
            ondelete="RESTRICT",
            name="fk_broker_orders_scoped_automation",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "decision_id"),
            ("trade_decisions.user_broker_id", "trade_decisions.id"),
            ondelete="RESTRICT",
            name="fk_broker_orders_scoped_decision",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "position_cycle_id"),
            ("position_cycles.user_broker_id", "position_cycles.id"),
            ondelete="RESTRICT",
            name="fk_broker_orders_scoped_cycle",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "instrument_id"),
            ("broker_instruments.user_broker_id", "broker_instruments.id"),
            ondelete="RESTRICT",
            name="fk_broker_orders_scoped_instrument",
        ),
        CheckConstraint(
            "intent_kind IN ('OPEN', 'BUY_MORE', 'SELL_PART', 'SELL_ALL')",
            name="ck_broker_orders_intent_kind",
        ),
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_broker_orders_side"),
        CheckConstraint("order_type IN ('MARKET', 'LIMIT')", name="ck_broker_orders_type"),
        CheckConstraint(f"state IN ({ORDER_STATES})", name="ck_broker_orders_state"),
        CheckConstraint("quantity_lots > 0", name="ck_broker_orders_positive_quantity"),
        CheckConstraint("limit_price IS NULL OR limit_price > 0", name="ck_broker_orders_positive_limit_price"),
        CheckConstraint("requested_amount >= 0", name="ck_broker_orders_non_negative_requested_amount"),
        CheckConstraint("executed_amount >= 0", name="ck_broker_orders_non_negative_executed_amount"),
        CheckConstraint("estimated_commission >= 0", name="ck_broker_orders_non_negative_estimated_commission"),
        CheckConstraint("executed_commission >= 0", name="ck_broker_orders_non_negative_executed_commission"),
        Index(
            "uq_broker_orders_scope_external_order",
            "user_broker_id",
            "external_order_id",
            unique=True,
            sqlite_where=text("external_order_id IS NOT NULL"),
            postgresql_where=text("external_order_id IS NOT NULL"),
        ),
    )

    fact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    position_cycle_id: Mapped[str | None] = mapped_column(String(36))
    instrument_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    external_order_id: Mapped[str | None] = mapped_column(String(128))
    intent_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(24), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity_lots: Mapped[int] = mapped_column(nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE))
    requested_amount: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    executed_amount: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    estimated_commission: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    executed_commission: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    strategy_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    broker_responded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    terminal_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class BrokerOrderEventModel(UuidPrimaryKeyMixin, Base):
    """Immutable broker-order lifecycle transition."""

    __tablename__ = "broker_order_events"
    __table_args__ = (
        UniqueConstraint("fact_id", name="uq_broker_order_events_fact_id"),
        ForeignKeyConstraint(
            ("user_broker_id", "automation_id"),
            ("trading_automations.user_broker_id", "trading_automations.id"),
            ondelete="RESTRICT",
            name="fk_broker_order_events_scoped_automation",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "broker_order_id"),
            ("broker_orders.user_broker_id", "broker_orders.id"),
            ondelete="RESTRICT",
            name="fk_broker_order_events_scoped_order",
        ),
        CheckConstraint(
            f"from_state IS NULL OR from_state IN ({ORDER_STATES})",
            name="ck_broker_order_events_from_state",
        ),
        CheckConstraint(f"to_state IN ({ORDER_STATES})", name="ck_broker_order_events_to_state"),
    )

    fact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    broker_order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    safe_reason: Mapped[str] = mapped_column(String(128), nullable=False)
    safe_message: Mapped[str] = mapped_column(String(1000), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class TradeExecutionModel(UuidPrimaryKeyMixin, Base):
    """Most detailed immutable operation known to Core."""

    __tablename__ = "trade_executions"
    __table_args__ = (
        UniqueConstraint("fact_id", name="uq_trade_executions_fact_id"),
        UniqueConstraint("user_broker_id", "id", name="uq_trade_executions_scope_id"),
        ForeignKeyConstraint(
            ("user_broker_id", "automation_id"),
            ("trading_automations.user_broker_id", "trading_automations.id"),
            ondelete="RESTRICT",
            name="fk_trade_executions_scoped_automation",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "broker_order_id"),
            ("broker_orders.user_broker_id", "broker_orders.id"),
            ondelete="RESTRICT",
            name="fk_trade_executions_scoped_order",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "position_cycle_id"),
            ("position_cycles.user_broker_id", "position_cycles.id"),
            ondelete="RESTRICT",
            name="fk_trade_executions_scoped_cycle",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "instrument_id"),
            ("broker_instruments.user_broker_id", "broker_instruments.id"),
            ondelete="RESTRICT",
            name="fk_trade_executions_scoped_instrument",
        ),
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_trade_executions_side"),
        CheckConstraint("executed_lots > 0", name="ck_trade_executions_positive_lots"),
        CheckConstraint("price > 0", name="ck_trade_executions_positive_price"),
        CheckConstraint("value > 0", name="ck_trade_executions_positive_value"),
        CheckConstraint("broker_commission >= 0", name="ck_trade_executions_non_negative_commission"),
        CheckConstraint("other_fees >= 0", name="ck_trade_executions_non_negative_fees"),
        CheckConstraint("source = 'BROKER_FILL'", name="ck_trade_executions_source"),
        CheckConstraint("external_execution_id IS NOT NULL", name="ck_trade_executions_external_id"),
        Index(
            "uq_trade_executions_scope_external_execution",
            "user_broker_id",
            "external_execution_id",
            unique=True,
            sqlite_where=text("external_execution_id IS NOT NULL"),
            postgresql_where=text("external_execution_id IS NOT NULL"),
        ),
    )

    fact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    broker_order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    position_cycle_id: Mapped[str] = mapped_column(String(36), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(36), nullable=False)
    external_execution_id: Mapped[str | None] = mapped_column(String(128))
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    executed_lots: Mapped[int] = mapped_column(nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    broker_commission: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    other_fees: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
