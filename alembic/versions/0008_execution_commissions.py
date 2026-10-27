"""Persist exact broker fee evidence and bounded history scan state."""

# ruff: noqa: INP001

import sqlalchemy as sa

from alembic import op
from moex_sentinel.storage.models.base import MONEY_PRECISION, MONEY_SCALE
from moex_sentinel.storage.types import UTCDateTime

revision = "0008_execution_commissions"
down_revision = "0007_portfolio_run_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "broker_order_fee_observations",
        sa.Column("user_broker_id", sa.String(36), nullable=False),
        sa.Column("broker_order_id", sa.String(36), nullable=False),
        sa.Column("parent_operation_id", sa.String(128), nullable=False),
        sa.Column("operation_type", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("scan_pass_id", sa.String(36), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("amount", sa.Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("occurred_at", UTCDateTime(), nullable=False),
        sa.Column("observed_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.CheckConstraint("operation_type = 'BROKER_FEE'", name="ck_broker_fee_observations_type"),
        sa.CheckConstraint("amount > 0", name="ck_broker_fee_observations_positive_amount"),
        sa.CheckConstraint(
            "status IN ('CANDIDATE', 'KNOWN', 'UNKNOWN')",
            name="ck_broker_fee_observations_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_broker_id", "broker_order_id"],
            ["broker_orders.user_broker_id", "broker_orders.id"],
            ondelete="RESTRICT",
            name="fk_broker_fee_observations_scoped_order",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_broker_id",
            "parent_operation_id",
            "operation_type",
            "currency",
            "scan_pass_id",
            "amount",
            name="uq_broker_fee_observations_pass_amount",
        ),
    )
    op.create_index(
        "ix_broker_fee_observations_effective",
        "broker_order_fee_observations",
        ["user_broker_id", "parent_operation_id", "operation_type", "currency", "status"],
    )
    op.create_table(
        "broker_fee_scan_state",
        sa.Column("user_broker_id", sa.String(36), nullable=False),
        sa.Column("scan_pass_id", sa.String(36)),
        sa.Column("completed_scan_pass_id", sa.String(36)),
        sa.Column("lower_bound", UTCDateTime()),
        sa.Column("upper_bound", UTCDateTime()),
        sa.Column("cursor", sa.String(1024)),
        sa.Column("seen_cursors", sa.JSON(), nullable=False),
        sa.Column("claim_id", sa.String(36)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_attempted_at", UTCDateTime()),
        sa.Column("completed_at", UTCDateTime()),
        sa.Column("last_error_code", sa.String(64)),
        sa.CheckConstraint(
            "status IN ('SCANNING', 'COMPLETE', 'ERROR')",
            name="ck_broker_fee_scan_state_status",
        ),
        sa.ForeignKeyConstraint(["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("user_broker_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "LOCK TABLE broker_order_fee_observations, broker_fee_scan_state IN ACCESS EXCLUSIVE MODE"
            )
        )
    elif bind.dialect.name == "sqlite":
        bind.exec_driver_sql("BEGIN IMMEDIATE")
    observations = sa.table("broker_order_fee_observations", sa.column("id", sa.String(36)))
    if bind.scalar(sa.select(sa.func.count()).select_from(observations)):
        raise RuntimeError("Cannot downgrade while broker fee observations preserve financial history.")
    op.drop_table("broker_fee_scan_state")
    op.drop_index("ix_broker_fee_observations_effective", table_name="broker_order_fee_observations")
    op.drop_table("broker_order_fee_observations")
