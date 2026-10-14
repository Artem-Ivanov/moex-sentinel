"""Index exact run lookups without changing snapshot data or existing indexes.

Downgrade is safe after new snapshots: it removes only this non-unique index.
"""

# ruff: noqa: INP001

from alembic import op

revision = "0007_portfolio_run_index"
down_revision = "0006_strict_state_invariants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_portfolio_snapshots_run_id", "portfolio_snapshots", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_portfolio_snapshots_run_id", table_name="portfolio_snapshots")
