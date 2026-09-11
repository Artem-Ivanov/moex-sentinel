from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from moex_sentinel.domain.trading_facts import TradingFactErrorCode, TradingFactPersistenceError
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base, TradeDecisionModel
from moex_sentinel.storage.repositories.trading_facts_support import append_idempotent, flush_or_translate
from tests.storage.test_trading_facts_models import decision_model, seed_cycle


@pytest.fixture
def database() -> Iterator[tuple[Engine, Session]]:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_cycle(session)
        yield engine, session
    engine.dispose()


def decision_identity(model: TradeDecisionModel) -> tuple[str, str]:
    return model.fact_id, model.reason_code


def test_append_idempotent_returns_existing_equal_value(database: tuple[Engine, Session]) -> None:
    _, session = database
    first_model = decision_model()
    second_model = decision_model()

    first = append_idempotent(
        session,
        candidate=first_model,
        identity=TradeDecisionModel.fact_id == first_model.fact_id,
        to_value=decision_identity,
        conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
        entity_type="trade_decision",
    )
    second = append_idempotent(
        session,
        candidate=second_model,
        identity=TradeDecisionModel.fact_id == second_model.fact_id,
        to_value=decision_identity,
        conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
        entity_type="trade_decision",
    )

    assert first == ("fact-decision-1", "SYNTHETIC_SIGNAL")
    assert second == first


def test_append_idempotent_rejects_conflicting_identity(database: tuple[Engine, Session]) -> None:
    _, session = database
    first_model = decision_model()
    append_idempotent(
        session,
        candidate=first_model,
        identity=TradeDecisionModel.fact_id == first_model.fact_id,
        to_value=decision_identity,
        conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
        entity_type="trade_decision",
    )
    conflict = decision_model("decision-2")
    conflict.reason_code = "DIFFERENT"

    with pytest.raises(TradingFactPersistenceError) as caught:
        append_idempotent(
            session,
            candidate=conflict,
            identity=TradeDecisionModel.fact_id == conflict.fact_id,
            to_value=decision_identity,
            conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
            entity_type="trade_decision",
        )

    assert caught.value.code is TradingFactErrorCode.FACT_ID_CONFLICT


def test_constraint_translation_never_exposes_payload(database: tuple[Engine, Session]) -> None:
    _, session = database
    first = decision_model()
    session.add(first)
    session.flush()
    conflict = decision_model("decision-2")
    conflict.strategy_snapshot = {"payload": "synthetic-payload"}
    session.add(conflict)

    with pytest.raises(TradingFactPersistenceError) as caught:
        flush_or_translate(session, entity_type="trade_decision")

    assert caught.value.code is TradingFactErrorCode.FACT_ID_CONFLICT
    assert caught.value.constraint_name == "uq_trade_decisions_fact_id"
    assert "synthetic-payload" not in str(caught.value)


def test_check_constraint_is_translated_to_invalid_state(database: tuple[Engine, Session]) -> None:
    _, session = database
    invalid = decision_model()
    invalid.current_price = -1
    session.add(invalid)

    with pytest.raises(TradingFactPersistenceError) as caught:
        flush_or_translate(session, entity_type="trade_decision")

    assert caught.value.code is TradingFactErrorCode.INVALID_STATE
    assert caught.value.constraint_name == "ck_trade_decisions_positive_current_price"


def test_sqlite_foreign_key_failure_is_translated_to_cross_scope(database: tuple[Engine, Session]) -> None:
    _, session = database
    invalid = decision_model()
    invalid.user_broker_id = "00000000-0000-4000-8000-000000000999"
    session.add(invalid)

    with pytest.raises(TradingFactPersistenceError) as caught:
        flush_or_translate(session, entity_type="trade_decision")

    assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE
    assert caught.value.constraint_name is None
