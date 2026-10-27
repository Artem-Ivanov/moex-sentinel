"""Behavior and bounded reads for scoped fact lineage on both dialects."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.trading_facts import (
    BrokerOrderDraft,
    TradeAuditEventDraft,
    TradeDecisionDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base, TradeDecisionModel
from moex_sentinel.storage.repositories.order_facts import OrderFactsRepository
from moex_sentinel.storage.repositories.trading_audit import TradingAuditRepository
from tests.domain.trading_facts_helpers import all_fact_drafts
from tests.storage.trading_facts_helpers import (
    automation_model,
    decision_model,
    execution_model,
    instrument_model,
    order_model,
    position_cycle_model,
    seed_buy_and_sell_executions,
)


@pytest.fixture
def lineage_engine() -> Iterator[Engine]:
    engine = create_database_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def lineage_factory(lineage_engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(lineage_engine, expire_on_commit=False)
    with factory.begin() as session:
        seed_buy_and_sell_executions(session)
        session.add(instrument_model("instrument-3", "scope-1"))
        session.flush()
        session.add(automation_model("automation-3", instrument_id="instrument-3"))
        session.add(automation_model("automation-2", user_broker_id="scope-2", instrument_id="instrument-2"))
        session.flush()
        cycle = position_cycle_model("cycle-2")
        cycle.user_broker_id, cycle.automation_id, cycle.instrument_id = "scope-2", "automation-2", "instrument-2"
        session.add(cycle)
        session.flush()
        decision = decision_model(
            "decision-2",
            user_broker_id="scope-2",
            automation_id="automation-2",
            instrument_id="instrument-2",
            fact_id="fact-decision-2",
        )
        decision.position_cycle_id = "cycle-2"
        session.add(decision)
        session.flush()
        order = order_model(
            "order-2",
            decision_id="decision-2",
            user_broker_id="scope-2",
            automation_id="automation-2",
            instrument_id="instrument-2",
        )
        order.position_cycle_id = "cycle-2"
        session.add(order)
        session.flush()
        execution = execution_model("execution-2", external_execution_id="external-2", source="BROKER_FILL")
        execution.user_broker_id, execution.automation_id, execution.instrument_id = (
            "scope-2",
            "automation-2",
            "instrument-2",
        )
        execution.position_cycle_id, execution.broker_order_id = "cycle-2", "order-2"
        session.add(execution)
    return factory


def fact_value(value_type):
    return next(value for value in all_fact_drafts() if isinstance(value, value_type))


@contextmanager
def capture_statements(engine: Engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.split(None, 1)[0].upper())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


class TestCoreFactLineageReads:
    @pytest.mark.parametrize(("cycle_id", "budget"), [("cycle-1", 4), (None, 3)])
    def test_decision_sql_budget(self, lineage_factory, lineage_engine, cycle_id, budget):
        value = fact_value(TradeDecisionDraft).model_copy(
            update={"id": "decision-new", "fact_id": "fact-new", "position_cycle_id": cycle_id}
        )
        with lineage_factory.begin() as session, capture_statements(lineage_engine) as statements:
            assert OrderFactsRepository(session).append_decision("scope-1", value) == value
        assert statements.count("SELECT") == budget
        assert statements.count("INSERT") == 1

    def test_audit_sql_budget(self, lineage_factory, lineage_engine):
        value = fact_value(TradeAuditEventDraft).model_copy(update={"execution_id": "execution-buy"})
        with lineage_factory.begin() as session, capture_statements(lineage_engine) as statements:
            assert TradingAuditRepository(session).append_audit("scope-1", value) == value
        assert statements.count("SELECT") == 6
        assert statements.count("INSERT") == 1

    @pytest.mark.parametrize(
        ("changes", "code"),
        [
            ({"automation_id": "absent", "instrument_id": "instrument-3"}, TradingFactErrorCode.CROSS_SCOPE),
            ({"automation_id": "automation-3", "position_cycle_id": "absent"}, TradingFactErrorCode.CROSS_SCOPE),
            ({"automation_id": "automation-3", "instrument_id": "absent"}, TradingFactErrorCode.CROSS_SCOPE),
            ({"automation_id": "automation-3", "instrument_id": "instrument-2"}, TradingFactErrorCode.CROSS_SCOPE),
            ({"instrument_id": "instrument-3"}, TradingFactErrorCode.INVALID_STATE),
            ({"automation_id": "automation-3", "instrument_id": "instrument-3"}, TradingFactErrorCode.INVALID_STATE),
        ],
    )
    def test_decision_scope_errors_precede_lineage(self, lineage_factory, changes, code):
        value = fact_value(TradeDecisionDraft).model_copy(update=changes)
        with lineage_factory() as session:
            with pytest.raises(TradingFactPersistenceError) as caught:
                OrderFactsRepository(session).append_decision("scope-1", value)
            assert caught.value.code is code
            assert caught.value.entity_type == "trade_decision"

    @pytest.mark.parametrize("reference", ["decision_id", "broker_order_id", "execution_id", "instrument_id"])
    def test_audit_late_missing_reference_precedes_automation_lineage(self, lineage_factory, reference):
        value = fact_value(TradeAuditEventDraft).model_copy(
            update={"automation_id": "automation-3", "execution_id": "execution-buy", reference: "absent"}
        )
        with lineage_factory() as session:
            with pytest.raises(TradingFactPersistenceError) as caught:
                TradingAuditRepository(session).append_audit("scope-1", value)
            assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE

    @pytest.mark.parametrize("reference", ["decision_id", "broker_order_id", "execution_id"])
    def test_audit_each_optional_reference_requires_matching_lineage(self, lineage_factory, reference):
        # Existing reference belongs to automation-1; all other refs are nullable.
        value = fact_value(TradeAuditEventDraft).model_copy(
            update={
                "automation_id": "automation-3",
                "instrument_id": "instrument-3",
                "decision_id": None,
                "broker_order_id": None,
                "execution_id": None,
                reference: {"decision_id": "decision-1", "broker_order_id": "order-1", "execution_id": "execution-buy"}[
                    reference
                ],
            }
        )
        with lineage_factory() as session:
            with pytest.raises(TradingFactPersistenceError) as caught:
                TradingAuditRepository(session).append_audit("scope-1", value)
            assert caught.value.code is TradingFactErrorCode.INVALID_STATE

    def test_audit_null_references_and_retry(self, lineage_factory):
        value = fact_value(TradeAuditEventDraft).model_copy(
            update={"decision_id": None, "broker_order_id": None, "execution_id": None}
        )
        with lineage_factory.begin() as session:
            repository = TradingAuditRepository(session)
            assert repository.append_audit("scope-1", value) == value
            assert repository.append_audit("scope-1", value) == value

    def test_pending_cycle_is_autoflushed_before_decision(self, lineage_factory):
        value = fact_value(TradeDecisionDraft).model_copy(
            update={
                "id": "decision-3",
                "fact_id": "fact-decision-3",
                "automation_id": "automation-3",
                "instrument_id": "instrument-3",
                "position_cycle_id": "cycle-3",
            }
        )
        with lineage_factory.begin() as session:
            cycle = position_cycle_model("cycle-3")
            cycle.automation_id = "automation-3"
            cycle.instrument_id = "instrument-3"
            session.add(cycle)
            assert OrderFactsRepository(session).append_decision("scope-1", value) == value

    def test_group_error_rolls_back_decision_and_corrected_replay_succeeds(self, lineage_factory):
        value = fact_value(TradeDecisionDraft).model_copy(update={"id": "decision-new", "fact_id": "fact-new"})
        order = fact_value(BrokerOrderDraft).model_copy(
            update={
                "id": "order-new",
                "fact_id": "fact-order-new",
                "decision_id": value.id,
                "instrument_id": "absent",
                "idempotency_key": "new-key",
            }
        )

        def write_rejected_group():
            with lineage_factory.begin() as session:
                repository = OrderFactsRepository(session)
                repository.append_decision("scope-1", value)
                repository.append_order("scope-1", order)

        with pytest.raises(TradingFactPersistenceError) as caught:
            write_rejected_group()
        assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE
        with lineage_factory() as session:
            assert (
                session.scalar(
                    select(func.count()).select_from(TradeDecisionModel).where(TradeDecisionModel.id == value.id)
                )
                == 0
            )
        with lineage_factory.begin() as session:
            repository = OrderFactsRepository(session)
            assert repository.append_decision("scope-1", value) == value
            corrected = order.model_copy(update={"instrument_id": "instrument-1"})
            assert repository.append_order("scope-1", corrected) == corrected

    @pytest.mark.parametrize(
        ("field", "foreign_id"),
        [("automation_id", "automation-2"), ("position_cycle_id", "cycle-2"), ("instrument_id", "instrument-2")],
    )
    def test_decision_foreign_scoped_references_are_rejected(self, lineage_factory, field, foreign_id):
        value = fact_value(TradeDecisionDraft).model_copy(update={field: foreign_id})
        with lineage_factory() as session:
            with pytest.raises(TradingFactPersistenceError) as caught:
                OrderFactsRepository(session).append_decision("scope-1", value)
            assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE

    @pytest.mark.parametrize(
        ("field", "foreign_id"),
        [
            ("automation_id", "automation-2"),
            ("decision_id", "decision-2"),
            ("broker_order_id", "order-2"),
            ("execution_id", "execution-2"),
            ("instrument_id", "instrument-2"),
        ],
    )
    def test_audit_foreign_scoped_references_are_rejected(self, lineage_factory, field, foreign_id):
        value = fact_value(TradeAuditEventDraft).model_copy(update={"execution_id": "execution-buy", field: foreign_id})
        with lineage_factory() as session:
            with pytest.raises(TradingFactPersistenceError) as caught:
                TradingAuditRepository(session).append_audit("scope-1", value)
            assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE

    @pytest.mark.parametrize("late_reference", ["broker_order_id", "execution_id", "instrument_id"])
    def test_audit_scoped_error_precedes_wrong_optional_reference_lineage(self, lineage_factory, late_reference):
        value = fact_value(TradeAuditEventDraft).model_copy(
            update={
                "automation_id": "automation-3",
                "instrument_id": "instrument-3",
                "execution_id": "execution-buy",
                late_reference: "absent",
            }
        )
        with lineage_factory() as session:
            with pytest.raises(TradingFactPersistenceError) as caught:
                TradingAuditRepository(session).append_audit("scope-1", value)
            assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE

    def test_pending_execution_is_autoflushed_before_audit(self, lineage_factory):
        value = fact_value(TradeAuditEventDraft).model_copy(update={"execution_id": "execution-pending"})
        with lineage_factory.begin() as session:
            session.add(
                execution_model("execution-pending", external_execution_id="external-pending", source="BROKER_FILL")
            )
            assert TradingAuditRepository(session).append_audit("scope-1", value) == value

    def test_audit_failure_rolls_back_pending_fact_group_and_accepts_replay(self, lineage_factory):
        audit = fact_value(TradeAuditEventDraft).model_copy(
            update={"execution_id": "execution-pending", "instrument_id": "absent"}
        )

        def write_rejected_group():
            with lineage_factory.begin() as session:
                session.add(
                    execution_model("execution-pending", external_execution_id="external-pending", source="BROKER_FILL")
                )
                TradingAuditRepository(session).append_audit("scope-1", audit)

        with pytest.raises(TradingFactPersistenceError) as caught:
            write_rejected_group()
        assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE
        with lineage_factory.begin() as session:
            session.add(
                execution_model("execution-pending", external_execution_id="external-pending", source="BROKER_FILL")
            )
            corrected = audit.model_copy(update={"instrument_id": "instrument-1"})
            repository = TradingAuditRepository(session)
            assert repository.append_audit("scope-1", corrected) == corrected
            assert repository.append_audit("scope-1", corrected) == corrected
