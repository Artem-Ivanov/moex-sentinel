"""Rehearse fixture PG dump + SQLite backup into new disposable stores.

Requires pg_dump/pg_restore matching the server major. Never uses production databases.
"""

import asyncio
import importlib.util
import os
import re
import runpy
import shutil
import subprocess
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from moex_sentinel.domain.trading_facts import (
    AutomationEnvelopeDraft,
    BrokerOrderDraft,
    BrokerOrderEventDraft,
    PositionCycleDraft,
    PositionLotDraft,
    TradeAuditEventDraft,
    TradeDecisionDraft,
    TradeExecutionDraft,
    TradingAutomationDraft,
)
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from sentinel_contracts.broker_execution import BrokerOrderState
from tests.domain.trading_facts_helpers import all_fact_drafts
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.worker_storage_helpers import (
    INTENT_ID,
    NOW,
    SELL_INTENT_ID,
    baseline_command,
    filled_buy,
)
from trading_automaton.services.uncertain_intent_reconciliation import UncertainIntentReconciliationService
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base
from trading_automaton.storage.repository import IntentBatchItem, LocalAutomationRepository, TradingCycleState

SPEC = importlib.util.spec_from_file_location(
    "paired_backup", Path(__file__).parents[3] / "develop/scripts/paired_backup.py"
)
assert SPEC
assert SPEC.loader
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


def fingerprint(engine):
    metadata = MetaData()
    metadata.reflect(engine)
    with engine.connect() as connection:
        return {
            name: sorted((tuple(map(str, row)) for row in connection.execute(select(table))), key=repr)
            for name, table in metadata.tables.items()
        }


REPAIR = runpy.run_path(str(Path(__file__).parents[3] / "develop/scripts/repair_execution_prices.py"))


def journal(engine):
    # Exercise the actual repair journal schema, not the repair algorithm.
    with engine.begin() as connection:
        table = REPAIR["journal"](connection)
        table.create(connection)
        connection.execute(
            table.insert().values(
                plan_id="synthetic-repair", status="COMPLETE", plan={"version": 1}, created_at=NOW.isoformat()
            )
        )


def assert_journal(engine):
    with engine.connect() as connection:
        table = REPAIR["journal"](connection)
        row = connection.execute(select(table)).mappings().one()
        assert row["status"] == "COMPLETE"
        assert row["plan"] == {"version": 1}


def require_matching_tools(database_url):
    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            server_major = int(connection.scalar(text("SHOW server_version_num"))) // 10000
    finally:
        engine.dispose()
    for executable in ("pg_dump", "pg_restore"):
        binary = shutil.which(executable)
        if binary is None:
            pytest.skip(f"{executable} required for restore rehearsal")
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True).stdout
        if int(re.search(r"(\d+)\.", version).group(1)) != server_major:
            pytest.skip("PostgreSQL dump/restore tools must match the server major; restore not verified")


@contextmanager
def restored_pair(isolated_postgresql_database_url, postgresql_database_url, worker_path, tmp_path):
    schema_name = isolated_postgresql_database_url.query["options"].split("=")[-1]
    environment = os.environ.copy()
    environment.update(
        PGHOST=postgresql_database_url.host or "",
        PGPORT=str(postgresql_database_url.port or 5432),
        PGUSER=postgresql_database_url.username or "",
        PGPASSWORD=postgresql_database_url.password or "",
        PGDATABASE=postgresql_database_url.database or "",
    )
    dump = tmp_path / "core.dump"
    subprocess.run(
        [
            shutil.which("pg_dump"),
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--schema",
            schema_name,
            "--file",
            str(dump),
        ],
        env=environment,
        capture_output=True,
        check=True,
    )
    restored_path = tmp_path / "restored.sqlite"
    backup.backup_sqlite(worker_path, restored_path)
    database_name = "restore_" + uuid4().hex
    admin = create_database_engine(postgresql_database_url).execution_options(isolation_level="AUTOCOMMIT")
    restored_pg = None
    restored_worker = None
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}" TEMPLATE template0'))
        environment["PGDATABASE"] = database_name
        subprocess.run(
            [
                shutil.which("pg_restore"),
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
                "--dbname",
                database_name,
                str(dump),
            ],
            env=environment,
            capture_output=True,
            check=True,
        )
        restored_pg = create_database_engine(isolated_postgresql_database_url.set(database=database_name))
        restored_worker = create_worker_engine(f"sqlite:///{restored_path}")
        yield restored_pg, restored_worker
    finally:
        if restored_pg is not None:
            restored_pg.dispose()
        if restored_worker is not None:
            restored_worker.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()


@pytest.mark.postgresql
def test_paired_backup_restore_keeps_graph_and_reconciles_without_resubmit(
    isolated_postgresql_database_url: URL, postgresql_database_url: URL, tmp_path
):
    require_matching_tools(isolated_postgresql_database_url)
    source = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(source)
    with factory.begin() as session:
        session.add(user_broker_model("scope-1", "account-1"))
        session.flush()
        session.add(instrument_model("instrument-1", "scope-1"))
    drafts = {type(value): value for value in all_fact_drafts()}
    with TradingFactsUnitOfWork(factory) as uow:
        uow.automations.create("scope-1", drafts[TradingAutomationDraft])
        uow.positions.open_cycle("scope-1", drafts[PositionCycleDraft])
        uow.orders.append_decision("scope-1", drafts[TradeDecisionDraft])
        uow.orders.append_order("scope-1", drafts[BrokerOrderDraft])
        uow.orders.append_order_event("scope-1", drafts[BrokerOrderEventDraft])
        uow.orders.append_execution("scope-1", drafts[TradeExecutionDraft])
        uow.positions.append_lot("scope-1", drafts[PositionLotDraft])
        uow.audit.append_audit("scope-1", drafts[TradeAuditEventDraft])
        uow.audit.append_envelope("scope-1", drafts[AutomationEnvelopeDraft])
    journal(source)
    worker_path = tmp_path / "live.sqlite"
    worker = create_worker_engine(f"sqlite:///{worker_path}")
    Base.metadata.create_all(worker)
    repository = LocalAutomationRepository(
        sessionmaker(worker, expire_on_commit=False), fact_writer=FactOutboxWriter(clock=lambda: NOW)
    )
    command = baseline_command()
    repository.cache_command(command)
    repository.save_decision_batch(
        (
            decision_item(command).model_copy(
                update={
                    "intent": IntentBatchItem(INTENT_ID, "BUY_MORE", "BUY", 2, Decimal("100")),
                    "decision_quantity_lots": 2,
                }
            ),
        ),
        occurred_at=NOW,
    )
    repository.finalize_execution(filled_buy().model_copy(update={"quantity_lots": 2, "executed_lots": 2}))
    repository.save_decision_batch(
        (
            decision_item(command).model_copy(
                update={"intent": IntentBatchItem(SELL_INTENT_ID, "SELL_ALL", "SELL", 1, Decimal("101"))}
            ),
        ),
        occurred_at=NOW,
    )
    repository.finalize_execution(
        filled_buy().model_copy(
            update={
                "intent_id": SELL_INTENT_ID,
                "side": "SELL",
                "requested_price": Decimal("101"),
                "executed_price": Decimal("101"),
                "broker_order_id": "synthetic-sell",
            }
        )
    )
    repository.save_cycle_state(
        TradingCycleState(str(command.automation_id), Decimal("98.25"), NOW, True, Decimal("101.25"), NOW)
    )
    pending_id = str(uuid4())
    repository.save_decision_batch(
        (
            decision_item(command).model_copy(
                update={"intent": IntentBatchItem(pending_id, "BUY_MORE", "BUY", 1, Decimal("100"))}
            ),
        ),
        occurred_at=NOW,
    )
    repository.update_intent(pending_id, state="UNCERTAIN", broker_order_id=None, occurred_at=NOW)
    repository.begin_run("synthetic-worker")
    journal(worker)
    original_pg, original_worker = fingerprint(source), fingerprint(worker)
    try:
        with restored_pair(isolated_postgresql_database_url, postgresql_database_url, worker_path, tmp_path) as (
            restored_pg,
            restored_worker,
        ):
            assert fingerprint(restored_pg) == original_pg
            assert fingerprint(restored_worker) == original_worker
            restored_repository = LocalAutomationRepository(
                sessionmaker(restored_worker, expire_on_commit=False), fact_writer=FactOutboxWriter(clock=lambda: NOW)
            )
            assert restored_repository.begin_run("synthetic-worker") is True
            assert restored_repository.get_active_intent(str(command.automation_id)).idempotency_key == pending_id

            class Broker:
                submits = 0

                async def get_order_state(self, *args):
                    return None

                async def find_by_idempotency_key(self, account, key):
                    assert key == pending_id
                    return BrokerOrderState(
                        broker_order_id="synthetic-recovered",
                        idempotency_key=key,
                        status="FILLED",
                        requested_lots=1,
                        executed_lots=1,
                        requested_amount=Decimal("1000"),
                        executed_amount=Decimal("1000"),
                        estimated_commission=Decimal("1"),
                        executed_commission=Decimal("2"),
                        currency="RUB",
                        executed_price=Decimal("100"),
                        executed_at=NOW,
                    )

                async def inspect_position(self, *args):
                    return {"quantity_lots": Decimal("2")}

                async def inspect_recent_operations(self, *args):
                    return ()

                async def dispatch_limit_order(self, *args):
                    self.submits += 1
                    raise AssertionError("Recovery must not submit orders")

            broker = Broker()
            service = UncertainIntentReconciliationService(restored_repository, broker, now=lambda: NOW)
            asyncio.run(service.reconcile((command,)))
            after = fingerprint(restored_worker)
            asyncio.run(
                UncertainIntentReconciliationService(restored_repository, broker, now=lambda: NOW).reconcile((command,))
            )
            assert fingerprint(restored_worker) == after
            assert broker.submits == 0
            assert (
                sum(lot.remaining_lots for lot in restored_repository.list_open_lots(str(command.automation_id))) == 2
            )
            assert restored_repository.has_pending_fact_outbox(str(command.automation_id))
            assert_journal(restored_worker)
            assert_journal(restored_pg)
    finally:
        worker.dispose()
        source.dispose()


@pytest.mark.postgresql
def test_paired_restore_delivers_same_typed_events_after_lost_ack(
    isolated_postgresql_database_url: URL, postgresql_database_url: URL, tmp_path, monkeypatch
):
    require_matching_tools(isolated_postgresql_database_url)
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from moex_sentinel.api.app import create_app  # noqa: PLC0415
    from moex_sentinel.storage.models import AutomationEventModel, TradingAutomationModel  # noqa: PLC0415
    from tests.integration.test_core_worker_lifecycle import (  # noqa: PLC0415
        AUTOMATION_ID,
        FACT_ADAPTER,
        INSTRUMENT_ID,
        SCOPE_ID,
    )
    from tests.storage.trading_facts_helpers import automation_model  # noqa: PLC0415
    from trading_automaton.adapters.core_client import CoreClient  # noqa: PLC0415
    from trading_automaton.services.fact_synchronization import FactSynchronizationService  # noqa: PLC0415

    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "TEST")
    monkeypatch.setenv("BROKER_ACCESS_MODE", "TRADE")
    worker_path = tmp_path / "live.sqlite"
    worker = create_worker_engine(f"sqlite:///{worker_path}")
    Base.metadata.create_all(worker)
    repository = LocalAutomationRepository(
        sessionmaker(worker, expire_on_commit=False), fact_writer=FactOutboxWriter(clock=lambda: NOW)
    )
    app = create_app(test_auth_bypass=True, database_url=isolated_postgresql_database_url)
    try:
        with TestClient(app) as http:
            with app.state.session_factory.begin() as session:
                session.add(user_broker_model(str(SCOPE_ID), "synthetic-account"))
                session.flush()
                instrument = instrument_model(str(INSTRUMENT_ID), str(SCOPE_ID))
                instrument.ticker = "SYNTH"
                session.add(instrument)
                session.flush()
                session.add(
                    automation_model(
                        str(AUTOMATION_ID),
                        user_broker_id=str(SCOPE_ID),
                        instrument_id=str(INSTRUMENT_ID),
                        state="IN_QUEUE",
                    )
                )
            client = CoreClient(http)
            sync = FactSynchronizationService(repository, client, now=lambda: NOW, sleep=lambda _: None, deadline_ms=0)
            commands = sync.claim_commands("synthetic-worker", 10)
            assert len(commands) == 1
            repository.transition_state(
                automation_id=str(AUTOMATION_ID), state="IN_WORK", safe_message="Synthetic activation", occurred_at=NOW
            )
            rows = repository.ready_fact_outbox(100, now=NOW, deadline_ms=0)
            facts = [
                FACT_ADAPTER.validate_python(
                    {
                        name: getattr(row, name)
                        for name in (
                            "event_id",
                            "user_broker_id",
                            "automation_id",
                            "sequence_number",
                            "expected_revision",
                            "fact_kind",
                            "payload",
                            "safe_message",
                            "occurred_at",
                        )
                    }
                )
                for row in rows
            ]
            first = client.publish_facts(facts)  # Core accepted; deliberately drop ACK before local commit.
            assert first.failures == ()
            assert first.results[0].accepted_event_ids == (facts[0].event_id,)
            assert repository.has_pending_fact_outbox(str(AUTOMATION_ID))
            before_core, before_worker = fingerprint(app.state.database_engine), fingerprint(worker)
        with restored_pair(isolated_postgresql_database_url, postgresql_database_url, worker_path, tmp_path) as (
            restored_pg,
            restored_worker,
        ):
            assert fingerprint(restored_pg) == before_core
            assert fingerprint(restored_worker) == before_worker
            restored_repository = LocalAutomationRepository(
                sessionmaker(restored_worker, expire_on_commit=False), fact_writer=FactOutboxWriter(clock=lambda: NOW)
            )
            restored_app = create_app(test_auth_bypass=True, database_url=restored_pg.url)
            with TestClient(restored_app) as restored_http:
                restored_client = CoreClient(restored_http)
                restored_sync = FactSynchronizationService(
                    restored_repository, restored_client, now=lambda: NOW, sleep=lambda _: None, deadline_ms=0
                )
                assert restored_sync.flush_outbox() is True
                assert not restored_repository.has_pending_fact_outbox(str(AUTOMATION_ID))
                assert restored_client.publish_facts(facts) == first
                with restored_app.state.session_factory() as session:
                    automation = session.get_one(TradingAutomationModel, str(AUTOMATION_ID))
                    assert (automation.revision, automation.last_sequence_number) == (2, 1)
                    events = session.scalars(select(AutomationEventModel)).all()
                    assert [event.event_id for event in events] == [str(facts[0].event_id)]
                frontier = restored_repository.get_state(str(AUTOMATION_ID))
                assert (frontier.revision, frontier.last_sequence_number) == (2, 1)
    finally:
        worker.dispose()
