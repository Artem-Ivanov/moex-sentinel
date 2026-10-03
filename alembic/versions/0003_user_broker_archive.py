"""Archive broker settings without deleting their immutable scopes or history.

Revision ID: 0003_user_broker_archive
Revises: 0002_portfolio_snapshot_runs
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from moex_sentinel.storage.types import UTCDateTime

revision: str = "0003_user_broker_archive"
down_revision: str | None = "0002_portfolio_snapshot_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_brokers", sa.Column("archived_at", UTCDateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("LOCK TABLE user_brokers IN ACCESS EXCLUSIVE MODE"))
    elif bind.dialect.name == "sqlite":
        # SQLite's legacy SELECT/DDL mode does not begin a physical transaction.
        # Keep the writer lock until Alembic commits the column and revision change.
        bind.exec_driver_sql("BEGIN IMMEDIATE")
    archived_count = bind.scalar(sa.text("SELECT count(*) FROM user_brokers WHERE archived_at IS NOT NULL"))
    if archived_count:
        raise RuntimeError(
            f"Cannot remove broker archive metadata: it contains {archived_count} archived row(s). "
            "Migrate those records explicitly before retrying this revision."
        )
    op.drop_column("user_brokers", "archived_at")
