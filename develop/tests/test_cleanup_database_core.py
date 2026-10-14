from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from develop.scripts.cleanup_database import run_cleanup
from moex_sentinel.storage.models import (
    PortfolioSnapshotModel,
    PortfolioSnapshotRunModel,
    PositionCycleModel,
    PositionValuationSnapshotModel,
    TradeAuditEventModel,
    TradingSubmitReservationModel,
)
from tests.storage.trading_facts_helpers import (
    NOW,
    automation_model,
    decision_model,
    instrument_model,
    order_model,
    position_cycle_model,
    seed_cycle,
    user_broker_model,
)

pytest_plugins = ("tests.integration.postgresql.conftest",)


pytestmark = pytest.mark.postgresql

_NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
_CUTOFF = datetime(2026, 9, 10, 12, tzinfo=UTC)
_OLD = datetime(2026, 9, 1, 12, tzinfo=UTC)
_NEW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def _valuation(snapshot_id: str, *, cycle_id: str, captured_at: datetime) -> PositionValuationSnapshotModel:
    return PositionValuationSnapshotModel(
        id=snapshot_id,
        user_broker_id="scope-1",
        automation_id="automation-1",
        position_cycle_id=cycle_id,
        instrument_id="instrument-1",
        quantity_lots=1,
        average_price=Decimal("10"),
        current_price=Decimal("11"),
        invested_amount=Decimal("100"),
        market_value=Decimal("110"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("10"),
        net_pnl=Decimal("10"),
        actual_commissions=Decimal("0"),
        source="TEST",
        captured_at=captured_at,
        created_at=captured_at,
    )


def _portfolio_snapshot(
    snapshot_id: str,
    *,
    run_id: str,
    user_broker_id: str,
    account_id: str,
    currency: str,
    bucket_start: datetime,
    captured_at: datetime,
) -> PortfolioSnapshotModel:
    return PortfolioSnapshotModel(
        id=snapshot_id,
        run_id=run_id,
        user_broker_id=user_broker_id,
        account_id=account_id,
        total_value=Decimal("100"),
        free_cash=Decimal("50"),
        cumulative_pnl=Decimal("0"),
        currency=currency,
        captured_at=captured_at,
        bucket_start=bucket_start,
        created_at=captured_at,
    )


def _audit(event_id: str, *, occurred_at: datetime, critical: bool) -> TradeAuditEventModel:
    scope = "scope-2" if event_id == "audit-reserved" else "scope-1"
    automation = "automation-2" if scope == "scope-2" else "automation-1"
    instrument = "instrument-2" if scope == "scope-2" else "instrument-1"
    return TradeAuditEventModel(
        event_id=event_id,
        process_id=f"process-{event_id}",
        parent_process_id=None,
        user_broker_id=scope,
        automation_id=automation,
        decision_id=None,
        broker_order_id=None,
        execution_id=None,
        instrument_id=instrument,
        level="CRITICAL" if critical else "INFO",
        stage="TEST_STAGE",
        safe_message="synthetic audit row",
        data={},
        occurred_at=occurred_at,
        created_at=occurred_at,
        critical=critical,
    )


def test_core_dry_run_and_apply_prune_only_expired_noncritical_unprotected_rows(
    isolated_postgresql_database_url,
) -> None:
    engine = create_engine(isolated_postgresql_database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory.begin() as session:
            session.add_all([user_broker_model("scope-1", "account-1"), user_broker_model("scope-2", "account-2")])
            session.flush()
            session.add_all(
                [
                    instrument_model("instrument-1", "scope-1"),
                    instrument_model("instrument-2", "scope-2"),
                ]
            )
            session.flush()
            session.add_all(
                [
                    automation_model("automation-1"),
                    automation_model("automation-2", user_broker_id="scope-2", instrument_id="instrument-2"),
                ]
            )
            session.flush()
            session.add_all(
                [
                    position_cycle_model("cycle-old-and-new", state="CLOSED", closed=True),
                    position_cycle_model("cycle-old-only", state="CLOSED", closed=True),
                    PositionCycleModel(
                        id="cycle-tied-latest",
                        user_broker_id="scope-1",
                        automation_id="automation-1",
                        instrument_id="instrument-1",
                        state="CLOSED",
                        quantity_lots=0,
                        average_entry_price=Decimal("10"),
                        invested_amount=Decimal("0"),
                        realized_pnl=Decimal("0"),
                        unrealized_pnl=Decimal("0"),
                        net_pnl=Decimal("0"),
                        accumulated_commissions=Decimal("0"),
                        opened_at=NOW,
                        closed_at=NOW,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    _audit("audit-old", occurred_at=_OLD, critical=False),
                    _audit("audit-critical", occurred_at=_OLD, critical=True),
                    _audit("audit-at-cutoff", occurred_at=_CUTOFF, critical=False),
                    _audit("audit-reserved", occurred_at=_OLD, critical=False),
                    _valuation("value-prunable", cycle_id="cycle-old-and-new", captured_at=_OLD),
                    _valuation("value-latest", cycle_id="cycle-old-and-new", captured_at=_NEW),
                    _valuation("value-only-old", cycle_id="cycle-old-only", captured_at=_OLD),
                    _valuation("value-tied-1", cycle_id="cycle-tied-latest", captured_at=_OLD),
                    _valuation("value-tied-2", cycle_id="cycle-tied-latest", captured_at=_OLD),
                ]
            )
            for run_id in ("run-old", "run-new", "run-other"):
                captured_at = _NEW if run_id == "run-new" else _OLD
                if run_id == "run-other":
                    captured_at = datetime(2026, 9, 1, 12, 1, tzinfo=UTC)
                session.add(
                    PortfolioSnapshotRunModel(
                        id=run_id,
                        captured_at=captured_at,
                        bucket_start=captured_at,
                        safe_errors=[],
                        created_at=captured_at,
                    )
                )
            session.flush()
            session.add_all(
                [
                    _portfolio_snapshot(
                        "portfolio-prunable",
                        run_id="run-old",
                        user_broker_id="scope-1",
                        account_id="account-1",
                        currency="RUB",
                        bucket_start=_OLD,
                        captured_at=_OLD,
                    ),
                    _portfolio_snapshot(
                        "portfolio-latest",
                        run_id="run-new",
                        user_broker_id="scope-1",
                        account_id="account-1",
                        currency="RUB",
                        bucket_start=_NEW,
                        captured_at=_NEW,
                    ),
                    _portfolio_snapshot(
                        "portfolio-only-old",
                        run_id="run-other",
                        user_broker_id="scope-2",
                        account_id="account-2",
                        currency="USD",
                        bucket_start=datetime(2026, 9, 1, 12, 1, tzinfo=UTC),
                        captured_at=_OLD,
                    ),
                ]
            )
            session.add(
                TradingSubmitReservationModel(
                    reservation_id="reservation-unknown",
                    user_broker_id="scope-2",
                    automation_id="automation-2",
                    idempotency_key="idempotency-unknown",
                    request_fingerprint="f" * 64,
                    state="UNKNOWN",
                    runtime_instance_id="runtime-1",
                    generation=1,
                    configuration_fingerprint="c" * 64,
                    permit_id=None,
                    side="BUY",
                    instrument_uid="uid-2",
                    quantity_lots=1,
                    limit_price=Decimal("10"),
                    requested_amount=Decimal("10"),
                    reserved_cash=Decimal("10"),
                    reserved_position_units=Decimal("0"),
                    payload={},
                    created_at=_OLD,
                    updated_at=_OLD,
                )
            )

        dry_run = run_cleanup(engine, "core", now=_NOW, apply=False, batch_size=1)

        assert dry_run["cutoff_utc"] == _CUTOFF.isoformat().replace("+00:00", "Z")
        assert dry_run["mode"] == "dry-run"
        assert dry_run["tables"]["trade_audit_events"]["would_delete"] == 1
        assert dry_run["tables"]["position_valuation_snapshots"]["would_delete"] == 1
        assert dry_run["tables"]["portfolio_snapshots"]["would_delete"] == 1
        assert sum(item["would_delete"] for item in dry_run["tables"].values()) == 3
        assert sum(item["deleted"] for item in dry_run["tables"].values()) == 0
        assert len(dry_run["tables"]) == 23
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(TradeAuditEventModel)) == 4
            assert session.scalar(select(func.count()).select_from(PositionValuationSnapshotModel)) == 5
            assert session.scalar(select(func.count()).select_from(PortfolioSnapshotModel)) == 3

        applied = run_cleanup(engine, "core", now=_NOW, apply=True, batch_size=1)

        assert applied["mode"] == "apply"
        assert sum(item["would_delete"] for item in applied["tables"].values()) == 3
        assert sum(item["deleted"] for item in applied["tables"].values()) == 3
        assert applied["tables"]["trade_audit_events"]["deleted"] == 1
        assert applied["tables"]["position_valuation_snapshots"]["deleted"] == 1
        assert applied["tables"]["portfolio_snapshots"]["deleted"] == 1
        with factory() as session:
            audit_ids = set(session.scalars(select(TradeAuditEventModel.event_id)))
            valuation_ids = set(session.scalars(select(PositionValuationSnapshotModel.id)))
            portfolio_ids = set(session.scalars(select(PortfolioSnapshotModel.id)))
            run_ids = set(session.scalars(select(PortfolioSnapshotRunModel.id)))
            assert audit_ids == {"audit-critical", "audit-at-cutoff", "audit-reserved"}
            assert valuation_ids == {
                "value-latest",
                "value-only-old",
                "value-tied-1",
                "value-tied-2",
            }
            assert portfolio_ids == {"portfolio-latest", "portfolio-only-old"}
            assert run_ids == {"run-old", "run-new", "run-other"}
    finally:
        engine.dispose()


def test_core_calendar_month_cutoff_clamps_month_end(isolated_postgresql_database_url) -> None:
    engine = create_engine(isolated_postgresql_database_url)
    try:
        result = run_cleanup(
            engine,
            "core",
            now=datetime(2026, 3, 31, 8, tzinfo=UTC),
            apply=False,
        )
        assert result["cutoff_utc"] == "2026-02-28T08:00:00Z"
    finally:
        engine.dispose()


def test_core_cutoff_is_utc_when_postgres_session_timezone_is_non_utc(isolated_postgresql_database_url) -> None:
    engine = create_engine(isolated_postgresql_database_url)

    @event.listens_for(engine, "connect")
    def set_non_utc_session_timezone(dbapi_connection, _connection_record) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'Etc/GMT+5'")

    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory.begin() as session:
            session.add(user_broker_model("scope-1", "account-1"))
            session.flush()
            session.add(instrument_model("instrument-1", "scope-1"))
            session.flush()
            session.add(automation_model("automation-1"))
            session.flush()
            session.add(
                _audit(
                    "audit-newer-than-cutoff",
                    occurred_at=datetime(2026, 9, 10, 13, tzinfo=UTC),
                    critical=False,
                )
            )

        result = run_cleanup(engine, "core", now=_NOW, apply=True)

        assert result["tables"]["trade_audit_events"]["would_delete"] == 0
        assert result["tables"]["trade_audit_events"]["deleted"] == 0
        with factory() as session:
            assert session.get(TradeAuditEventModel, "audit-newer-than-cutoff") is not None
    finally:
        engine.dispose()


@pytest.mark.parametrize("state", ["SUBMITTED", "FILLED"])
def test_core_retains_audit_when_order_is_nonterminal_or_lacks_terminal_timestamp(
    isolated_postgresql_database_url, state: str
) -> None:
    engine = create_engine(isolated_postgresql_database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory.begin() as session:
            seed_cycle(session)
            decision = decision_model()
            session.add(decision)
            session.flush()
            order = order_model("order-1")
            order.state = state
            order.terminal_at = None
            session.add(order)
            session.add(_audit("audit-order-protected", occurred_at=_OLD, critical=False))

        result = run_cleanup(engine, "core", now=_NOW, apply=True)

        assert result["tables"]["trade_audit_events"]["would_delete"] == 0
        assert result["tables"]["trade_audit_events"]["deleted"] == 0
        with factory() as session:
            assert session.get(TradeAuditEventModel, "audit-order-protected") is not None
    finally:
        engine.dispose()


@pytest.mark.parametrize("batch_size", [0, -1, 100_001])
def test_core_rejects_invalid_batch_size(isolated_postgresql_database_url, batch_size: int) -> None:
    engine = create_engine(isolated_postgresql_database_url)
    try:
        with pytest.raises(ValueError, match="batch_size"):
            run_cleanup(engine, "core", now=_NOW, apply=False, batch_size=batch_size)
    finally:
        engine.dispose()
