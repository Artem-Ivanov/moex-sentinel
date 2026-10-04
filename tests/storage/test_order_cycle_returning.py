"""Aggregate replacements preserve facts and return fresh values without rereads."""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.trading_facts import (
    BrokerOrderDraft,
    BrokerOrderStatus,
    TradeDecisionDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base, BrokerOrderModel, PositionCycleModel
from moex_sentinel.storage.repositories.order_facts import OrderFactsRepository
from moex_sentinel.storage.repositories.position_ledger import PositionLedgerRepository
from sentinel_contracts.time import floor_utc_millisecond
from tests.storage.test_core_fact_lineage_reads import capture_statements, fact_value
from tests.storage.trading_facts_helpers import seed_order


@pytest.fixture
def replacement_engine() -> Iterator[Engine]:
    engine = create_database_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def replacement_factory(replacement_engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(replacement_engine, expire_on_commit=False)
    with factory.begin() as session:
        seed_order(session)
    return factory


def repository_for(session: Session, kind: str):
    return OrderFactsRepository(session) if kind == "order" else PositionLedgerRepository(session)


def read(repository, kind: str):
    return repository.get_order("scope-1", "order-1") if kind == "order" else repository.get_cycle("scope-1", "cycle-1")


def replace(repository, kind: str, value, scope: str = "scope-1"):
    return (
        repository.replace_order_aggregate(scope, value)
        if kind == "order"
        else repository.replace_cycle_aggregate(scope, value)
    )


def changed(value, kind: str, iteration: int = 1):
    values = value.model_dump()
    values["updated_at"] += timedelta(seconds=iteration)
    if kind == "order":
        values.update(state=BrokerOrderStatus.SUBMITTED, requested_amount=value.requested_amount + Decimal(iteration))
    else:
        values.update(unrealized_pnl=Decimal(iteration), net_pnl=Decimal(iteration))
    return type(value).model_validate(values)


class TestOrderCycleReturning:
    @pytest.mark.parametrize(("kind", "expected"), [("order", ["SELECT", "UPDATE"] * 2), ("cycle", ["UPDATE"] * 2)])
    def test_two_replacements_have_no_post_update_select(self, replacement_factory, replacement_engine, kind, expected):
        with replacement_factory.begin() as session:
            repository = repository_for(session, kind)
            initial = read(repository, kind)
            first = changed(initial, kind)
            second = changed(first, kind, 2)
            with capture_statements(replacement_engine) as statements:
                assert replace(repository, kind, first) == first
                assert replace(repository, kind, second) == second
            assert statements == expected
        with replacement_factory() as session:
            assert read(repository_for(session, kind), kind) == second

    @pytest.mark.parametrize("kind", ["order", "cycle"])
    def test_preloaded_model_and_returned_dto_follow_two_replacements(self, replacement_factory, kind):
        with replacement_factory.begin() as session:
            model_type = BrokerOrderModel if kind == "order" else PositionCycleModel
            model = session.get(model_type, "order-1" if kind == "order" else "cycle-1")
            repository = repository_for(session, kind)
            original = read(repository, kind)
            field = "requested_amount" if kind == "order" else "unrealized_pnl"
            # A Core SQL write intentionally leaves the preloaded ORM model stale.
            session.connection().execute(
                update(model_type.__table__)
                .where(model_type.id == model.id)
                .values(**{field: Decimal("99"), "updated_at": original.updated_at})
            )
            assert getattr(model, field) != Decimal("99")
            for iteration in (1, 2):
                candidate = changed(original, kind, iteration)
                assert replace(repository, kind, candidate) == candidate
                field = "requested_amount" if kind == "order" else "unrealized_pnl"
                assert getattr(model, field) == getattr(candidate, field)
                assert model.updated_at == candidate.updated_at
                assert read(repository, kind) == candidate
        with replacement_factory() as session:
            assert read(repository_for(session, kind), kind) == candidate

    @pytest.mark.parametrize("kind", ["order", "cycle"])
    def test_utc_milliseconds_and_decimal_fields_match_persisted_value(self, replacement_factory, kind):
        observed = datetime(2026, 8, 13, 15, 12, 4, 123456, tzinfo=timezone(timedelta(hours=3)))
        with replacement_factory.begin() as session:
            repository = repository_for(session, kind)
            values = changed(read(repository, kind), kind).model_dump()
            values["updated_at"] = observed
            values["executed_at" if kind == "order" else "closed_at"] = observed
            if kind == "cycle":
                values["state"] = "CLOSED"
            candidate = type(read(repository, kind)).model_validate(values)
            normalized = floor_utc_millisecond(observed)
            expected = candidate.model_copy(
                update={"updated_at": normalized, "executed_at" if kind == "order" else "closed_at": normalized}
            )
            returned = replace(repository, kind, candidate)
            assert returned == expected
            assert returned.updated_at.utcoffset() == timedelta(0)
            assert returned.updated_at.microsecond == 123000
        with replacement_factory() as session:
            assert read(repository_for(session, kind), kind) == returned

    @pytest.mark.parametrize("kind", ["order", "cycle"])
    def test_missing_or_foreign_aggregate_is_not_found(self, replacement_factory, kind):
        with replacement_factory() as session:
            repository = repository_for(session, kind)
            original = read(repository, kind)
            for candidate, scope in (
                (original.model_copy(update={"id": "absent"}), "scope-1"),
                (original.model_copy(update={"user_broker_id": "scope-2"}), "scope-2"),
            ):
                with pytest.raises(TradingFactPersistenceError) as caught:
                    replace(repository, kind, candidate, scope)
                assert caught.value.code is TradingFactErrorCode.NOT_FOUND
            with pytest.raises(TradingFactPersistenceError) as caught:
                replace(repository, kind, original, "scope-2")
            assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE
            assert read(repository, kind) == original

    @pytest.mark.parametrize("kind", ["order", "cycle"])
    def test_group_rollback_restores_original_aggregate(self, replacement_factory, kind):
        with replacement_factory() as session:
            original = read(repository_for(session, kind), kind)

        def write_rejected_group():
            with replacement_factory.begin() as session:
                repository = repository_for(session, kind)
                first = replace(repository, kind, changed(original, kind))
                replace(repository, kind, changed(first, kind, 2))
                raise RuntimeError("rollback group")

        with pytest.raises(RuntimeError, match="rollback group"):
            write_rejected_group()
        with replacement_factory() as session:
            assert read(repository_for(session, kind), kind) == original
            assert replace(repository_for(session, kind), kind, changed(original, kind)) == changed(original, kind)

    @pytest.mark.parametrize(
        ("field", "value"),
        [("quantity_lots", 2), ("automation_id", "other"), ("strategy_snapshot", {"different": True})],
    )
    def test_order_immutable_intent_rejected_without_mutation(self, replacement_factory, field, value):
        with replacement_factory() as session:
            repository = OrderFactsRepository(session)
            original = repository.get_order("scope-1", "order-1")
            with pytest.raises(TradingFactPersistenceError) as caught:
                repository.replace_order_aggregate("scope-1", original.model_copy(update={field: value}))
            assert caught.value.code is TradingFactErrorCode.INVALID_STATE
            assert repository.get_order("scope-1", "order-1") == original

    def test_cycle_stale_replay_and_wrong_lineage_preserve_newer_value(self, replacement_factory):
        with replacement_factory.begin() as session:
            repository = PositionLedgerRepository(session)
            original = repository.get_cycle("scope-1", "cycle-1")
            newer = changed(original, "cycle")
            assert repository.replace_cycle_aggregate("scope-1", newer) == newer
            assert repository.replace_cycle_aggregate("scope-1", original) == newer
            with pytest.raises(TradingFactPersistenceError) as caught:
                repository.replace_cycle_aggregate(
                    "scope-1", newer.model_copy(update={"instrument_id": "instrument-2"})
                )
            assert caught.value.code is TradingFactErrorCode.NOT_FOUND
            assert repository.get_cycle("scope-1", "cycle-1") == newer

    @pytest.mark.parametrize("missing", [None, "", "   "])
    def test_order_keeps_known_external_identity(self, replacement_factory, missing):
        with replacement_factory.begin() as session:
            repository = OrderFactsRepository(session)
            original = repository.get_order("scope-1", "order-1")
            known = original.model_copy(update={"external_order_id": "external-1"})
            assert repository.replace_order_aggregate("scope-1", known) == known
            candidate = changed(known, "order").model_copy(update={"external_order_id": missing})
            returned = repository.replace_order_aggregate("scope-1", candidate)
            assert returned.external_order_id == "external-1"
            with pytest.raises(TradingFactPersistenceError) as caught:
                repository.replace_order_aggregate(
                    "scope-1", candidate.model_copy(update={"external_order_id": "external-2"})
                )
            assert caught.value.code is TradingFactErrorCode.INVALID_STATE
            assert repository.get_order("scope-1", "order-1") == returned

    def test_duplicate_external_id_is_typed_and_group_rolls_back(self, replacement_factory):
        with replacement_factory.begin() as session:
            repository = OrderFactsRepository(session)
            original = repository.get_order("scope-1", "order-1")
            repository.replace_order_aggregate(
                "scope-1", original.model_copy(update={"external_order_id": "external-1"})
            )
            decision = fact_value(TradeDecisionDraft).model_copy(
                update={"id": "decision-2", "fact_id": "fact-decision-2"}
            )
            repository.append_decision("scope-1", decision)
            second = fact_value(BrokerOrderDraft).model_copy(
                update={
                    "id": "order-2",
                    "fact_id": "fact-order-2",
                    "decision_id": "decision-2",
                    "idempotency_key": "key-2",
                }
            )
            repository.append_order("scope-1", second)

        def write_rejected_group():
            with replacement_factory.begin() as session:
                repository = OrderFactsRepository(session)
                repository.replace_order_aggregate(
                    "scope-1", changed(repository.get_order("scope-1", "order-1"), "order")
                )
                repository.replace_order_aggregate(
                    "scope-1", second.model_copy(update={"external_order_id": "external-1"})
                )

        with pytest.raises(TradingFactPersistenceError) as caught:
            write_rejected_group()
        assert caught.value.code is TradingFactErrorCode.INVALID_STATE
        with replacement_factory() as session:
            repository = OrderFactsRepository(session)
            assert repository.get_order("scope-1", "order-1").requested_amount == original.requested_amount
            assert repository.get_order("scope-1", "order-2") == second

    def test_stale_cycle_fallback_refreshes_preloaded_identity_and_preserves_newer_database(
        self, replacement_factory, replacement_engine
    ):
        with replacement_factory.begin() as session:
            repository = PositionLedgerRepository(session)
            loaded = session.get(PositionCycleModel, "cycle-1")
            original = repository.get_cycle("scope-1", "cycle-1")
            newer = changed(original, "cycle", 5)
            session.connection().execute(
                update(PositionCycleModel.__table__)
                .where(PositionCycleModel.id == "cycle-1")
                .values(unrealized_pnl=newer.unrealized_pnl, net_pnl=newer.net_pnl, updated_at=newer.updated_at)
            )
            assert loaded.updated_at == original.updated_at
            with capture_statements(replacement_engine) as statements:
                returned = repository.replace_cycle_aggregate("scope-1", original)
            assert statements == ["UPDATE", "SELECT"]
            assert returned == newer
            assert loaded.updated_at == newer.updated_at
            assert loaded.unrealized_pnl == newer.unrealized_pnl
        with replacement_factory() as session:
            assert PositionLedgerRepository(session).get_cycle("scope-1", "cycle-1") == newer

    def test_order_and_cycle_replacements_share_rollback_boundary(self, replacement_factory):
        with replacement_factory() as session:
            order = OrderFactsRepository(session).get_order("scope-1", "order-1")
            cycle = PositionLedgerRepository(session).get_cycle("scope-1", "cycle-1")

        def write_rejected_group():
            with replacement_factory.begin() as session:
                assert OrderFactsRepository(session).replace_order_aggregate(
                    "scope-1", changed(order, "order")
                ) == changed(order, "order")
                assert PositionLedgerRepository(session).replace_cycle_aggregate(
                    "scope-1", changed(cycle, "cycle")
                ) == changed(cycle, "cycle")
                raise RuntimeError("atomic group")

        with pytest.raises(RuntimeError, match="atomic group"):
            write_rejected_group()
        with replacement_factory() as session:
            assert OrderFactsRepository(session).get_order("scope-1", "order-1") == order
            assert PositionLedgerRepository(session).get_cycle("scope-1", "cycle-1") == cycle
