# ruff: noqa: INP001
"""Reject incomplete bootstrap snapshots and incompatible runtime ownership.

Downgrade only relaxes CHECKs; all rows and financial history are retained.
Legacy incompatible rows must be repaired explicitly before upgrade.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_strict_state_invariants"
down_revision: str | None = "0005_order_quota_percent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_BOOTSTRAP = (
    "(bootstrap_position_cycle_id IS NULL AND bootstrap_position_lot_id IS NULL "
    "AND bootstrap_quantity_lots IS NULL AND bootstrap_average_price IS NULL "
    "AND bootstrap_invested_amount IS NULL AND bootstrap_currency IS NULL "
    "AND bootstrap_observed_at IS NULL) OR "
    "(bootstrap_position_cycle_id IS NOT NULL AND bootstrap_position_lot_id IS NOT NULL "
    "AND bootstrap_quantity_lots > 0 AND bootstrap_average_price > 0 "
    "AND bootstrap_invested_amount > 0 AND bootstrap_currency IS NOT NULL "
    "AND bootstrap_observed_at IS NOT NULL)"
)
STRICT_BOOTSTRAP = OLD_BOOTSTRAP
for numeric_field in ("bootstrap_quantity_lots", "bootstrap_average_price", "bootstrap_invested_amount"):
    STRICT_BOOTSTRAP = STRICT_BOOTSTRAP.replace(
        f"{numeric_field} > 0", f"{numeric_field} IS NOT NULL AND {numeric_field} > 0"
    )

OLD_RUNTIME = (
    "(registration_status = 'PENDING' AND generation IS NULL AND lease_expires_at IS NULL) OR "
    "(registration_status IN ('OWNER', 'RETIRED') AND generation > 0)"
)
STRICT_RUNTIME = (
    "(registration_status = 'PENDING' AND generation IS NULL AND lease_expires_at IS NULL) OR "
    "(registration_status IN ('OWNER', 'RETIRED') AND generation IS NOT NULL AND generation > 0 "
    "AND (registration_status = 'RETIRED' OR lease_expires_at IS NOT NULL))"
)


def _lock_tables() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("LOCK TABLE trading_automations, worker_runtime_instances IN ACCESS EXCLUSIVE MODE"))
    elif bind.dialect.name == "sqlite" and not bind.connection.driver_connection.in_transaction:
        # Alembic's SQLite legacy DDL mode otherwise cannot roll back a rebuild.
        bind.exec_driver_sql("BEGIN IMMEDIATE")


def _replace_checks(bootstrap: str, runtime: str) -> None:
    bind = op.get_bind()
    for table, name, expression in (
        ("trading_automations", "ck_trading_automations_complete_bootstrap_snapshot", bootstrap),
        ("worker_runtime_instances", "ck_worker_runtime_generation_status", runtime),
    ):
        reflected_table = None
        if bind.dialect.name == "sqlite" and table == "trading_automations":
            reflected_table = sa.Table(table, sa.MetaData(), autoload_with=bind)
            # Preserve 0005's inline CHECKs so its native DROP COLUMN downgrade still works.
            for column, check_name in (
                ("settings_revision", "ck_trading_automations_non_negative_settings_revision"),
                ("order_quota_percent", "ck_trading_automations_order_quota_percent"),
            ):
                check = next(item for item in reflected_table.constraints if item.name == check_name)
                reflected_table.constraints.remove(check)
                reflected_table.c[column].constraints.add(sa.CheckConstraint(str(check.sqltext), name=check_name))
        with op.batch_alter_table(table, copy_from=reflected_table) as batch:
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, expression)


def upgrade() -> None:
    _lock_tables()
    bind = op.get_bind()
    for table, expression in (("trading_automations", STRICT_BOOTSTRAP), ("worker_runtime_instances", STRICT_RUNTIME)):
        count = bind.scalar(
            sa.select(sa.func.count()).select_from(sa.table(table)).where(sa.text(f"({expression}) IS NOT TRUE"))
        )
        if count:
            raise RuntimeError(
                f"Cannot enforce strict state invariants: {table} contains {count} incompatible row(s). "
                "Repair these rows explicitly before retrying this revision."
            )
    _replace_checks(STRICT_BOOTSTRAP, STRICT_RUNTIME)


def downgrade() -> None:
    _lock_tables()
    _replace_checks(OLD_BOOTSTRAP, OLD_RUNTIME)
