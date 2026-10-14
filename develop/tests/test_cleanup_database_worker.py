import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from develop.scripts.cleanup_database import main, run_cleanup
from trading_automaton.storage.models import (
    AccountCommissionProfileModel,
    Base,
    BusinessAuditEventModel,
    CachedAutomationModel,
    FactOutboxModel,
    LocalIntentModel,
    LotAllocationModel,
    TradeDecisionModel,
    TradeLotModel,
    TradingCycleStateModel,
    WorkerRunModel,
)

_NOW = datetime(2026, 3, 31, 12, 0, tzinfo=UTC)
_CUTOFF = datetime(2026, 2, 28, 12, 0, tzinfo=UTC)
_OLD = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
_NEW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
_TABLES = {
    "cached_automations",
    "fact_outbox",
    "worker_runs",
    "account_commission_profiles",
    "business_audit_events",
    "broker_intents",
    "trade_decisions",
    "trade_lots",
    "lot_allocations",
    "trading_cycle_states",
}


@pytest.fixture
def worker_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, factory
    finally:
        engine.dispose()


def _automation(automation_id: str) -> CachedAutomationModel:
    return CachedAutomationModel(
        automation_id=automation_id,
        state="IN_WORK",
        revision=1,
        last_sequence_number=0,
    )


def _audit(
    event_id: str,
    automation_id: str,
    *,
    occurred_at: datetime = _OLD,
    critical: bool = False,
) -> BusinessAuditEventModel:
    return BusinessAuditEventModel(
        event_id=event_id,
        process_id=f"process-{event_id}",
        automation_id=automation_id,
        broker_id="broker-1",
        account_id="account-1",
        instrument_id="instrument-1",
        level="CRITICAL" if critical else "INFO",
        stage="TEST_STAGE",
        message="synthetic audit event",
        event_data={"reason": "fixture"},
        occurred_at=occurred_at,
        critical=critical,
    )


def _intent(
    intent_id: str,
    automation_id: str,
    state: str,
    *,
    terminal_at: datetime | None = None,
    facts_emitted_at: datetime | None = None,
) -> LocalIntentModel:
    return LocalIntentModel(
        idempotency_key=intent_id,
        automation_id=automation_id,
        kind="ENTRY",
        side="BUY",
        state=state,
        quantity_lots=1,
        limit_price=Decimal("10"),
        strategy_snapshot={"strategy": "synthetic"},
        created_at=_OLD,
        updated_at=_OLD,
        terminal_at=terminal_at,
        execution_facts_emitted_at=facts_emitted_at,
    )


def _outbox(event_id: str, automation_id: str, delivery_state: str) -> FactOutboxModel:
    return FactOutboxModel(
        event_id=event_id,
        user_broker_id="broker-link-1",
        automation_id=automation_id,
        sequence_number=1,
        expected_revision=1,
        fact_kind="ORDER_FILLED",
        payload={"synthetic": True},
        safe_message="synthetic outbox item",
        occurred_at=_OLD,
        delivery_state=delivery_state,
    )


def _seed_protected_protocol_rows(factory) -> None:
    with factory.begin() as session:
        session.add_all(
            [
                _automation("active"),
                _automation("uncertain"),
                _automation("filled-unemitted"),
                _automation("terminal-unfinalized"),
                _automation("outbox-pending"),
                _automation("outbox-failed"),
                _automation("completed"),
            ]
        )
        session.flush()
        session.add_all(
            [
                _intent("intent-active", "active", "SUBMITTED"),
                _intent("intent-uncertain", "uncertain", "UNCERTAIN"),
                _intent(
                    "intent-filled-unemitted",
                    "filled-unemitted",
                    "FILLED",
                    terminal_at=_OLD,
                ),
                _intent("intent-terminal-unfinalized", "terminal-unfinalized", "REJECTED"),
                _intent("intent-completed", "completed", "FILLED", terminal_at=_OLD, facts_emitted_at=_OLD),
                _outbox("outbox-pending-event", "outbox-pending", "PENDING"),
                _outbox("outbox-failed-event", "outbox-failed", "FAILED"),
            ]
        )
        session.add_all(
            [
                _audit("audit-active", "active"),
                _audit("audit-uncertain", "uncertain"),
                _audit("audit-filled-unemitted", "filled-unemitted"),
                _audit("audit-terminal-unfinalized", "terminal-unfinalized"),
                _audit("audit-outbox-pending", "outbox-pending"),
                _audit("audit-outbox-failed", "outbox-failed"),
                _audit("audit-completed", "completed"),
                _audit("audit-critical", "completed", critical=True),
                _audit("audit-recent", "completed", occurred_at=_NEW),
            ]
        )


def _row_ids(session, model, column):
    return set(session.scalars(select(column)))


def test_worker_dry_run_uses_strict_calendar_month_cutoff_and_changes_nothing(worker_db):
    engine, factory = worker_db
    with factory.begin() as session:
        session.add(_automation("completed"))
        session.add_all(
            [
                _audit(
                    "audit-before-cutoff",
                    "completed",
                    occurred_at=datetime(2026, 2, 28, 11, 59, 59, 999999, tzinfo=UTC),
                ),
                _audit("audit-at-cutoff", "completed", occurred_at=_CUTOFF),
                _audit("audit-after-cutoff", "completed", occurred_at=_NEW),
            ]
        )

    result = run_cleanup(engine, "worker", now=_NOW, apply=False, batch_size=2)

    assert result["store"] == "worker"
    assert result["mode"] == "dry-run"
    assert result["cutoff_utc"] == "2026-02-28T12:00:00Z"
    assert result["batch_size"] == 2
    assert set(result["tables"]) == _TABLES
    for table_report in result["tables"].values():
        assert set(table_report) == {"would_delete", "deleted", "preserved_reason"}
        assert table_report["preserved_reason"]
    assert result["tables"]["business_audit_events"]["would_delete"] == 1
    assert result["tables"]["business_audit_events"]["deleted"] == 0
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(BusinessAuditEventModel)) == 3


def test_worker_apply_deletes_only_old_noncritical_audit_for_finalized_automations(worker_db):
    engine, factory = worker_db
    _seed_protected_protocol_rows(factory)

    result = run_cleanup(engine, "worker", now=_NOW, apply=True, batch_size=2)

    assert result["store"] == "worker"
    assert result["mode"] == "apply"
    audit_report = result["tables"]["business_audit_events"]
    assert audit_report["would_delete"] == 1
    assert audit_report["deleted"] == 1
    assert result["tables"]["cached_automations"]["preserved_reason"]
    assert result["tables"]["fact_outbox"]["preserved_reason"]
    with factory() as session:
        assert _row_ids(session, BusinessAuditEventModel, BusinessAuditEventModel.event_id) == {
            "audit-active",
            "audit-uncertain",
            "audit-filled-unemitted",
            "audit-terminal-unfinalized",
            "audit-outbox-pending",
            "audit-outbox-failed",
            "audit-critical",
            "audit-recent",
        }
        assert _row_ids(session, FactOutboxModel, FactOutboxModel.event_id) == {
            "outbox-pending-event",
            "outbox-failed-event",
        }
        assert _row_ids(session, LocalIntentModel, LocalIntentModel.idempotency_key) == {
            "intent-active",
            "intent-uncertain",
            "intent-filled-unemitted",
            "intent-terminal-unfinalized",
            "intent-completed",
        }


def test_worker_apply_deletes_candidates_across_multiple_batches(worker_db):
    engine, factory = worker_db
    with factory.begin() as session:
        session.add(_automation("completed"))
        session.add_all([_audit(f"audit-{index}", "completed") for index in range(5)])

    result = run_cleanup(engine, "worker", now=_NOW, apply=True, batch_size=2)

    assert result["batch_size"] == 2
    assert result["tables"]["business_audit_events"]["would_delete"] == 5
    assert result["tables"]["business_audit_events"]["deleted"] == 5
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(BusinessAuditEventModel)) == 0


def test_worker_batch_failure_keeps_prior_commit_and_later_rows(worker_db):
    engine, factory = worker_db
    with factory.begin() as session:
        session.add_all([_automation("completed"), _automation("protocol")])
        session.add_all([_audit(event_id, "completed") for event_id in ("a", "b", "c")])
        session.add(_intent("protocol-intent", "protocol", "SUBMITTED"))
        session.add(_outbox("protocol-outbox", "protocol", "PENDING"))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER fail_second_audit_delete
            BEFORE DELETE ON business_audit_events
            WHEN OLD.event_id = 'b'
            BEGIN
                SELECT RAISE(ABORT, 'synthetic failure token');
            END
            """
        )

    with factory() as session:
        intent_ids_before = _row_ids(session, LocalIntentModel, LocalIntentModel.idempotency_key)
        outbox_ids_before = _row_ids(session, FactOutboxModel, FactOutboxModel.event_id)

    with pytest.raises(IntegrityError):
        run_cleanup(engine, "worker", now=_NOW, apply=True, batch_size=1)

    with factory() as session:
        assert _row_ids(session, BusinessAuditEventModel, BusinessAuditEventModel.event_id) == {"b", "c"}
        assert _row_ids(session, LocalIntentModel, LocalIntentModel.idempotency_key) == intent_ids_before
        assert _row_ids(session, FactOutboxModel, FactOutboxModel.event_id) == outbox_ids_before

    dry_run = run_cleanup(engine, "worker", now=_NOW, apply=False, batch_size=1)
    assert dry_run["tables"]["business_audit_events"]["would_delete"] == 2
    assert dry_run["tables"]["business_audit_events"]["deleted"] == 0


def test_worker_apply_preserves_all_non_audit_protocol_and_ledger_rows(worker_db):
    engine, factory = worker_db
    _seed_protected_protocol_rows(factory)
    with factory.begin() as session:
        session.add(WorkerRunModel(worker_id="worker-1", clean_shutdown=False))
        session.add(
            AccountCommissionProfileModel(
                broker_id="broker-1",
                account_id="account-1",
                instrument_type="SHARE",
                currency="RUB",
                buy_rate=Decimal("0.01"),
                sell_rate=Decimal("0.01"),
                service_rate=Decimal("0"),
                deal_rate=Decimal("0"),
                source="TEST",
                calculated_at=_OLD,
                valid_until=_NEW,
            )
        )
        session.add(
            TradeDecisionModel(
                id="decision-1",
                automation_id="completed",
                broker_id="broker-1",
                instrument_id="instrument-1",
                occurred_at=_OLD,
                quantity_lots=1,
                lot_size=1,
                average_price=Decimal("10"),
                current_price=Decimal("11"),
                best_bid=Decimal("10.9"),
                best_ask=Decimal("11"),
                invested_amount=Decimal("10"),
                estimated_commission=Decimal("0"),
                decision="WAIT",
                reason_code="TEST",
                decision_quantity_lots=0,
                strategy_snapshot={"synthetic": True},
            )
        )
        session.add(
            TradeLotModel(
                id="lot-1",
                automation_id="completed",
                source="TEST",
                original_lots=1,
                remaining_lots=1,
                entry_price=Decimal("10"),
                entry_commission=Decimal("0"),
                opened_at=_OLD,
            )
        )
        session.add(
            LotAllocationModel(
                id="allocation-1",
                automation_id="completed",
                sell_intent_id="sell-1",
                lot_id="lot-1",
                quantity_lots=1,
                exit_price=Decimal("11"),
                exit_commission=Decimal("0"),
                realized_pnl=Decimal("1"),
                closed_at=_OLD,
            )
        )
        session.add(
            TradingCycleStateModel(
                automation_id="completed",
                sell_armed=True,
                updated_at=_OLD,
            )
        )

    with factory() as session:
        before = {
            table.name: session.execute(select(table)).all()
            for table in Base.metadata.sorted_tables
            if table.name != "business_audit_events"
        }

    run_cleanup(engine, "worker", now=_NOW, apply=True, batch_size=1)

    with factory() as session:
        after = {
            table.name: session.execute(select(table)).all()
            for table in Base.metadata.sorted_tables
            if table.name != "business_audit_events"
        }
    assert after == before


def test_worker_missing_sqlite_database_is_not_created(tmp_path):
    missing = tmp_path / "must-not-be-created.sqlite3"
    engine = create_engine(f"sqlite:///{missing}")
    try:
        with pytest.raises(FileNotFoundError):
            run_cleanup(engine, "worker", now=_NOW, apply=False)
        assert not missing.exists()
    finally:
        engine.dispose()


def test_worker_cli_defaults_to_dry_run(monkeypatch, capsys, worker_db):
    engine, factory = worker_db
    with factory.begin() as session:
        session.add(_automation("completed"))
        session.add(_audit("audit-cli", "completed"))
    monkeypatch.setenv("AUTOMATON_DATABASE_URL", engine.url.render_as_string(hide_password=False))

    assert main(["--store", "worker"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    assert report["tables"]["business_audit_events"]["would_delete"] == 1
    assert report["tables"]["business_audit_events"]["deleted"] == 0
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(BusinessAuditEventModel)) == 1


def test_worker_cli_does_not_create_missing_database(monkeypatch, capsys, tmp_path):
    missing = tmp_path / "missing-cli.sqlite3"
    monkeypatch.setenv("AUTOMATON_DATABASE_URL", f"sqlite:///{missing}")

    assert main(["--store", "worker"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Database cleanup failed" in captured.err
    assert not missing.exists()


def test_worker_cli_apply_deletes_only_eligible_rows(monkeypatch, capsys, worker_db):
    engine, factory = worker_db
    recent = datetime.now(UTC)
    with factory.begin() as session:
        session.add(_automation("completed"))
        session.add_all(
            [
                _audit("audit-cli-eligible", "completed"),
                _audit("audit-cli-critical", "completed", critical=True),
                _audit("audit-cli-recent", "completed", occurred_at=recent),
            ]
        )
    monkeypatch.setenv("AUTOMATON_DATABASE_URL", engine.url.render_as_string(hide_password=False))

    assert main(["--store", "worker", "--apply", "--batch-size", "1"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "apply"
    assert report["batch_size"] == 1
    assert report["tables"]["business_audit_events"]["would_delete"] == 1
    assert report["tables"]["business_audit_events"]["deleted"] == 1
    with factory() as session:
        assert _row_ids(session, BusinessAuditEventModel, BusinessAuditEventModel.event_id) == {
            "audit-cli-critical",
            "audit-cli-recent",
        }
    assert [path.name for path in Path(engine.url.database).parent.iterdir()] == ["worker.sqlite3"]
