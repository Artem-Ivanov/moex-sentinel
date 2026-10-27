from decimal import Decimal

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from moex_sentinel.storage.models.order_facts import BrokerOrderModel
from moex_sentinel.storage.models.trading_admission import TradingSubmitReservationModel
from moex_sentinel.storage.models.trading_observability import AutomationEventModel
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from tests.storage.trading_facts_helpers import NOW, execution_model, seed_order


def test_execution_diagnostics_exact_fact_acceptance_on_postgresql(isolated_postgresql_database_url):
    engine = create_engine(isolated_postgresql_database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        seed_order(session)
        session.scalar(select(BrokerOrderModel)).fact_id = "accepted-order-fact"
        execution = execution_model("execution-1", external_execution_id="fill-1", source="BROKER_FILL")
        execution.fact_id = "accepted-execution-fact"
        session.add(execution)
        session.add(
            TradingSubmitReservationModel(
                reservation_id="reservation-1",
                user_broker_id="scope-1",
                automation_id="automation-1",
                idempotency_key="intent-1",
                request_fingerprint="a" * 64,
                state="UNKNOWN",
                runtime_instance_id="runtime-1",
                generation=2,
                configuration_fingerprint="b" * 64,
                side="BUY",
                instrument_uid="TEST@TQBR",
                quantity_lots=3,
                limit_price=Decimal("10"),
                requested_amount=Decimal("30"),
                reserved_cash=Decimal("30"),
                reserved_position_units=Decimal("0"),
                payload={
                    "private": "not selected",
                    "_age_policy": {"version": 1, "expired_at": "2026-10-20T12:00:00.000Z"},
                    "settlement_reason": "AGE_POLICY",
                    "broker_outcome_unknown": True,
                },
                created_at=NOW,
                updated_at=NOW,
            )
        )
        for event_id, sequence, kind in (
            ("accepted-order-fact", 3, "BROKER_ORDER_RECORDED"),
            ("accepted-execution-fact", 8, "TRADE_EXECUTION_RECORDED"),
        ):
            session.add(
                AutomationEventModel(
                    event_id=event_id,
                    automation_id="automation-1",
                    user_broker_id="scope-1",
                    sequence_number=sequence,
                    expected_revision=1,
                    fact_kind=kind,
                    safe_message="accepted",
                    payload={},
                    occurred_at=NOW,
                    received_at=NOW,
                )
            )

    statements: list[str] = []

    def capture_statement(_conn, _cursor, sql, _params, _ctx, _many):
        statements.append(sql)

    event.listen(engine, "before_cursor_execute", capture_statement)
    with TradingFactsUnitOfWork(factory) as uow:
        projection = uow.orders.execution_diagnostics("scope-1", "automation-1", limit=20)
        scope = uow.execution_diagnostics_scope("scope-1", "automation-1", limit=20)
    engine.dispose()

    assert {item["fact_id"] for item in projection["accepted_envelopes"]} == {
        "accepted-order-fact",
        "accepted-execution-fact",
    }
    assert {item["sequence_number"] for item in projection["accepted_envelopes"]} == {3, 8}
    assert {item["fact_id"] for item in projection["executions"]} == {"accepted-execution-fact"}
    assert {item["execution_id"] for item in projection["executions"]} == {"execution-1"}
    assert projection["executions"][0]["execution_id"] != projection["executions"][0]["fact_id"]
    assert scope is not None
    assert scope["reservations"][0]["reserved_cash"] == Decimal("30")
    assert scope["reservations"][0]["settlement_reason"] == "AGE_POLICY"
    assert scope["reservations"][0]["broker_outcome_unknown"] is True
    assert "private" not in scope["reservations"][0]
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    assert all("FOR UPDATE" not in sql.upper() for sql in statements)
