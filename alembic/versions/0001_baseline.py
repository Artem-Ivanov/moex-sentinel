"""Create the complete clean Core schema.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-14 11:11:20.759512
"""

from collections.abc import Sequence

import moex_sentinel.storage.types
import sqlalchemy as sa

from alembic import op


revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_brokers",
        sa.Column("api_slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("environment", sa.String(length=8), nullable=False),
        sa.Column("fqdn", sa.String(length=255), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("external_account_id", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint("environment IN ('TEST', 'PROD')", name="ck_user_brokers_environment"),
        sa.CheckConstraint(
            "state != 'ACTIVE' OR (external_account_id IS NOT NULL AND length(trim(external_account_id)) > 0)",
            name="ck_user_brokers_active_account",
        ),
        sa.CheckConstraint("state IN ('DRAFT', 'ACTIVE', 'DISABLED', 'ERROR')", name="ck_user_brokers_state"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("user_brokers", schema=None) as batch_op:
        batch_op.create_index(
            "uq_user_brokers_api_environment_account",
            ["api_slug", "environment", "external_account_id"],
            unique=True,
            sqlite_where=sa.text("external_account_id IS NOT NULL"),
        )

    op.create_table(
        "broker_account_fee_profiles",
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("instrument_type", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("buy_rate", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("sell_rate", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("service_rate", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("deal_rate", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("calculated_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("valid_until", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint("buy_rate >= 0", name="ck_broker_account_fee_profiles_non_negative_buy_rate"),
        sa.CheckConstraint("deal_rate >= 0", name="ck_broker_account_fee_profiles_non_negative_deal_rate"),
        sa.CheckConstraint("sell_rate >= 0", name="ck_broker_account_fee_profiles_non_negative_sell_rate"),
        sa.CheckConstraint("service_rate >= 0", name="ck_broker_account_fee_profiles_non_negative_service_rate"),
        sa.ForeignKeyConstraint(["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_broker_id", "instrument_type", "currency", name="uq_broker_account_fee_profiles_scope_market"
        ),
    )
    op.create_table(
        "broker_instruments",
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("external_instrument_id", sa.String(length=128), nullable=False),
        sa.Column("external_identifiers", sa.JSON(), nullable=False),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("instrument_type", sa.String(length=64), nullable=False),
        sa.Column("class_code", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("min_price_increment", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("api_trade_available", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_selected", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint("lot_size > 0", name="ck_broker_instruments_positive_lot_size"),
        sa.CheckConstraint("min_price_increment > 0", name="ck_broker_instruments_positive_price_increment"),
        sa.ForeignKeyConstraint(["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_broker_id", "external_instrument_id", name="uq_broker_instruments_scope_external_instrument"
        ),
        sa.UniqueConstraint("user_broker_id", "id", name="uq_broker_instruments_scope_id"),
    )
    with op.batch_alter_table("broker_instruments", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_broker_instruments_ticker"), ["ticker"], unique=False)

    op.create_table(
        "instrument_sync_state",
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("last_attempt_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("safe_error", sa.String(length=1000), nullable=True),
        sa.Column("reconciliation_required", sa.Boolean(), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("user_broker_id"),
    )
    op.create_table(
        "portfolio_snapshots",
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("total_value", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("free_cash", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("net_pnl", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("captured_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("free_cash >= 0", name="ck_portfolio_snapshots_non_negative_free_cash"),
        sa.CheckConstraint("total_value >= 0", name="ck_portfolio_snapshots_non_negative_total_value"),
        sa.ForeignKeyConstraint(["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("portfolio_snapshots", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_portfolio_snapshots_captured_at"), ["captured_at"], unique=False)

    op.create_table(
        "trading_automations",
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("instrument_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("suspended_from_state", sa.String(length=16), nullable=True),
        sa.Column("hold_reason", sa.String(length=1000), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("last_sequence_number", sa.Integer(), nullable=False),
        sa.Column("resume_requested", sa.Boolean(), nullable=False),
        sa.Column("closed_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("bootstrap_position_cycle_id", sa.String(length=36), nullable=True),
        sa.Column("bootstrap_position_lot_id", sa.String(length=36), nullable=True),
        sa.Column("bootstrap_quantity_lots", sa.Integer(), nullable=True),
        sa.Column("bootstrap_average_price", sa.Numeric(precision=28, scale=9), nullable=True),
        sa.Column("bootstrap_invested_amount", sa.Numeric(precision=28, scale=9), nullable=True),
        sa.Column("bootstrap_currency", sa.String(length=8), nullable=True),
        sa.Column("bootstrap_observed_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(closed_at IS NULL AND state != 'CLOSED') OR (closed_at IS NOT NULL AND state = 'CLOSED')",
            name="ck_trading_automations_terminal_timestamp",
        ),
        sa.CheckConstraint(
            "state IN ('IN_QUEUE', 'OPENING', 'IN_WORK', 'HOLD', 'CLOSED')", name="ck_trading_automations_state"
        ),
        sa.CheckConstraint(
            "(bootstrap_position_cycle_id IS NULL AND bootstrap_position_lot_id IS NULL AND bootstrap_quantity_lots IS NULL AND bootstrap_average_price IS NULL AND bootstrap_invested_amount IS NULL AND bootstrap_currency IS NULL AND bootstrap_observed_at IS NULL) OR (bootstrap_position_cycle_id IS NOT NULL AND bootstrap_position_lot_id IS NOT NULL AND bootstrap_quantity_lots > 0 AND bootstrap_average_price > 0 AND bootstrap_invested_amount > 0 AND bootstrap_currency IS NOT NULL AND bootstrap_observed_at IS NOT NULL)",
            name="ck_trading_automations_complete_bootstrap_snapshot",
        ),
        sa.CheckConstraint("last_sequence_number >= 0", name="ck_trading_automations_non_negative_sequence"),
        sa.CheckConstraint("revision > 0", name="ck_trading_automations_positive_revision"),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "instrument_id"],
            ["broker_instruments.user_broker_id", "broker_instruments.id"],
            name="fk_trading_automations_scoped_instrument",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_broker_id", "id", name="uq_trading_automations_scope_id"),
    )
    with op.batch_alter_table("trading_automations", schema=None) as batch_op:
        batch_op.create_index(
            "uq_trading_automations_active_scope_instrument",
            ["user_broker_id", "instrument_id"],
            unique=True,
            sqlite_where=sa.text("closed_at IS NULL"),
            postgresql_where=sa.text("closed_at IS NULL"),
        )

    op.create_table(
        "automation_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("automation_id", sa.String(length=36), nullable=False),
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("fact_kind", sa.String(length=64), nullable=False),
        sa.Column("safe_message", sa.String(length=1000), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("received_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint("expected_revision > 0", name="ck_automation_events_positive_revision"),
        sa.CheckConstraint("length(trim(fact_kind)) > 0", name="ck_automation_events_non_blank_fact_kind"),
        sa.CheckConstraint("sequence_number > 0", name="ck_automation_events_positive_sequence"),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "automation_id"],
            ["trading_automations.user_broker_id", "trading_automations.id"],
            name="fk_automation_events_scoped_automation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "user_broker_id", "automation_id", "sequence_number", name="uq_automation_events_scope_sequence"
        ),
    )
    with op.batch_alter_table("automation_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_automation_events_occurred_at"), ["occurred_at"], unique=False)

    op.create_table(
        "position_cycles",
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("automation_id", sa.String(length=36), nullable=False),
        sa.Column("instrument_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("quantity_lots", sa.Integer(), nullable=False),
        sa.Column("average_entry_price", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("invested_amount", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("net_pnl", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("accumulated_commissions", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("opened_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("closed_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(state = 'OPEN' AND closed_at IS NULL) OR (state = 'CLOSED' AND closed_at IS NOT NULL)",
            name="ck_position_cycles_state_timestamp",
        ),
        sa.CheckConstraint("state IN ('OPEN', 'CLOSED')", name="ck_position_cycles_state"),
        sa.CheckConstraint("accumulated_commissions >= 0", name="ck_position_cycles_non_negative_commissions"),
        sa.CheckConstraint("average_entry_price >= 0", name="ck_position_cycles_non_negative_average_price"),
        sa.CheckConstraint("invested_amount >= 0", name="ck_position_cycles_non_negative_invested_amount"),
        sa.CheckConstraint("quantity_lots >= 0", name="ck_position_cycles_non_negative_quantity"),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "automation_id"],
            ["trading_automations.user_broker_id", "trading_automations.id"],
            name="fk_position_cycles_scoped_automation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "instrument_id"],
            ["broker_instruments.user_broker_id", "broker_instruments.id"],
            name="fk_position_cycles_scoped_instrument",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_broker_id", "id", name="uq_position_cycles_scope_id"),
    )
    with op.batch_alter_table("position_cycles", schema=None) as batch_op:
        batch_op.create_index(
            "uq_position_cycles_open_scope_automation",
            ["user_broker_id", "automation_id"],
            unique=True,
            sqlite_where=sa.text("closed_at IS NULL"),
            postgresql_where=sa.text("closed_at IS NULL"),
        )

    op.create_table(
        "position_valuation_snapshots",
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("automation_id", sa.String(length=36), nullable=False),
        sa.Column("position_cycle_id", sa.String(length=36), nullable=False),
        sa.Column("instrument_id", sa.String(length=36), nullable=False),
        sa.Column("quantity_lots", sa.Integer(), nullable=False),
        sa.Column("average_price", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("current_price", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("invested_amount", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("market_value", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("net_pnl", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("actual_commissions", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("captured_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("actual_commissions >= 0", name="ck_position_valuation_snapshots_non_negative_commissions"),
        sa.CheckConstraint("average_price >= 0", name="ck_position_valuation_snapshots_non_negative_average_price"),
        sa.CheckConstraint("current_price > 0", name="ck_position_valuation_snapshots_positive_current_price"),
        sa.CheckConstraint("invested_amount >= 0", name="ck_position_valuation_snapshots_non_negative_invested_amount"),
        sa.CheckConstraint("market_value >= 0", name="ck_position_valuation_snapshots_non_negative_market_value"),
        sa.CheckConstraint("quantity_lots >= 0", name="ck_position_valuation_snapshots_non_negative_quantity"),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "automation_id"],
            ["trading_automations.user_broker_id", "trading_automations.id"],
            name="fk_position_valuation_snapshots_scoped_automation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "instrument_id"],
            ["broker_instruments.user_broker_id", "broker_instruments.id"],
            name="fk_position_valuation_snapshots_scoped_instrument",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "position_cycle_id"],
            ["position_cycles.user_broker_id", "position_cycles.id"],
            name="fk_position_valuation_snapshots_scoped_cycle",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("position_valuation_snapshots", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_position_valuation_snapshots_captured_at"), ["captured_at"], unique=False)

    op.create_table(
        "trade_decisions",
        sa.Column("fact_id", sa.String(length=128), nullable=False),
        sa.Column("process_id", sa.String(length=36), nullable=True),
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("automation_id", sa.String(length=36), nullable=False),
        sa.Column("position_cycle_id", sa.String(length=36), nullable=True),
        sa.Column("instrument_id", sa.String(length=36), nullable=False),
        sa.Column("quantity_lots", sa.Integer(), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("average_price", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("invested_amount", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("current_price", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("best_bid", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("best_ask", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("indicators", sa.JSON(), nullable=False),
        sa.Column("estimated_commission", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("requested_quantity_lots", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Numeric(precision=28, scale=9), nullable=True),
        sa.Column("strategy_snapshot", sa.JSON(), nullable=False),
        sa.Column("decided_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.CheckConstraint(
            "decision IN ('BUY_MORE', 'WAIT', 'SELL_PART', 'SELL_ALL', 'NO_ACTION')", name="ck_trade_decisions_decision"
        ),
        sa.CheckConstraint("average_price >= 0", name="ck_trade_decisions_non_negative_average_price"),
        sa.CheckConstraint("best_ask > 0", name="ck_trade_decisions_positive_best_ask"),
        sa.CheckConstraint("best_bid > 0", name="ck_trade_decisions_positive_best_bid"),
        sa.CheckConstraint("current_price > 0", name="ck_trade_decisions_positive_current_price"),
        sa.CheckConstraint("estimated_commission >= 0", name="ck_trade_decisions_non_negative_commission"),
        sa.CheckConstraint("invested_amount >= 0", name="ck_trade_decisions_non_negative_invested_amount"),
        sa.CheckConstraint("limit_price IS NULL OR limit_price > 0", name="ck_trade_decisions_positive_limit_price"),
        sa.CheckConstraint("lot_size > 0", name="ck_trade_decisions_positive_lot_size"),
        sa.CheckConstraint("quantity_lots >= 0", name="ck_trade_decisions_non_negative_quantity"),
        sa.CheckConstraint("requested_quantity_lots >= 0", name="ck_trade_decisions_non_negative_requested_quantity"),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "automation_id"],
            ["trading_automations.user_broker_id", "trading_automations.id"],
            name="fk_trade_decisions_scoped_automation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "instrument_id"],
            ["broker_instruments.user_broker_id", "broker_instruments.id"],
            name="fk_trade_decisions_scoped_instrument",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "position_cycle_id"],
            ["position_cycles.user_broker_id", "position_cycles.id"],
            name="fk_trade_decisions_scoped_cycle",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fact_id", name="uq_trade_decisions_fact_id"),
        sa.UniqueConstraint("user_broker_id", "id", name="uq_trade_decisions_scope_id"),
    )
    with op.batch_alter_table("trade_decisions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_trade_decisions_decided_at"), ["decided_at"], unique=False)

    op.create_table(
        "broker_orders",
        sa.Column("fact_id", sa.String(length=128), nullable=False),
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("automation_id", sa.String(length=36), nullable=False),
        sa.Column("decision_id", sa.String(length=36), nullable=False),
        sa.Column("position_cycle_id", sa.String(length=36), nullable=True),
        sa.Column("instrument_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("external_order_id", sa.String(length=128), nullable=True),
        sa.Column("intent_kind", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("order_type", sa.String(length=24), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("quantity_lots", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Numeric(precision=28, scale=9), nullable=True),
        sa.Column("requested_amount", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("executed_amount", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("estimated_commission", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("executed_commission", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("strategy_snapshot", sa.JSON(), nullable=False),
        sa.Column("dispatch_started_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("broker_responded_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("executed_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "intent_kind IN ('OPEN', 'BUY_MORE', 'SELL_PART', 'SELL_ALL')", name="ck_broker_orders_intent_kind"
        ),
        sa.CheckConstraint("order_type IN ('MARKET', 'LIMIT')", name="ck_broker_orders_type"),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_broker_orders_side"),
        sa.CheckConstraint(
            "state IN ('CREATED', 'DISPATCH_PENDING', 'SUBMITTING', 'SUBMITTED', 'ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'UNCERTAIN', 'FAILED')",
            name="ck_broker_orders_state",
        ),
        sa.CheckConstraint("estimated_commission >= 0", name="ck_broker_orders_non_negative_estimated_commission"),
        sa.CheckConstraint("executed_amount >= 0", name="ck_broker_orders_non_negative_executed_amount"),
        sa.CheckConstraint("executed_commission >= 0", name="ck_broker_orders_non_negative_executed_commission"),
        sa.CheckConstraint("limit_price IS NULL OR limit_price > 0", name="ck_broker_orders_positive_limit_price"),
        sa.CheckConstraint("quantity_lots > 0", name="ck_broker_orders_positive_quantity"),
        sa.CheckConstraint("requested_amount >= 0", name="ck_broker_orders_non_negative_requested_amount"),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "automation_id"],
            ["trading_automations.user_broker_id", "trading_automations.id"],
            name="fk_broker_orders_scoped_automation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "decision_id"],
            ["trade_decisions.user_broker_id", "trade_decisions.id"],
            name="fk_broker_orders_scoped_decision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "instrument_id"],
            ["broker_instruments.user_broker_id", "broker_instruments.id"],
            name="fk_broker_orders_scoped_instrument",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "position_cycle_id"],
            ["position_cycles.user_broker_id", "position_cycles.id"],
            name="fk_broker_orders_scoped_cycle",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fact_id", name="uq_broker_orders_fact_id"),
        sa.UniqueConstraint("user_broker_id", "decision_id", name="uq_broker_orders_scope_decision"),
        sa.UniqueConstraint("user_broker_id", "id", name="uq_broker_orders_scope_id"),
        sa.UniqueConstraint("user_broker_id", "idempotency_key", name="uq_broker_orders_scope_idempotency"),
    )
    with op.batch_alter_table("broker_orders", schema=None) as batch_op:
        batch_op.create_index(
            "uq_broker_orders_scope_external_order",
            ["user_broker_id", "external_order_id"],
            unique=True,
            sqlite_where=sa.text("external_order_id IS NOT NULL"),
            postgresql_where=sa.text("external_order_id IS NOT NULL"),
        )

    op.create_table(
        "broker_order_events",
        sa.Column("fact_id", sa.String(length=128), nullable=False),
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("automation_id", sa.String(length=36), nullable=False),
        sa.Column("broker_order_id", sa.String(length=36), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=False),
        sa.Column("safe_reason", sa.String(length=128), nullable=False),
        sa.Column("safe_message", sa.String(length=1000), nullable=False),
        sa.Column("occurred_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.CheckConstraint(
            "from_state IS NULL OR from_state IN ('CREATED', 'DISPATCH_PENDING', 'SUBMITTING', 'SUBMITTED', 'ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'UNCERTAIN', 'FAILED')",
            name="ck_broker_order_events_from_state",
        ),
        sa.CheckConstraint(
            "to_state IN ('CREATED', 'DISPATCH_PENDING', 'SUBMITTING', 'SUBMITTED', 'ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'UNCERTAIN', 'FAILED')",
            name="ck_broker_order_events_to_state",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "automation_id"],
            ["trading_automations.user_broker_id", "trading_automations.id"],
            name="fk_broker_order_events_scoped_automation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "broker_order_id"],
            ["broker_orders.user_broker_id", "broker_orders.id"],
            name="fk_broker_order_events_scoped_order",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fact_id", name="uq_broker_order_events_fact_id"),
    )
    with op.batch_alter_table("broker_order_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_broker_order_events_occurred_at"), ["occurred_at"], unique=False)

    op.create_table(
        "trade_executions",
        sa.Column("fact_id", sa.String(length=128), nullable=False),
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("automation_id", sa.String(length=36), nullable=False),
        sa.Column("broker_order_id", sa.String(length=36), nullable=False),
        sa.Column("position_cycle_id", sa.String(length=36), nullable=False),
        sa.Column("instrument_id", sa.String(length=36), nullable=False),
        sa.Column("external_execution_id", sa.String(length=128), nullable=True),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("executed_lots", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("value", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("broker_commission", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("other_fees", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("executed_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_trade_executions_side"),
        sa.CheckConstraint("source = 'BROKER_FILL'", name="ck_trade_executions_source"),
        sa.CheckConstraint("broker_commission >= 0", name="ck_trade_executions_non_negative_commission"),
        sa.CheckConstraint("executed_lots > 0", name="ck_trade_executions_positive_lots"),
        sa.CheckConstraint("external_execution_id IS NOT NULL", name="ck_trade_executions_external_id"),
        sa.CheckConstraint("other_fees >= 0", name="ck_trade_executions_non_negative_fees"),
        sa.CheckConstraint("price > 0", name="ck_trade_executions_positive_price"),
        sa.CheckConstraint("value > 0", name="ck_trade_executions_positive_value"),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "automation_id"],
            ["trading_automations.user_broker_id", "trading_automations.id"],
            name="fk_trade_executions_scoped_automation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "broker_order_id"],
            ["broker_orders.user_broker_id", "broker_orders.id"],
            name="fk_trade_executions_scoped_order",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "instrument_id"],
            ["broker_instruments.user_broker_id", "broker_instruments.id"],
            name="fk_trade_executions_scoped_instrument",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "position_cycle_id"],
            ["position_cycles.user_broker_id", "position_cycles.id"],
            name="fk_trade_executions_scoped_cycle",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fact_id", name="uq_trade_executions_fact_id"),
        sa.UniqueConstraint("user_broker_id", "id", name="uq_trade_executions_scope_id"),
    )
    with op.batch_alter_table("trade_executions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_trade_executions_executed_at"), ["executed_at"], unique=False)
        batch_op.create_index(
            "uq_trade_executions_scope_external_execution",
            ["user_broker_id", "external_execution_id"],
            unique=True,
            sqlite_where=sa.text("external_execution_id IS NOT NULL"),
            postgresql_where=sa.text("external_execution_id IS NOT NULL"),
        )

    op.create_table(
        "position_lots",
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("automation_id", sa.String(length=36), nullable=False),
        sa.Column("position_cycle_id", sa.String(length=36), nullable=False),
        sa.Column("buy_execution_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("original_lots", sa.Integer(), nullable=False),
        sa.Column("remaining_lots", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("entry_commission", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("opened_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(source = 'BROKER_EXECUTION' AND buy_execution_id IS NOT NULL) OR (source = 'BROKER_POSITION_BOOTSTRAP' AND buy_execution_id IS NULL)",
            name="ck_position_lots_source_execution",
        ),
        sa.CheckConstraint("entry_commission >= 0", name="ck_position_lots_non_negative_commission"),
        sa.CheckConstraint("entry_price > 0", name="ck_position_lots_positive_entry_price"),
        sa.CheckConstraint("original_lots > 0", name="ck_position_lots_positive_original_lots"),
        sa.CheckConstraint("remaining_lots <= original_lots", name="ck_position_lots_remaining_within_original"),
        sa.CheckConstraint("remaining_lots >= 0", name="ck_position_lots_non_negative_remaining_lots"),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "automation_id"],
            ["trading_automations.user_broker_id", "trading_automations.id"],
            name="fk_position_lots_scoped_automation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "buy_execution_id"],
            ["trade_executions.user_broker_id", "trade_executions.id"],
            name="fk_position_lots_scoped_buy_execution",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "position_cycle_id"],
            ["position_cycles.user_broker_id", "position_cycles.id"],
            name="fk_position_lots_scoped_cycle",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_broker_id", "buy_execution_id", name="uq_position_lots_scope_buy_execution"),
        sa.UniqueConstraint("user_broker_id", "id", name="uq_position_lots_scope_id"),
    )
    with op.batch_alter_table("position_lots", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_position_lots_opened_at"), ["opened_at"], unique=False)

    op.create_table(
        "trade_audit_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("process_id", sa.String(length=36), nullable=False),
        sa.Column("parent_process_id", sa.String(length=36), nullable=True),
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("automation_id", sa.String(length=36), nullable=False),
        sa.Column("decision_id", sa.String(length=36), nullable=True),
        sa.Column("broker_order_id", sa.String(length=36), nullable=True),
        sa.Column("execution_id", sa.String(length=36), nullable=True),
        sa.Column("instrument_id", sa.String(length=36), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("safe_message", sa.String(length=1000), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("occurred_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("critical", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')", name="ck_trade_audit_events_level"
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "automation_id"],
            ["trading_automations.user_broker_id", "trading_automations.id"],
            name="fk_trade_audit_events_scoped_automation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "broker_order_id"],
            ["broker_orders.user_broker_id", "broker_orders.id"],
            name="fk_trade_audit_events_scoped_order",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "decision_id"],
            ["trade_decisions.user_broker_id", "trade_decisions.id"],
            name="fk_trade_audit_events_scoped_decision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "execution_id"],
            ["trade_executions.user_broker_id", "trade_executions.id"],
            name="fk_trade_audit_events_scoped_execution",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "instrument_id"],
            ["broker_instruments.user_broker_id", "broker_instruments.id"],
            name="fk_trade_audit_events_scoped_instrument",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    with op.batch_alter_table("trade_audit_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_trade_audit_events_occurred_at"), ["occurred_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_trade_audit_events_process_id"), ["process_id"], unique=False)

    op.create_table(
        "execution_lot_allocations",
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("automation_id", sa.String(length=36), nullable=False),
        sa.Column("position_cycle_id", sa.String(length=36), nullable=False),
        sa.Column("sell_execution_id", sa.String(length=36), nullable=False),
        sa.Column("position_lot_id", sa.String(length=36), nullable=False),
        sa.Column("allocated_lots", sa.Integer(), nullable=False),
        sa.Column("entry_value", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("exit_value", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("entry_commission", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("exit_commission", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("allocated_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("allocated_lots > 0", name="ck_execution_lot_allocations_positive_lots"),
        sa.CheckConstraint("entry_commission >= 0", name="ck_execution_lot_allocations_non_negative_entry_commission"),
        sa.CheckConstraint("entry_value > 0", name="ck_execution_lot_allocations_positive_entry_value"),
        sa.CheckConstraint("exit_commission >= 0", name="ck_execution_lot_allocations_non_negative_exit_commission"),
        sa.CheckConstraint("exit_value > 0", name="ck_execution_lot_allocations_positive_exit_value"),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "automation_id"],
            ["trading_automations.user_broker_id", "trading_automations.id"],
            name="fk_execution_lot_allocations_scoped_automation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "position_cycle_id"],
            ["position_cycles.user_broker_id", "position_cycles.id"],
            name="fk_execution_lot_allocations_scoped_cycle",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "position_lot_id"],
            ["position_lots.user_broker_id", "position_lots.id"],
            name="fk_execution_lot_allocations_scoped_position_lot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "sell_execution_id"],
            ["trade_executions.user_broker_id", "trade_executions.id"],
            name="fk_execution_lot_allocations_scoped_sell_execution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sell_execution_id", "position_lot_id", name="uq_execution_lot_allocations_execution_lot"),
        sa.UniqueConstraint("user_broker_id", "id", name="uq_execution_lot_allocations_scope_id"),
    )
    with op.batch_alter_table("execution_lot_allocations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_execution_lot_allocations_allocated_at"), ["allocated_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("execution_lot_allocations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_execution_lot_allocations_allocated_at"))

    op.drop_table("execution_lot_allocations")
    with op.batch_alter_table("trade_audit_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_trade_audit_events_process_id"))
        batch_op.drop_index(batch_op.f("ix_trade_audit_events_occurred_at"))

    op.drop_table("trade_audit_events")
    with op.batch_alter_table("position_lots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_position_lots_opened_at"))

    op.drop_table("position_lots")
    with op.batch_alter_table("trade_executions", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_trade_executions_scope_external_execution",
            sqlite_where=sa.text("external_execution_id IS NOT NULL"),
            postgresql_where=sa.text("external_execution_id IS NOT NULL"),
        )
        batch_op.drop_index(batch_op.f("ix_trade_executions_executed_at"))

    op.drop_table("trade_executions")
    with op.batch_alter_table("broker_order_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_broker_order_events_occurred_at"))

    op.drop_table("broker_order_events")
    with op.batch_alter_table("broker_orders", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_broker_orders_scope_external_order",
            sqlite_where=sa.text("external_order_id IS NOT NULL"),
            postgresql_where=sa.text("external_order_id IS NOT NULL"),
        )

    op.drop_table("broker_orders")
    with op.batch_alter_table("trade_decisions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_trade_decisions_decided_at"))

    op.drop_table("trade_decisions")
    with op.batch_alter_table("position_valuation_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_position_valuation_snapshots_captured_at"))

    op.drop_table("position_valuation_snapshots")
    with op.batch_alter_table("position_cycles", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_position_cycles_open_scope_automation",
            sqlite_where=sa.text("closed_at IS NULL"),
            postgresql_where=sa.text("closed_at IS NULL"),
        )

    op.drop_table("position_cycles")
    with op.batch_alter_table("automation_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_automation_events_occurred_at"))

    op.drop_table("automation_events")
    with op.batch_alter_table("trading_automations", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_trading_automations_active_scope_instrument",
            sqlite_where=sa.text("closed_at IS NULL"),
            postgresql_where=sa.text("closed_at IS NULL"),
        )

    op.drop_table("trading_automations")
    with op.batch_alter_table("portfolio_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_portfolio_snapshots_captured_at"))

    op.drop_table("portfolio_snapshots")
    op.drop_table("instrument_sync_state")
    with op.batch_alter_table("broker_instruments", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_broker_instruments_ticker"))

    op.drop_table("broker_instruments")
    op.drop_table("broker_account_fee_profiles")
    with op.batch_alter_table("user_brokers", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_user_brokers_api_environment_account", sqlite_where=sa.text("external_account_id IS NOT NULL")
        )

    op.drop_table("user_brokers")
