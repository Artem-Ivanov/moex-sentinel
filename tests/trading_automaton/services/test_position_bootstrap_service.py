"""Atomic Worker bootstrap from an immutable broker-position command."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from sentinel_contracts.automation_lifecycle import InvalidAutomationTransition
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import BrokerPositionBootstrap, FactKind, PositionLotSource
from tests.trading_automaton.command_factory import command
from tests.trading_automaton.storage.test_fact_outbox import audit_payload
from trading_automaton.config import StrategySettings
from trading_automaton.services.position_bootstrap import PositionBootstrapService
from trading_automaton.services.strategies import AdaptiveScalpingStrategy
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import (
    Base,
    BusinessAuditEventModel,
    CachedAutomationModel,
    FactOutboxModel,
    LocalIntentModel,
    TradeDecisionModel,
    TradeLotModel,
)
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 8, 14, 10, 0, 0, 123000, tzinfo=UTC)
CYCLE_ID = UUID("00000000-0000-4000-8000-000000000501")
LOT_ID = UUID("00000000-0000-4000-8000-000000000502")


def bootstrap_command(*, invested_amount: Decimal = Decimal("2000")):
    return command(
        state=AutomationState.HOLD,
        bootstrap=BrokerPositionBootstrap(
            position_cycle_id=CYCLE_ID,
            position_lot_id=LOT_ID,
            quantity_lots=2,
            average_price=Decimal("100"),
            invested_amount=invested_amount,
            currency="RUB",
            observed_at=NOW,
        ),
    )


def repository(*, writer: FactOutboxWriter | None = None) -> tuple[LocalAutomationRepository, sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    return LocalAutomationRepository(factory, fact_writer=writer or FactOutboxWriter(clock=lambda: NOW)), factory


@pytest.mark.parametrize("state", [AutomationState.HOLD, AutomationState.IN_QUEUE])
def test_bootstrap_atomically_creates_reconciled_lot_and_contiguous_fact_group(state: AutomationState) -> None:
    repo, factory = repository()
    value = bootstrap_command().model_copy(update={"state": state})
    repo.cache_command(value)

    result = PositionBootstrapService(repo).ensure(value, StrategySettings())

    assert result.applied is True
    assert result.emitted_facts == 4
    lots = repo.list_open_lots(str(value.automation_id))
    assert len(lots) == 1
    assert lots[0].id == str(LOT_ID)
    assert lots[0].source == PositionLotSource.BROKER_POSITION_BOOTSTRAP.value
    assert lots[0].source_intent_id is None
    assert lots[0].original_lots == lots[0].remaining_lots == 2
    assert lots[0].entry_price == Decimal("100")
    assert lots[0].entry_commission == 0
    facts = repo.ready_fact_outbox(10, now=NOW, deadline_ms=0)
    assert [item.fact_kind for item in facts] == [
        FactKind.POSITION_CYCLE_UPDATED.value,
        FactKind.POSITION_LOT_OPENED.value,
        FactKind.TRADE_AUDIT_RECORDED.value,
        FactKind.AUTOMATION_STATE_CHANGED.value,
    ]
    assert [item.sequence_number for item in facts] == [1, 2, 3, 4]
    assert facts[-1].payload["state"] == AutomationState.IN_WORK.value
    strategy = AdaptiveScalpingStrategy()
    audit_data = facts[2].payload["data"]
    assert (audit_data["strategy_code"], audit_data["strategy_version"]) == (strategy.code, strategy.version)
    assert repo.get_state(str(value.automation_id)).state == AutomationState.IN_WORK.value
    with factory() as session:
        local_audit = session.scalar(select(BusinessAuditEventModel))
        assert local_audit is not None
        assert local_audit.event_data == audit_data
        assert session.scalar(select(func.count()).select_from(LocalIntentModel)) == 0
        assert session.scalar(select(func.count()).select_from(TradeDecisionModel)) == 0


def test_exact_retry_keeps_one_lot_and_same_outbox_event_ids() -> None:
    repo, factory = repository()
    value = bootstrap_command()
    repo.cache_command(value)

    first = PositionBootstrapService(repo).ensure(value, StrategySettings())
    event_ids = tuple(row.event_id for row in repo.ready_fact_outbox(10, now=NOW, deadline_ms=0))
    second = PositionBootstrapService(repo).ensure(value, StrategySettings())

    assert first.applied is True
    assert second.applied is False
    assert tuple(row.event_id for row in repo.ready_fact_outbox(10, now=NOW, deadline_ms=0)) == event_ids
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(TradeLotModel)) == 1
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 4


@pytest.mark.parametrize("reconciled", [False, True])
def test_failure_before_commit_leaves_no_partial_bootstrap_state(reconciled: bool) -> None:
    class FailingWriter(FactOutboxWriter):
        def __init__(self) -> None:
            super().__init__(clock=lambda: NOW)
            self.calls = 0

        def append(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("synthetic failure")
            return super().append(*args, **kwargs)

    repo, factory = repository(writer=FailingWriter())
    value = bootstrap_command()
    repo.cache_command(value)
    original = None
    if reconciled:
        original = repo.create_trade_lot(
            automation_id=str(value.automation_id),
            source_intent_id=None,
            source="RECONCILED",
            quantity_lots=2,
            entry_price=Decimal("100"),
            entry_commission=Decimal(),
            opened_at=NOW,
        )

    with pytest.raises(RuntimeError, match="synthetic failure"):
        PositionBootstrapService(repo).ensure(value, StrategySettings())

    assert repo.get_state(str(value.automation_id)).state == AutomationState.HOLD.value
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(TradeLotModel)) == int(reconciled)
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0
    if original is not None:
        assert repo.list_open_lots(str(value.automation_id)) == [original]


@pytest.mark.parametrize(
    "cached_state",
    [
        AutomationState.IN_QUEUE,
        AutomationState.OPENING,
        AutomationState.IN_WORK,
        AutomationState.CLOSED,
    ],
)
def test_bootstrap_requires_hold_cached_state_before_writing_atomic_group(
    cached_state: AutomationState,
) -> None:
    repo, factory = repository()
    value = bootstrap_command()
    repo.cache_command(value)
    with factory.begin() as session:
        session.get_one(CachedAutomationModel, str(value.automation_id)).state = cached_state.value

    with pytest.raises(InvalidAutomationTransition):
        PositionBootstrapService(repo).ensure(value, StrategySettings())

    with factory() as session:
        cached = session.get_one(CachedAutomationModel, str(value.automation_id))
        assert cached.state == cached_state.value
        assert cached.position_cycle_id is None
        assert session.scalar(select(func.count()).select_from(TradeLotModel)) == 0
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0


def test_invalid_snapshot_keeps_hold_and_creates_no_trade_state() -> None:
    repo, factory = repository()
    value = bootstrap_command(invested_amount=Decimal("1999"))
    repo.cache_command(value)

    with pytest.raises(ValueError, match="invested amount"):
        PositionBootstrapService(repo).ensure(value, StrategySettings())

    assert repo.get_state(str(value.automation_id)).state == AutomationState.HOLD.value
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(TradeLotModel)) == 0
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0


@pytest.mark.parametrize(
    ("source", "quantity", "price", "commission"),
    [
        ("EXECUTED", 2, Decimal("100"), Decimal()),
        ("RECONCILED", 1, Decimal("100"), Decimal()),
        ("RECONCILED", 2, Decimal("99"), Decimal()),
        ("RECONCILED", 2, Decimal("100"), Decimal("1")),
    ],
)
def test_bootstrap_does_not_replace_incompatible_reconciled_ledger(source, quantity, price, commission) -> None:
    repo, factory = repository()
    value = bootstrap_command().model_copy(update={"state": AutomationState.IN_QUEUE})
    repo.cache_command(value)
    original = repo.create_trade_lot(
        automation_id=str(value.automation_id),
        source_intent_id=None,
        source=source,
        quantity_lots=quantity,
        entry_price=price,
        entry_commission=commission,
        opened_at=NOW,
    )

    with pytest.raises(ValueError, match="existing lot ledger"):
        PositionBootstrapService(repo).ensure(value, StrategySettings())

    assert repo.list_open_lots(str(value.automation_id)) == [original]
    assert repo.get_state(str(value.automation_id)).state == "IN_QUEUE"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_bootstrap_publication_does_not_split_the_activation_group(limit: int) -> None:
    repo, _factory = repository()
    value = bootstrap_command()
    repo.cache_command(value)
    PositionBootstrapService(repo).ensure(value, StrategySettings())

    rows = repo.ready_fact_outbox(limit, now=NOW, deadline_ms=1000)

    assert [row.sequence_number for row in rows] == [1, 2, 3, 4]


def test_delayed_bootstrap_member_blocks_the_whole_group_until_retry() -> None:
    repo, _factory = repository()
    value = bootstrap_command()
    repo.cache_command(value)
    PositionBootstrapService(repo).ensure(value, StrategySettings())
    original = repo.ready_fact_outbox(10, now=NOW, deadline_ms=0)
    retry_at = NOW + timedelta(seconds=1)
    repo.schedule_fact_retry((original[2].event_id,), retry_count=1, next_retry_at=retry_at)

    assert repo.ready_fact_outbox(1, now=NOW, deadline_ms=0) == []
    retried = repo.ready_fact_outbox(1, now=retry_at, deadline_ms=0)
    assert [row.event_id for row in retried] == [row.event_id for row in original]


def test_interleaved_bootstrap_groups_expand_only_the_selected_group() -> None:
    repo, _factory = repository()
    first = bootstrap_command()
    second = first.model_copy(
        update={
            "automation_id": UUID("00000000-0000-4000-8000-000000000511"),
            "bootstrap": first.bootstrap.model_copy(
                update={
                    "position_cycle_id": UUID("00000000-0000-4000-8000-000000000512"),
                    "position_lot_id": UUID("00000000-0000-4000-8000-000000000513"),
                }
            ),
        }
    )
    for value in (first, second):
        repo.cache_command(value)
        PositionBootstrapService(repo).ensure(value, StrategySettings())

    rows = repo.ready_fact_outbox(2, now=NOW, deadline_ms=0)

    assert len(rows) == 4
    assert len({row.automation_id for row in rows}) == 1
    assert [row.sequence_number for row in rows] == [1, 2, 3, 4]


@pytest.mark.parametrize("delayed_bootstrap", [False, True])
def test_bootstrap_batch_boundary_and_retry_preserve_an_independent_peer(delayed_bootstrap: bool) -> None:
    repo, factory = repository()
    value = bootstrap_command()
    repo.cache_command(value)
    PositionBootstrapService(repo).ensure(value, StrategySettings())
    original = repo.ready_fact_outbox(10, now=NOW, deadline_ms=0)
    if delayed_bootstrap:
        repo.schedule_fact_retry((original[2].event_id,), retry_count=1, next_retry_at=NOW + timedelta(seconds=1))
    peer = command(automation="bootstrap-peer")
    repo.cache_command(peer)
    with factory.begin() as session:
        peer_fact = FactOutboxWriter(clock=lambda: NOW).append(
            session,
            session.get_one(CachedAutomationModel, str(peer.automation_id)),
            payload=audit_payload(),
            safe_message="Independent peer audit",
            occurred_at=NOW - timedelta(milliseconds=1),
        )

    rows = repo.ready_fact_outbox(2, now=NOW, deadline_ms=0)

    assert rows[0].event_id == str(peer_fact.event_id)
    assert [row.event_id for row in rows[1:]] == ([] if delayed_bootstrap else [row.event_id for row in original])
