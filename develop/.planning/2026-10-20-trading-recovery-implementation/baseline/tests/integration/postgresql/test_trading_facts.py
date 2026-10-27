"""PostgreSQL baseline trading fact integration tests."""

from decimal import Decimal

import pytest
from sqlalchemy.engine import URL

from moex_sentinel.domain.trading_facts import (
    BrokerOrderDraft,
    PositionCycleDraft,
    TradeDecisionDraft,
    TradingAutomationDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from tests.domain.trading_facts_helpers import all_fact_drafts
from tests.services.test_trading_fact_ingress import (
    AUTOMATION_A,
    AUTOMATION_B,
    INSTRUMENT_A,
    INSTRUMENT_B,
    SCOPE_ID,
    assert_cancelled_orders_without_broker_ids_replay,
    assert_cycle_instrument_lineage_rejects_group_atomically,
)
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model


def fact_value(value_type):
    return next(value for value in all_fact_drafts() if isinstance(value, value_type))


@pytest.mark.postgresql
def test_postgresql_cycle_instrument_mismatch_rolls_back_group_and_allows_corrected_replay(
    isolated_postgresql_database_url: URL,
) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            session.add(user_broker_model(str(SCOPE_ID), "synthetic-account"))
            session.flush()
            for instrument_id, ticker in ((INSTRUMENT_A, "SYNTH_A"), (INSTRUMENT_B, "SYNTH_B")):
                instrument = instrument_model(str(instrument_id), str(SCOPE_ID))
                instrument.ticker = ticker
                session.add(instrument)
            session.flush()
            session.add(
                automation_model(str(AUTOMATION_A), user_broker_id=str(SCOPE_ID), instrument_id=str(INSTRUMENT_A))
            )
        assert_cycle_instrument_lineage_rejects_group_atomically(factory)
    finally:
        engine.dispose()


def persist_then_fail(factory, automation) -> None:
    with TradingFactsUnitOfWork(factory) as uow:
        uow.automations.create("scope-1", automation)
        raise RuntimeError("synthetic rollback")


@pytest.mark.postgresql
def test_postgresql_persists_fact_graph_and_rejects_cross_scope_lineage(
    isolated_postgresql_database_url: URL,
) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add_all(
            [
                user_broker_model("scope-1", "account-1"),
                user_broker_model("scope-2", "account-2"),
            ]
        )
        session.flush()
        session.add_all(
            [
                instrument_model("instrument-1", "scope-1"),
                instrument_model("instrument-2", "scope-2"),
            ]
        )
    automation = fact_value(TradingAutomationDraft)
    cycle = fact_value(PositionCycleDraft)
    decision = fact_value(TradeDecisionDraft)
    with TradingFactsUnitOfWork(factory) as uow:
        uow.automations.create("scope-1", automation)
        uow.positions.open_cycle("scope-1", cycle)
        saved = uow.orders.append_decision("scope-1", decision)

    assert saved.current_price == Decimal("10.25")
    assert saved.indicators == {"signal": "synthetic"}
    assert saved.decided_at.utcoffset() is not None

    cross_scope = fact_value(BrokerOrderDraft).model_copy(
        update={"user_broker_id": "scope-2", "instrument_id": "instrument-2"}
    )
    with pytest.raises(TradingFactPersistenceError) as caught, TradingFactsUnitOfWork(factory) as uow:
        uow.orders.append_order("scope-2", cross_scope)

    assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE
    engine.dispose()


@pytest.mark.postgresql
def test_postgresql_unit_of_work_rolls_back_and_active_index_is_partial(
    isolated_postgresql_database_url: URL,
) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(user_broker_model("scope-1", "account-1"))
        session.flush()
        session.add(instrument_model("instrument-1", "scope-1"))
    automation = fact_value(TradingAutomationDraft)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        persist_then_fail(factory, automation)

    with TradingFactsUnitOfWork(factory) as uow:
        uow.automations.create("scope-1", automation)

    second_automation = automation.model_copy(update={"id": "automation-2"})
    with pytest.raises(TradingFactPersistenceError) as caught, TradingFactsUnitOfWork(factory) as uow:
        uow.automations.create("scope-1", second_automation)

    assert caught.value.code is TradingFactErrorCode.INVALID_STATE
    engine.dispose()


@pytest.mark.postgresql
def test_postgresql_cancelled_orders_without_broker_ids_replay(isolated_postgresql_database_url: URL) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            session.add(user_broker_model(str(SCOPE_ID), "synthetic-account"))
            session.flush()
            for instrument_id, ticker in ((INSTRUMENT_A, "SYNTH_A"), (INSTRUMENT_B, "SYNTH_B")):
                instrument = instrument_model(str(instrument_id), str(SCOPE_ID))
                instrument.ticker = ticker
                session.add(instrument)
            session.flush()
            for automation_id, instrument_id in ((AUTOMATION_A, INSTRUMENT_A), (AUTOMATION_B, INSTRUMENT_B)):
                session.add(
                    automation_model(str(automation_id), user_broker_id=str(SCOPE_ID), instrument_id=str(instrument_id))
                )
        assert_cancelled_orders_without_broker_ids_replay(factory)
    finally:
        engine.dispose()
