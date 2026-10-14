"""The admission migration preserves existing Core data and refuses history loss."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import URL

from alembic import command
from alembic.config import Config
from moex_sentinel.domain.trading_facts import (
    AutomationEnvelopeDraft,
    BrokerOrderDraft,
    BrokerOrderEventDraft,
    ExecutionLotAllocationDraft,
    OrderIntentKind,
    PositionCycleDraft,
    PositionLotDraft,
    TradeAuditEventDraft,
    TradeDecisionDraft,
    TradeExecutionDraft,
    TradingAutomationDraft,
)
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.models.trading_admission import TradingAdmissionEventModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from moex_sentinel.storage.schema_revision import expected_schema_revision
from sentinel_contracts.broker_execution import OrderSide
from tests.domain.trading_facts_helpers import all_fact_drafts
from tests.integration.postgresql.test_paired_restore import fingerprint, journal
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model


def _config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.render_as_string(hide_password=False).replace("%", "%%"))
    return config


@pytest.mark.postgresql
def test_upgrade_preserves_seeded_core_graph_and_nonempty_downgrade_is_rejected(
    isolated_postgresql_database_url: URL,
) -> None:
    config = _config(isolated_postgresql_database_url)
    command.downgrade(config, "0003_user_broker_archive")

    broker_id = str(uuid4())
    instrument_id = "instrument-1"
    automation_id = "automation-1"
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            broker = user_broker_model(broker_id, "migration-preserved-account")
            session.add(broker)
            session.flush()
            instrument = instrument_model(instrument_id, broker_id)
            session.add(instrument)
            session.flush()
        drafts = {type(value): value for value in all_fact_drafts()}
        automation = drafts[TradingAutomationDraft].model_copy(
            update={"user_broker_id": broker_id, "instrument_id": instrument_id}
        )
        # The row belongs to the historical 0003 schema. Seed it through SQL so
        # the current ORM does not reference columns introduced by later revisions.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO trading_automations "
                    "(id, user_broker_id, instrument_id, state, suspended_from_state, hold_reason, "
                    "revision, last_sequence_number, resume_requested, closed_at, created_at, updated_at) "
                    "VALUES (:id, :user_broker_id, :instrument_id, :state, :suspended_from_state, "
                    ":hold_reason, :revision, :last_sequence_number, :resume_requested, :closed_at, "
                    ":created_at, :updated_at)"
                ),
                {
                    "id": automation.id,
                    "user_broker_id": automation.user_broker_id,
                    "instrument_id": automation.instrument_id,
                    "state": automation.state.value,
                    "suspended_from_state": None,
                    "hold_reason": automation.hold_reason,
                    "revision": automation.revision,
                    "last_sequence_number": automation.last_sequence_number,
                    "resume_requested": automation.resume_requested,
                    "closed_at": automation.closed_at,
                    "created_at": automation.created_at,
                    "updated_at": automation.updated_at,
                },
            )
        with TradingFactsUnitOfWork(factory) as uow:
            cycle = drafts[PositionCycleDraft].model_copy(update={"user_broker_id": broker_id})
            decision = drafts[TradeDecisionDraft].model_copy(update={"user_broker_id": broker_id})
            sell_decision = decision.model_copy(
                update={"id": "decision-sell-1", "fact_id": "fact-decision-sell-1", "process_id": "process-sell-1"}
            )
            order = drafts[BrokerOrderDraft].model_copy(update={"user_broker_id": broker_id})
            event = drafts[BrokerOrderEventDraft].model_copy(update={"user_broker_id": broker_id})
            execution = drafts[TradeExecutionDraft].model_copy(update={"user_broker_id": broker_id})
            lot = drafts[PositionLotDraft].model_copy(update={"user_broker_id": broker_id})
            allocation = drafts[ExecutionLotAllocationDraft].model_copy(update={"user_broker_id": broker_id})
            audit = drafts[TradeAuditEventDraft].model_copy(update={"user_broker_id": broker_id})
            envelope = drafts[AutomationEnvelopeDraft].model_copy(update={"user_broker_id": broker_id})
            sell_order = order.model_copy(
                update={
                    "id": "order-sell-1",
                    "fact_id": "fact-order-sell-1",
                    "decision_id": sell_decision.id,
                    "idempotency_key": "idempotency-sell-1",
                    "intent_kind": OrderIntentKind.SELL_PART,
                    "side": OrderSide.SELL,
                    "limit_price": Decimal("11"),
                    "requested_amount": Decimal("11"),
                }
            )
            sell_execution = execution.model_copy(
                update={
                    "id": "execution-sell-1",
                    "fact_id": "fact-execution-sell-1",
                    "broker_order_id": sell_order.id,
                    "external_execution_id": "external-execution-sell-1",
                    "side": OrderSide.SELL,
                    "price": Decimal("11"),
                    "value": Decimal("11"),
                }
            )
            allocation = allocation.model_copy(update={"sell_execution_id": sell_execution.id})
            uow.positions.open_cycle(broker_id, cycle)
            uow.orders.append_decision(broker_id, decision)
            uow.orders.append_decision(broker_id, sell_decision)
            uow.orders.append_order(broker_id, order)
            uow.orders.append_order(broker_id, sell_order)
            uow.orders.append_order_event(broker_id, event)
            uow.orders.append_execution(broker_id, execution)
            uow.orders.append_execution(broker_id, sell_execution)
            uow.positions.append_lot(broker_id, lot)
            uow.positions.append_allocation(broker_id, allocation)
            uow.audit.append_audit(broker_id, audit)
            uow.audit.append_envelope(broker_id, envelope)

        journal(engine)
        before = fingerprint(engine)

        command.upgrade(config, "0004_trading_admission")
        after = fingerprint(engine)
        assert {name: before[name] for name in before if name != "alembic_version"} == {
            name: after[name] for name in before if name != "alembic_version"
        }

        with factory() as session:
            assert session.get(UserBrokerModel, broker_id) is not None
            assert session.get(BrokerInstrumentModel, instrument_id) is not None
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT id FROM trading_automations WHERE id = :automation_id"),
                    {"automation_id": automation_id},
                )
                == automation_id
            )
        with factory.begin() as session:
            session.add(
                TradingAdmissionEventModel(
                    event_id=str(uuid4()),
                    user_broker_id=broker_id,
                    runtime_instance_id=None,
                    generation=None,
                    event_type="MIGRATION_GUARD_PROBE",
                    actor="test",
                    occurred_at=datetime(2026, 10, 9, tzinfo=UTC),
                    payload={"synthetic": True},
                )
            )

        with pytest.raises(RuntimeError, match="Cannot remove trading admission history"):
            command.downgrade(config, "0003_user_broker_archive")

        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004_trading_admission"
        with factory() as session:
            event = session.scalar(
                select(TradingAdmissionEventModel).where(
                    TradingAdmissionEventModel.user_broker_id == broker_id,
                    TradingAdmissionEventModel.event_type == "MIGRATION_GUARD_PROBE",
                )
            )
            assert event is not None

        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == expected_schema_revision()
        with factory() as session:
            migrated_automation = session.get(TradingAutomationModel, automation_id)
            assert migrated_automation is not None
            assert migrated_automation.order_quota_percent is None
            assert migrated_automation.settings_revision == 0
    finally:
        engine.dispose()
