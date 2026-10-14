# ruff: noqa: INP001
"""Create durable worker ownership and production trading admission records.

Revision ID: 0004_trading_admission
Revises: 0003_user_broker_archive
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from moex_sentinel.storage.types import UTCDateTime

revision: str = "0004_trading_admission"
down_revision: str | None = "0003_user_broker_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "worker_runtime_instances",
    "trading_account_admissions",
    "trading_admission_baselines",
    "trading_admission_reconciliations",
    "trading_submit_reservations",
    "trading_admission_events",
)
_ROW_COUNT_QUERIES = (
    sa.text("SELECT count(*) FROM worker_runtime_instances"),
    sa.text("SELECT count(*) FROM trading_account_admissions"),
    sa.text("SELECT count(*) FROM trading_admission_baselines"),
    sa.text("SELECT count(*) FROM trading_admission_reconciliations"),
    sa.text("SELECT count(*) FROM trading_submit_reservations"),
    sa.text("SELECT count(*) FROM trading_admission_events"),
)


def upgrade() -> None:
    op.create_table(
        "worker_runtime_instances",
        sa.Column("runtime_instance_id", sa.String(36), primary_key=True),
        sa.Column("user_broker_id", sa.String(36), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("environment", sa.String(8), nullable=False),
        sa.Column("access_mode", sa.String(9), nullable=False),
        sa.Column("strategy_enabled", sa.Boolean(), nullable=False),
        sa.Column("secret_digest", sa.String(64), nullable=False),
        sa.Column("registration_status", sa.String(8), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=True),
        sa.Column("registered_at", UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", UTCDateTime(), nullable=False),
        sa.Column("lease_expires_at", UTCDateTime(), nullable=True),
        sa.Column("retired_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("user_broker_id", "runtime_instance_id", name="uq_worker_runtime_scope_instance"),
        sa.CheckConstraint("environment IN ('TEST', 'PROD')", name="ck_worker_runtime_environment"),
        sa.CheckConstraint("access_mode IN ('READ_ONLY', 'TRADE')", name="ck_worker_runtime_access_mode"),
        sa.CheckConstraint(
            "registration_status IN ('OWNER', 'PENDING', 'RETIRED')",
            name="ck_worker_runtime_registration_status",
        ),
        sa.CheckConstraint(
            "(registration_status = 'PENDING' AND generation IS NULL AND lease_expires_at IS NULL) OR "
            "(registration_status IN ('OWNER', 'RETIRED') AND generation > 0)",
            name="ck_worker_runtime_generation_status",
        ),
    )
    op.create_index(
        "ix_worker_runtime_scope_status", "worker_runtime_instances", ["user_broker_id", "registration_status"]
    )

    op.create_table(
        "trading_account_admissions",
        sa.Column("user_broker_id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("owner_runtime_instance_id", sa.String(36), nullable=True),
        sa.Column("owner_generation", sa.Integer(), nullable=True),
        sa.Column("recovery_runtime_instance_id", sa.String(36), nullable=True),
        sa.Column("admission_status", sa.String(16), nullable=False),
        sa.Column("recovery_only", sa.Boolean(), nullable=False),
        sa.Column("lease_expires_at", UTCDateTime(), nullable=True),
        sa.Column("arm_revision", sa.Integer(), nullable=True),
        sa.Column("arm_expires_at", UTCDateTime(), nullable=True),
        sa.Column("limits", sa.JSON(), nullable=True),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=True),
        sa.Column("baseline_id", sa.String(36), nullable=True),
        sa.Column("reconciliation_id", sa.String(36), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "owner_runtime_instance_id"],
            ["worker_runtime_instances.user_broker_id", "worker_runtime_instances.runtime_instance_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recovery_runtime_instance_id"],
            ["worker_runtime_instances.runtime_instance_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("revision > 0", name="ck_trading_admission_positive_revision"),
        sa.CheckConstraint("owner_generation IS NULL OR owner_generation > 0", name="ck_trading_admission_owner_gen"),
        sa.CheckConstraint(
            "(owner_runtime_instance_id IS NULL AND owner_generation IS NULL) OR "
            "(owner_runtime_instance_id IS NOT NULL AND owner_generation IS NOT NULL)",
            name="ck_trading_admission_owner_pair",
        ),
        sa.CheckConstraint(
            "admission_status IN ('DISARMED', 'ARMED', 'STOP_REQUIRED')",
            name="ck_trading_admission_status",
        ),
    )

    op.create_table(
        "trading_admission_baselines",
        sa.Column("baseline_id", sa.String(36), primary_key=True),
        sa.Column("user_broker_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("moscow_day", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("equity", sa.Numeric(28, 9), nullable=False),
        sa.Column("net_external_cash_flow", sa.Numeric(28, 9), nullable=False),
        sa.Column("capture_started_at", UTCDateTime(), nullable=False),
        sa.Column("capture_finished_at", UTCDateTime(), nullable=False),
        sa.Column("operations_through", UTCDateTime(), nullable=False),
        sa.Column("accepted_by", sa.String(128), nullable=False),
        sa.Column("accepted_at", UTCDateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("user_broker_id", "moscow_day", name="uq_trading_baseline_scope_day"),
        sa.CheckConstraint("currency = 'RUB'", name="ck_trading_baseline_currency"),
        sa.CheckConstraint("equity >= 0", name="ck_trading_baseline_equity"),
        sa.CheckConstraint(
            "capture_started_at <= capture_finished_at AND operations_through <= capture_finished_at",
            name="ck_trading_baseline_capture_window",
        ),
    )

    op.create_table(
        "trading_admission_reconciliations",
        sa.Column("reconciliation_id", sa.String(36), primary_key=True),
        sa.Column("user_broker_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("owner_runtime_instance_id", sa.String(36), nullable=False),
        sa.Column("owner_generation", sa.Integer(), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("canary_uid", sa.String(128), nullable=True),
        sa.Column("captured_at", UTCDateTime(), nullable=False),
        sa.Column("valid_until", UTCDateTime(), nullable=False),
        sa.Column("can_arm", sa.Boolean(), nullable=False),
        sa.Column("position_anchor_units", sa.Numeric(28, 9), nullable=True),
        sa.Column("active_ordinary_order_count", sa.Integer(), nullable=False),
        sa.Column("active_stop_order_count", sa.Integer(), nullable=False),
        sa.Column("unresolved_reservation_count", sa.Integer(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("risk_snapshot_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("scope IN ('ACCOUNT', 'CANARY_UID')", name="ck_trading_reconcile_scope"),
        sa.CheckConstraint(
            "status IN ('ACCOUNT_ONLY', 'CANARY_READY', 'DATA_UNKNOWN')", name="ck_trading_reconcile_status"
        ),
        sa.CheckConstraint(
            "position_anchor_units IS NULL OR position_anchor_units >= 0", name="ck_trading_reconcile_anchor"
        ),
        sa.CheckConstraint("valid_until >= captured_at", name="ck_trading_reconcile_window"),
        sa.CheckConstraint("active_ordinary_order_count >= 0", name="ck_trading_reconcile_ordinary_count"),
        sa.CheckConstraint("active_stop_order_count >= 0", name="ck_trading_reconcile_stop_count"),
        sa.CheckConstraint("unresolved_reservation_count >= 0", name="ck_trading_reconcile_reservation_count"),
    )

    op.create_table(
        "trading_submit_reservations",
        sa.Column("reservation_id", sa.String(36), primary_key=True),
        sa.Column("user_broker_id", sa.String(36), nullable=False),
        sa.Column("automation_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(36), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("runtime_instance_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("permit_id", sa.String(36), nullable=True),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("instrument_uid", sa.String(128), nullable=False),
        sa.Column("quantity_lots", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Numeric(28, 9), nullable=False),
        sa.Column("requested_amount", sa.Numeric(28, 9), nullable=False),
        sa.Column("reserved_cash", sa.Numeric(28, 9), nullable=False),
        sa.Column("reserved_position_units", sa.Numeric(28, 9), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("user_broker_id", "idempotency_key", name="uq_trading_reservation_scope_key"),
        sa.CheckConstraint(
            "state IN ('IN_FLIGHT', 'BROKER_ACTIVE', 'UNKNOWN', 'SETTLED', 'CONFIRMED_NOT_SENT')",
            name="ck_trading_reservation_state",
        ),
        sa.CheckConstraint("generation > 0", name="ck_trading_reservation_generation"),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_trading_reservation_side"),
        sa.CheckConstraint("quantity_lots > 0", name="ck_trading_reservation_quantity"),
        sa.CheckConstraint("limit_price > 0 AND requested_amount > 0", name="ck_trading_reservation_amount"),
        sa.CheckConstraint(
            "reserved_cash >= 0 AND reserved_position_units >= 0", name="ck_trading_reservation_reserves"
        ),
    )
    op.create_index("ix_trading_reservation_scope_state", "trading_submit_reservations", ["user_broker_id", "state"])

    op.create_table(
        "trading_admission_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("user_broker_id", sa.String(36), nullable=False),
        sa.Column("runtime_instance_id", sa.String(36), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(128), nullable=True),
        sa.Column("occurred_at", UTCDateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_trading_admission_events_scope_time", "trading_admission_events", ["user_broker_id", "occurred_at"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("LOCK TABLE " + ", ".join(_TABLES) + " IN ACCESS EXCLUSIVE MODE"))
    elif bind.dialect.name == "sqlite":
        bind.exec_driver_sql("BEGIN IMMEDIATE")
    for table_name, query in zip(_TABLES, _ROW_COUNT_QUERIES, strict=True):
        count = bind.scalar(query)
        if count:
            raise RuntimeError(f"Cannot remove trading admission history: {table_name} contains {count} row(s).")
    op.drop_table("trading_admission_events")
    op.drop_index("ix_trading_reservation_scope_state", table_name="trading_submit_reservations")
    op.drop_table("trading_submit_reservations")
    op.drop_table("trading_admission_reconciliations")
    op.drop_table("trading_admission_baselines")
    op.drop_table("trading_account_admissions")
    op.drop_index("ix_worker_runtime_scope_status", table_name="worker_runtime_instances")
    op.drop_table("worker_runtime_instances")
