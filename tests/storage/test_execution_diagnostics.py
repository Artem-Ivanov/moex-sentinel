from decimal import Decimal

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from moex_sentinel.storage.models import Base
from moex_sentinel.storage.models.order_facts import BrokerOrderEventModel, BrokerOrderModel
from moex_sentinel.storage.models.trading_admission import TradingSubmitReservationModel
from moex_sentinel.storage.models.trading_observability import AutomationEventModel
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from tests.storage.trading_facts_helpers import NOW, execution_model, seed_order


def test_execution_diagnostics_three_exact_facts_at_limit_one_are_not_truncated(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'execution-complete.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        seed_order(session)
        session.scalar(select(BrokerOrderModel)).fact_id = "order-envelope"
        execution = execution_model("execution-1", external_execution_id="fill-1", source="BROKER_FILL")
        execution.fact_id = "execution-envelope"
        session.add(execution)
        session.add(
            BrokerOrderEventModel(
                id="state-1",
                fact_id="state-envelope",
                user_broker_id="scope-1",
                automation_id="automation-1",
                broker_order_id="order-1",
                from_state="CREATED",
                to_state="ACCEPTED",
                safe_reason="BROKER_ACCEPTED",
                safe_message="accepted",
                occurred_at=NOW,
                created_at=NOW,
            )
        )
        for fact_id, sequence, kind in (
            ("order-envelope", 1, "BROKER_ORDER_RECORDED"),
            ("state-envelope", 2, "BROKER_ORDER_STATE_CHANGED"),
            ("execution-envelope", 3, "TRADE_EXECUTION_RECORDED"),
        ):
            session.add(
                AutomationEventModel(
                    event_id=fact_id,
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

    with TradingFactsUnitOfWork(factory) as uow:
        result = uow.orders.execution_diagnostics("scope-1", "automation-1", limit=1)
    engine.dispose()

    assert len(result["orders"]) == len(result["events"]) == len(result["executions"]) == 1
    assert len(result["accepted_envelopes"]) == 3
    assert result["truncated"] is False


def test_execution_diagnostics_joins_each_fact_to_exact_accepted_envelope_read_only(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'execution-read.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        seed_order(session)
        session.scalar(select(BrokerOrderModel)).fact_id = "order-envelope"
        execution = execution_model("execution-1", external_execution_id="fill-1", source="BROKER_FILL")
        execution.fact_id = "execution-envelope"
        session.add(execution)
        second_execution = execution_model("execution-2", external_execution_id="fill-2", source="BROKER_FILL")
        second_execution.fact_id = "truncated-execution-envelope"
        session.add(second_execution)
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
                    "private": "SYNTHETIC-PRIVATE-CANARY",
                    "settlement_reason": "AGE_POLICY",
                    "broker_outcome_unknown": True,
                },
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add_all(
            [
                AutomationEventModel(
                    event_id="order-envelope",
                    automation_id="automation-1",
                    user_broker_id="scope-1",
                    sequence_number=4,
                    expected_revision=1,
                    fact_kind="BROKER_ORDER_RECORDED",
                    safe_message="order accepted",
                    payload={},
                    occurred_at=NOW,
                    received_at=NOW,
                ),
                AutomationEventModel(
                    event_id="execution-envelope",
                    automation_id="automation-1",
                    user_broker_id="scope-1",
                    sequence_number=9,
                    expected_revision=1,
                    fact_kind="TRADE_EXECUTION_RECORDED",
                    safe_message="execution accepted",
                    payload={},
                    occurred_at=NOW,
                    received_at=NOW,
                ),
                AutomationEventModel(
                    event_id="state-envelope",
                    automation_id="automation-1",
                    user_broker_id="scope-1",
                    sequence_number=6,
                    expected_revision=1,
                    fact_kind="BROKER_ORDER_STATE_CHANGED",
                    safe_message="state accepted",
                    payload={},
                    occurred_at=NOW,
                    received_at=NOW,
                ),
                AutomationEventModel(
                    event_id="unlinked-latest-envelope",
                    automation_id="automation-1",
                    user_broker_id="scope-1",
                    sequence_number=99,
                    expected_revision=1,
                    fact_kind="TRADE_EXECUTION_RECORDED",
                    safe_message="must not look like an acknowledgement",
                    payload={},
                    occurred_at=NOW,
                    received_at=NOW,
                ),
            ]
        )
        session.add(
            BrokerOrderEventModel(
                id="order-event-1",
                fact_id="state-envelope",
                user_broker_id="scope-1",
                automation_id="automation-1",
                broker_order_id="order-1",
                from_state="CREATED",
                to_state="ACCEPTED",
                safe_reason="BROKER_ACCEPTED",
                safe_message="accepted",
                occurred_at=NOW,
                created_at=NOW,
            )
        )

    statements = []

    def capture_statement(_conn, _cursor, sql, _params, _ctx, _many):
        statements.append(sql)

    event.listen(engine, "before_cursor_execute", capture_statement)
    with TradingFactsUnitOfWork(factory) as uow:
        result = uow.orders.execution_diagnostics("scope-1", "automation-1", limit=1)
        scope = uow.execution_diagnostics_scope("scope-1", "automation-1", limit=20)
    engine.dispose()

    assert {row["fact_id"] for row in result["accepted_envelopes"]} == {
        "order-envelope",
        "execution-envelope",
        "state-envelope",
    }
    assert {row["sequence_number"] for row in result["accepted_envelopes"]} == {4, 6, 9}
    assert {row["fact_id"] for row in result["orders"]} == {"order-envelope"}
    order = result["orders"][0]
    assert order["lot_size"] == 10
    assert order["decision_id"] == "decision-1"
    assert order["process_id"] == "process-1"
    assert order["estimated_commission"] == Decimal("0.01")
    assert {row["fact_id"] for row in result["executions"]} == {"execution-envelope"}
    assert result["executions"][0]["broker_commission"] == Decimal("0.01")
    assert result["executions"][0]["other_fees"] == Decimal("0")
    assert result["executions"][0]["execution_id"] == "execution-1"
    assert result["executions"][0]["fact_id"] == "execution-envelope"
    assert result["executions"][0]["execution_id"] != result["executions"][0]["fact_id"]
    assert "unlinked-latest-envelope" not in {row["fact_id"] for row in result["accepted_envelopes"]}
    assert len(result["events"]) == len(result["executions"]) == len(result["orders"]) == 1
    assert result["truncated"] is True
    assert scope is not None
    assert scope["reservations"] == (
        {
            "intent_id": "intent-1",
            "state": "UNKNOWN",
            "side": "BUY",
            "origin_runtime_instance_id": "runtime-1",
            "origin_generation": 2,
            "quantity_lots": 3,
            "reserved_cash": Decimal("30"),
            "reserved_position_units": Decimal("0"),
            "settlement_reason": "AGE_POLICY",
            "broker_outcome_unknown": True,
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    assert all("FOR UPDATE" not in sql.upper() for sql in statements)
    assert "SYNTHETIC-PRIVATE-CANARY" not in repr(scope)
    reservation_sql = [sql.lower() for sql in statements if "trading_submit_reservations" in sql.lower()]
    assert reservation_sql
    assert any("json_extract" in sql and "settlement_reason" in sql for sql in reservation_sql)
    assert any("json_extract" in sql and "broker_outcome_unknown" in sql for sql in reservation_sql)
    assert not any("select trading_submit_reservations.payload" in sql for sql in reservation_sql)
