# ruff: noqa: INP001
"""Add a nullable per-automation order quota percentage.

Revision ID: 0005_order_quota_percent
Revises: 0004_trading_admission
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_order_quota_percent"
down_revision: str | None = "0004_trading_admission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trading_automations",
        sa.Column(
            "settings_revision",
            sa.Integer(),
            sa.CheckConstraint("settings_revision >= 0", name="ck_trading_automations_non_negative_settings_revision"),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "trading_automations",
        sa.Column(
            "order_quota_percent",
            sa.Numeric(precision=28, scale=9),
            sa.CheckConstraint(
                "order_quota_percent IS NULL OR (order_quota_percent > 0 AND order_quota_percent <= 100)",
                name="ck_trading_automations_order_quota_percent",
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("LOCK TABLE trading_automations IN ACCESS EXCLUSIVE MODE"))
    elif bind.dialect.name == "sqlite":
        # Keep the writer lock until Alembic commits the column and revision change.
        bind.exec_driver_sql("BEGIN IMMEDIATE")
    configured_count = bind.scalar(
        sa.text("SELECT count(*) FROM trading_automations WHERE order_quota_percent IS NOT NULL")
    )
    if configured_count:
        raise RuntimeError(
            f"Cannot remove order quota settings: {configured_count} automation(s) have a configured percentage. "
            "Clear those settings explicitly before retrying this revision."
        )
    op.drop_column("trading_automations", "order_quota_percent")
    op.drop_column("trading_automations", "settings_revision")
