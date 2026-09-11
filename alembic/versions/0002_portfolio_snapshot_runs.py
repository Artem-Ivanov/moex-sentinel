"""Replace account summaries with run-grouped portfolio snapshots.

Revision ID: 0002_portfolio_snapshot_runs
Revises: 0001_baseline
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

import moex_sentinel.storage.types
from alembic import op

revision: str = "0002_portfolio_snapshot_runs"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROW_COUNT_QUERIES = {
    "portfolio_snapshots": sa.text("SELECT count(*) FROM portfolio_snapshots"),
    "portfolio_snapshot_runs": sa.text("SELECT count(*) FROM portfolio_snapshot_runs"),
}


def _assert_empty(table_name: str) -> None:
    row_count = op.get_bind().scalar(_ROW_COUNT_QUERIES[table_name])
    if row_count:
        raise RuntimeError(
            f"Cannot replace {table_name}: it contains {row_count} row(s). "
            "Migrate those records explicitly before retrying this revision."
        )


def _lock_tables(*table_names: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    names = ", ".join(table_names)
    bind.execute(sa.text(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE"))


def _create_previous_portfolio_snapshots() -> None:
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


def _create_portfolio_snapshot_runs() -> None:
    op.create_table(
        "portfolio_snapshot_runs",
        sa.Column("captured_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("bucket_start", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("safe_errors", sa.JSON(), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bucket_start", name="uq_portfolio_snapshot_runs_bucket"),
    )
    with op.batch_alter_table("portfolio_snapshot_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_portfolio_snapshot_runs_captured_at"), ["captured_at"], unique=False)


def _create_portfolio_snapshots() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("user_broker_id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=128), nullable=False),
        sa.Column("total_value", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("free_cash", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("cumulative_pnl", sa.Numeric(precision=28, scale=9), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("captured_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("bucket_start", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("created_at", moex_sentinel.storage.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("free_cash >= 0", name="ck_portfolio_snapshots_non_negative_free_cash"),
        sa.CheckConstraint("total_value >= 0", name="ck_portfolio_snapshots_non_negative_total_value"),
        sa.ForeignKeyConstraint(["run_id"], ["portfolio_snapshot_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_broker_id"], ["user_brokers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_broker_id",
            "account_id",
            "currency",
            "bucket_start",
            name="uq_portfolio_snapshots_account_currency_bucket",
        ),
    )
    with op.batch_alter_table("portfolio_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            "ix_portfolio_snapshots_account_currency_captured",
            ["user_broker_id", "account_id", "currency", "captured_at"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_portfolio_snapshots_captured_at"), ["captured_at"], unique=False)


def upgrade() -> None:
    _lock_tables("portfolio_snapshots")
    _assert_empty("portfolio_snapshots")
    with op.batch_alter_table("portfolio_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_portfolio_snapshots_captured_at"))
    op.drop_table("portfolio_snapshots")
    _create_portfolio_snapshot_runs()
    _create_portfolio_snapshots()


def downgrade() -> None:
    _lock_tables("portfolio_snapshots", "portfolio_snapshot_runs")
    _assert_empty("portfolio_snapshots")
    _assert_empty("portfolio_snapshot_runs")
    with op.batch_alter_table("portfolio_snapshots", schema=None) as batch_op:
        batch_op.drop_index("ix_portfolio_snapshots_account_currency_captured")
        batch_op.drop_index(batch_op.f("ix_portfolio_snapshots_captured_at"))
    op.drop_table("portfolio_snapshots")
    with op.batch_alter_table("portfolio_snapshot_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_portfolio_snapshot_runs_captured_at"))
    op.drop_table("portfolio_snapshot_runs")
    _create_previous_portfolio_snapshots()
