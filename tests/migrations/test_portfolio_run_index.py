from datetime import timedelta

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from alembic import command
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import PortfolioSnapshotModel, PortfolioSnapshotRunModel
from moex_sentinel.storage.repositories.portfolio_snapshots import PortfolioSnapshotRepository
from moex_sentinel.storage.schema_revision import current_schema_revision
from tests.migrations.test_strict_state_invariants import migration_config
from tests.migrations.user_broker_archive_helpers import archive_graph_snapshot, seed_archive_graph
from tests.storage.trading_facts_helpers import NOW, user_broker_model

OLD_REVISION = "0006_strict_state_invariants"
NEW_REVISION = "0007_portfolio_run_index"
INDEX = "ix_portfolio_snapshots_run_id"


def seed_tied_snapshots(engine):
    seed_archive_graph(engine)
    with Session(engine) as session, session.begin():
        broker = user_broker_model("scope-2", "account-2")
        broker.environment = "PROD"
        session.add(broker)
        session.flush()
        for run_id, day in (("tie-1", 1), ("tie-2", 2)):
            session.add(
                PortfolioSnapshotRunModel(
                    id=run_id,
                    captured_at=NOW + timedelta(days=2),
                    bucket_start=NOW + timedelta(days=day),
                    safe_errors=[],
                    created_at=NOW,
                )
            )
            session.flush()
            for scope, account, currency in (
                ("scope-2", "z", "USD"),
                ("scope-1", "z", "RUB"),
                ("scope-2", "a", "RUB"),
                ("scope-1", "a", "RUB"),
                ("scope-1", "a", "EUR"),
            ):
                session.add(
                    PortfolioSnapshotModel(
                        id=f"{run_id}-{scope}-{account}-{currency}",
                        run_id=run_id,
                        user_broker_id=scope,
                        account_id=account,
                        currency=currency,
                        total_value=100,
                        free_cash=25,
                        cumulative_pnl=-3,
                        captured_at=NOW + timedelta(days=2),
                        bucket_start=NOW + timedelta(days=day),
                        created_at=NOW,
                    )
                )


def snapshot_semantics(engine):
    with Session(engine) as session:
        result = {}
        for environment in (None, "TEST", "PROD"):
            repository = PortfolioSnapshotRepository(session, environment=environment)
            for run_id in ("tie-1", "tie-2", "missing"):
                rows = repository.latest_snapshots(run_id)
                identities = [(row.currency, row.user_broker_id, row.account_id) for row in rows]
                expected = (
                    {
                        None: [
                            ("EUR", "scope-1", "a"),
                            ("RUB", "scope-1", "a"),
                            ("RUB", "scope-1", "z"),
                            ("RUB", "scope-2", "a"),
                            ("USD", "scope-2", "z"),
                        ],
                        "TEST": [("EUR", "scope-1", "a"), ("RUB", "scope-1", "a"), ("RUB", "scope-1", "z")],
                        "PROD": [("RUB", "scope-2", "a"), ("USD", "scope-2", "z")],
                    }[environment]
                    if run_id != "missing"
                    else []
                )
                assert identities == expected
                assert all(row.run_id == run_id for row in rows)
                result[environment, run_id] = rows
        return result


def index_catalogue(engine):
    inspector = inspect(engine)
    return {
        table: [
            {**index, "dialect_options": {key: str(value) for key, value in index.get("dialect_options", {}).items()}}
            for index in inspector.get_indexes(table)
        ]
        for table in inspector.get_table_names()
    }


def assert_run_index_roundtrip(config, engine):
    if current_schema_revision(engine) is None:
        command.upgrade(config, OLD_REVISION)
    else:
        command.downgrade(config, OLD_REVISION)
    seed_tied_snapshots(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE INDEX task6_extra_cash ON portfolio_snapshots (free_cash)"))
    before_rows = archive_graph_snapshot(engine)
    before_indexes = index_catalogue(engine)
    before_results = snapshot_semantics(engine)
    command.upgrade(config, "head")
    indexes = {item["name"]: item for item in inspect(engine).get_indexes("portfolio_snapshots")}
    assert INDEX in indexes, "run_id index missing after upgrade"
    assert indexes[INDEX]["column_names"] == ["run_id"]
    assert not indexes[INDEX]["unique"]
    assert current_schema_revision(engine) == NEW_REVISION
    assert archive_graph_snapshot(engine) == before_rows
    assert snapshot_semantics(engine) == before_results
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO portfolio_snapshots "
                "(id, run_id, user_broker_id, account_id, currency, total_value, free_cash, "
                "cumulative_pnl, captured_at, bucket_start, created_at) "
                "SELECT 'after-upgrade', run_id, user_broker_id, 'new-account', currency, "
                "total_value, free_cash, cumulative_pnl, captured_at, bucket_start, created_at "
                "FROM portfolio_snapshots WHERE id = 'snapshot-1'"
            )
        )
    before_rows = archive_graph_snapshot(engine)
    after_indexes = index_catalogue(engine)
    command.downgrade(config, OLD_REVISION)
    assert current_schema_revision(engine) == OLD_REVISION
    assert index_catalogue(engine) == before_indexes
    assert archive_graph_snapshot(engine) == before_rows
    assert snapshot_semantics(engine) == before_results
    command.upgrade(config, NEW_REVISION)
    assert index_catalogue(engine) == after_indexes
    assert archive_graph_snapshot(engine) == before_rows
    assert snapshot_semantics(engine) == before_results


def test_sqlite_run_index_roundtrip_preserves_financial_graph_and_latest_ties(tmp_path):
    url = f"sqlite:///{tmp_path / 'run-index.db'}"
    engine = create_database_engine(url)
    try:
        assert_run_index_roundtrip(migration_config(url), engine)
    finally:
        engine.dispose()
