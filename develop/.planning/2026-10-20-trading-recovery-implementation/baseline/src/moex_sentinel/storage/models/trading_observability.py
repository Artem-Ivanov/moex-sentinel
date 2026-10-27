"""Accepted fact-envelope and business-audit persistence models."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from moex_sentinel.storage.models.base import Base
from moex_sentinel.storage.types import UTCDateTime


class TradeAuditEventModel(Base):
    """Immutable process diagnostic linked to authoritative facts."""

    __tablename__ = "trade_audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ("user_broker_id", "automation_id"),
            ("trading_automations.user_broker_id", "trading_automations.id"),
            ondelete="RESTRICT",
            name="fk_trade_audit_events_scoped_automation",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "decision_id"),
            ("trade_decisions.user_broker_id", "trade_decisions.id"),
            ondelete="RESTRICT",
            name="fk_trade_audit_events_scoped_decision",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "broker_order_id"),
            ("broker_orders.user_broker_id", "broker_orders.id"),
            ondelete="RESTRICT",
            name="fk_trade_audit_events_scoped_order",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "execution_id"),
            ("trade_executions.user_broker_id", "trade_executions.id"),
            ondelete="RESTRICT",
            name="fk_trade_audit_events_scoped_execution",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "instrument_id"),
            ("broker_instruments.user_broker_id", "broker_instruments.id"),
            ondelete="RESTRICT",
            name="fk_trade_audit_events_scoped_instrument",
        ),
        CheckConstraint(
            "level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')",
            name="ck_trade_audit_events_level",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    process_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parent_process_id: Mapped[str | None] = mapped_column(String(36))
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    decision_id: Mapped[str | None] = mapped_column(String(36))
    broker_order_id: Mapped[str | None] = mapped_column(String(36))
    execution_id: Mapped[str | None] = mapped_column(String(36))
    instrument_id: Mapped[str] = mapped_column(String(36), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    safe_message: Mapped[str] = mapped_column(String(1000), nullable=False)
    data: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    critical: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AutomationEventModel(Base):
    """Accepted ordered Worker-to-Core fact envelope."""

    __tablename__ = "automation_events"
    __table_args__ = (
        UniqueConstraint(
            "user_broker_id",
            "automation_id",
            "sequence_number",
            name="uq_automation_events_scope_sequence",
        ),
        ForeignKeyConstraint(
            ("user_broker_id", "automation_id"),
            ("trading_automations.user_broker_id", "trading_automations.id"),
            ondelete="RESTRICT",
            name="fk_automation_events_scoped_automation",
        ),
        CheckConstraint("sequence_number > 0", name="ck_automation_events_positive_sequence"),
        CheckConstraint("expected_revision > 0", name="ck_automation_events_positive_revision"),
        CheckConstraint("length(trim(fact_kind)) > 0", name="ck_automation_events_non_blank_fact_kind"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sequence_number: Mapped[int] = mapped_column(nullable=False)
    expected_revision: Mapped[int] = mapped_column(nullable=False)
    fact_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    safe_message: Mapped[str] = mapped_column(String(1000), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
