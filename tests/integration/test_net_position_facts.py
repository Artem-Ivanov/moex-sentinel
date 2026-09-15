"""Worker LIFO valuation and distinct strategy basis survive Core fact ingress."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.services.trading_fact_mapping import TradingFactMapper
from moex_sentinel.storage.models import Base as CoreBase
from moex_sentinel.storage.models import PositionCycleModel, TradeDecisionModel
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from sentinel_contracts.broker_execution import BrokerPosition, OrderBookLevel
from sentinel_contracts.streaming_market import InstrumentMarketState, StreamOrderBook
from sentinel_contracts.trading import DecisionKind
from tests.integration.test_open_position_bootstrap import NOW as BOOTSTRAP_NOW
from tests.integration.test_open_position_bootstrap import bootstrap_context
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model
from tests.trading_automaton.command_factory import command, decision_item
from tests.trading_automaton.storage.worker_storage_helpers import NOW, filled_buy
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import (
    HydratedPositionState,
    MarketIndicators,
    PositionEvaluationResult,
    PositionWorkItem,
    PreparedDecision,
    TradeDecision,
)
from trading_automaton.services.decision_materialization import DecisionMaterializerService
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.repository import IntentBatchItem

BUY = "00000000-0000-4000-8000-000000000405"
PART = "00000000-0000-4000-8000-000000000451"
CLOSE = "00000000-0000-4000-8000-000000000452"


@pytest.fixture(
    params=[
        pytest.param((1, "0", "0", "140"), id="lot1-no-fees"),
        pytest.param((10, "2", "1", "1395"), id="lot10-with-fees"),
        pytest.param((100, "2", "1", "13995"), id="lot100-with-fees"),
    ]
)
def lifo_position(request, worker_repository_factory):
    """Own two databases and prepare two buys followed by a real LIFO sale."""
    lot_size, entry_fee, exit_fee, expected_net = request.param
    repo, factory = worker_repository_factory()
    cmd = command(broker="broker-1", account="account-1", instrument="instrument-1", lot_size=lot_size)
    aid = str(cmd.automation_id)
    repo.cache_command(cmd)
    core_engine = create_engine("sqlite:///:memory:")
    try:
        CoreBase.metadata.create_all(core_engine)
        core_factory = sessionmaker(core_engine, expire_on_commit=False)
        with core_factory.begin() as session:
            session.add(user_broker_model(str(cmd.user_broker_id), cmd.account_id))
            instrument = instrument_model(str(cmd.instrument_id), str(cmd.user_broker_id))
            instrument.lot_size = lot_size
            session.add(instrument)
            session.add(
                automation_model(aid, user_broker_id=str(cmd.user_broker_id), instrument_id=str(cmd.instrument_id))
            )
        ingress = TradingFactIngressService(
            lambda: TradingFactsUnitOfWork(core_factory), TradingFactMapper(), now=lambda: NOW
        )

        def publish():
            rows = repo.ready_fact_outbox(1000, now=NOW + timedelta(minutes=1), deadline_ms=0)
            facts = [FactSynchronizationService._envelope(row) for row in rows]
            assert facts
            accepted = ingress.publish(facts)
            assert accepted.failures == ()
            ack = accepted.results[0]
            repo.acknowledge_fact_outbox(
                aid, accepted_through_sequence=ack.accepted_through_sequence, current_revision=ack.current_revision
            )
            return facts

        terminal = None
        for index, (intent_id, side, price) in enumerate(
            [(BUY, "BUY", "100"), (PART, "BUY", "200"), (CLOSE, "SELL", "220")]
        ):
            kind = "BUY_MORE" if side == "BUY" else "SELL_PART"
            item = decision_item(cmd, intent_id=intent_id).model_copy(
                update={
                    "decision": kind,
                    "limit_price": Decimal(price),
                    "intent": IntentBatchItem(intent_id, kind, side, 1, Decimal(price)),
                }
            )
            repo.save_decision_batch((item,), occurred_at=NOW)
            publish()
            terminal = repo.finalize_execution(
                filled_buy().model_copy(
                    update={
                        "intent_id": intent_id,
                        "broker_order_id": f"synthetic-order-{index}",
                        "side": side,
                        "requested_price": Decimal(price),
                        "requested_amount": Decimal(price) * lot_size,
                        "executed_amount": Decimal(price) * lot_size,
                        "executed_price": Decimal(price),
                        "lot_size": lot_size,
                        "executed_commission": Decimal(entry_fee if side == "BUY" else exit_fee),
                        "occurred_at": NOW + timedelta(seconds=index),
                        "executed_at": NOW + timedelta(seconds=index),
                        "terminal_at": NOW + timedelta(seconds=index),
                    }
                )
            ).authoritative_position_snapshot
            publish()
        assert Decimal(terminal["net_pnl"]) == Decimal(expected_net)

        yield SimpleNamespace(
            repo=repo,
            command=cmd,
            core_factory=core_factory,
            ingress=ingress,
            publish=publish,
            lot_size=lot_size,
            exit_fee=exit_fee,
            expected_net=Decimal(expected_net),
        )
    finally:
        core_engine.dispose()


def materialize_wait(position, *, mark_price, captured_at):
    """Build a WAIT from the durable lots and the broker's distinct average."""
    repo = position.repo
    cmd = position.command
    aid = str(cmd.automation_id)
    state = HydratedPositionState(
        # The broker keeps weighted-average cost, unlike the Worker's LIFO ledger.
        BrokerPosition(cmd.external_instrument_id, Decimal(1), Decimal("150"), Decimal("220"), "RUB"),
        repo.intent_history(aid),
        tuple(repo.list_open_lots(aid)),
        repo.get_cycle_state(aid, now=NOW),
        MarketIndicators(Decimal("0.5"), Decimal("0.5"), "TEST", None, None, None, NOW),
        realized_pnl=repo.realized_pnl(aid),
    )
    prepared = PreparedDecision(
        cmd,
        "snapshot",
        NOW,
        PositionEvaluationResult(
            TradeDecision(DecisionKind.WAIT, 0, None, "NO_THRESHOLD"),
            state,
            Decimal(),
        ),
    )
    result = asyncio.run(
        DecisionMaterializerService(settings=StrategySettings()).materialize(
            prepared,
            PositionWorkItem(cmd, False),
            InstrumentMarketState(
                cmd.external_instrument_id,
                StreamOrderBook(
                    cmd.external_instrument_id,
                    (OrderBookLevel(mark_price, 10),),
                    (OrderBookLevel(mark_price + 1, 10),),
                    captured_at,
                    True,
                ),
            ),
            None,
            cash=None,
            pending_cash={},
            snapshot_at=NOW,
        )
    )
    return result.item


@pytest.fixture
def quoted_position(lifo_position):
    """Prepare the initial raw-precision quote before later observation scenarios."""
    position = lifo_position
    item = materialize_wait(
        position, mark_price=Decimal("230"), captured_at=NOW + timedelta(seconds=3, microseconds=65)
    )
    position.repo.save_decision_batch((item,), occurred_at=NOW + timedelta(seconds=4))
    facts = position.publish()
    assert position.ingress.publish(facts).failures == ()
    return position, item


def persist_observation(position, item, *, mark, quote_second, hydrated_quantity):
    """Publish a quote whose hydrated quantity is intentionally obsolete."""
    next_item = item.model_copy(
        update={
            "best_bid": Decimal(mark),
            "current_price": Decimal(mark),
            "position_snapshot_at": NOW + timedelta(seconds=quote_second),
            "position_snapshot": {**item.position_snapshot, "quantity_lots": hydrated_quantity},
        }
    )
    position.repo.save_decision_batch((next_item,), occurred_at=NOW + timedelta(seconds=6))
    facts = position.publish()
    assert position.ingress.publish(facts).failures == ()


def test_wait_revalues_remaining_lifo_with_raw_quote_precision(lifo_position):
    """A fresh WAIT keeps strategy inputs while delivering LIFO net P&L in milliseconds."""
    position = lifo_position
    item = materialize_wait(
        position, mark_price=Decimal("230"), captured_at=NOW + timedelta(seconds=3, microseconds=65)
    )
    assert item.position_snapshot_at == NOW + timedelta(seconds=3, microseconds=65)
    snapshot = item.position_snapshot
    assert Decimal(snapshot["net_pnl"]) == position.expected_net + 10 * position.lot_size
    assert Decimal(snapshot["average_price"]) == Decimal("100")
    assert Decimal(snapshot["invested_amount"]) == Decimal("100") * position.lot_size
    assert item.average_price == Decimal("150")
    position.repo.save_decision_batch((item,), occurred_at=NOW + timedelta(seconds=4))
    facts = position.publish()
    assert position.ingress.publish(facts).failures == ()
    with position.core_factory() as session:
        cycle = session.scalar(select(PositionCycleModel))
        assert cycle.average_entry_price == Decimal("100")
        assert cycle.invested_amount == Decimal("100") * position.lot_size
        assert cycle.net_pnl == position.expected_net + 10 * position.lot_size
        assert cycle.updated_at == NOW + timedelta(seconds=3)
        decisions = session.scalars(select(TradeDecisionModel)).all()
        assert len(decisions) == 4
        assert next(item for item in decisions if item.decision == "WAIT").average_price == Decimal("150")


def test_older_quote_cannot_regress_lifo_valuation(quoted_position):
    """A T4 quote delivered after T5 is acknowledged without regressing its valuation."""
    position, item = quoted_position
    for mark, quote_second, expected_mark in [("240", 5, "240"), ("210", 4, "240")]:
        persist_observation(position, item, mark=mark, quote_second=quote_second, hydrated_quantity=999)
        with position.core_factory() as session:
            cycle = session.scalar(select(PositionCycleModel))
            assert cycle.quantity_lots == 1
            assert cycle.net_pnl == position.expected_net + (Decimal(expected_mark) - 220) * position.lot_size
            assert cycle.updated_at == NOW + timedelta(seconds=5)


def test_wait_uses_durable_lots_when_hydrated_snapshot_is_obsolete(quoted_position):
    """A snapshot with quantity 999 cannot replace the one remaining durable lot."""
    position, item = quoted_position
    persist_observation(position, item, mark="240", quote_second=5, hydrated_quantity=999)
    with position.core_factory() as session:
        cycle = session.scalar(select(PositionCycleModel))
        assert cycle.quantity_lots == 1
        assert cycle.average_entry_price == Decimal("100")
        assert cycle.invested_amount == Decimal("100") * position.lot_size
        assert cycle.net_pnl == position.expected_net + (Decimal("240") - 220) * position.lot_size
        assert cycle.updated_at == NOW + timedelta(seconds=5)


@pytest.mark.parametrize(
    ("side", "kind", "expected_quantity", "expected_state", "expected_closed_at"),
    [
        pytest.param("BUY", "BUY_MORE", 2, "OPEN", None, id="late-buy"),
        pytest.param("SELL", "SELL_ALL", 0, "CLOSED", NOW + timedelta(seconds=4), id="late-sell-close"),
    ],
)
def test_delayed_fill_after_newer_quote_updates_ledger(
    quoted_position, side, kind, expected_quantity, expected_state, expected_closed_at
):
    """A fill at T4 observed at T7 changes the ledger after a T5 valuation."""
    position, item = quoted_position
    for mark, quote_second in [("240", 5), ("210", 4)]:
        persist_observation(position, item, mark=mark, quote_second=quote_second, hydrated_quantity=999)
    with position.core_factory() as session:
        assert session.scalar(select(PositionCycleModel)).updated_at == NOW + timedelta(seconds=5)
    delayed_id = "00000000-0000-4000-8000-000000000453"
    decision = decision_item(position.command, intent_id=delayed_id).model_copy(
        update={
            "decision": kind,
            "limit_price": Decimal("250"),
            "intent": IntentBatchItem(delayed_id, kind, side, 1, Decimal("250")),
        }
    )
    position.repo.save_decision_batch((decision,), occurred_at=NOW + timedelta(seconds=6))
    position.publish()
    finalized = filled_buy().model_copy(
        update={
            "intent_id": delayed_id,
            "side": side,
            "broker_order_id": "delayed-fill",
            "requested_price": Decimal("250"),
            "requested_amount": Decimal("250") * position.lot_size,
            "executed_amount": Decimal("250") * position.lot_size,
            "executed_price": Decimal("250"),
            "lot_size": position.lot_size,
            "executed_commission": Decimal(position.exit_fee),
            "executed_at": NOW + timedelta(seconds=4),
            "occurred_at": NOW + timedelta(seconds=7),
            "terminal_at": NOW + timedelta(seconds=7),
        }
    )
    position.repo.finalize_execution(finalized)
    facts = position.publish()
    assert position.ingress.publish(facts).failures == ()
    with position.core_factory() as session:
        cycle = session.scalar(select(PositionCycleModel))
        assert cycle.quantity_lots == expected_quantity
        assert cycle.state == expected_state
        assert cycle.closed_at == expected_closed_at
        assert cycle.updated_at == NOW + timedelta(seconds=7)


def test_bootstrap_wait_ticks_revalue_and_preserve_original_open_time():
    core_factory, repo, ingress, bootstrap_facts = bootstrap_context()
    try:
        accepted = ingress.publish(bootstrap_facts)
        assert accepted.failures == ()
        cmd = repo.list_active()[0]
        aid = str(cmd.automation_id)
        ack = accepted.results[0]
        repo.acknowledge_fact_outbox(
            aid, accepted_through_sequence=ack.accepted_through_sequence, current_revision=ack.current_revision
        )
        for second, mark in [(1, "105"), (2, "95")]:
            item = decision_item(cmd).model_copy(
                update={
                    "intent": None,
                    "decision": "WAIT",
                    "decision_quantity_lots": 0,
                    "limit_price": None,
                    "best_bid": Decimal(mark),
                    "position_snapshot": {},
                    "position_snapshot_at": BOOTSTRAP_NOW + timedelta(seconds=second),
                }
            )
            repo.save_decision_batch((item,), occurred_at=BOOTSTRAP_NOW + timedelta(seconds=second + 1))
            rows = repo.ready_fact_outbox(100, now=BOOTSTRAP_NOW + timedelta(minutes=1), deadline_ms=0)
            facts = [FactSynchronizationService._envelope(row) for row in rows]
            accepted = ingress.publish(facts)
            assert accepted.failures == ()
            assert ingress.publish(facts).failures == ()
            ack = accepted.results[0]
            repo.acknowledge_fact_outbox(
                aid, accepted_through_sequence=ack.accepted_through_sequence, current_revision=ack.current_revision
            )
            with core_factory() as session:
                cycle = session.scalar(select(PositionCycleModel))
                assert cycle.quantity_lots == 2
                assert cycle.opened_at == BOOTSTRAP_NOW
                assert cycle.created_at == BOOTSTRAP_NOW
                assert cycle.updated_at == BOOTSTRAP_NOW + timedelta(seconds=second)
                assert cycle.unrealized_pnl == (Decimal(mark) - 100) * 20
                assert cycle.net_pnl == cycle.unrealized_pnl
                assert cycle.accumulated_commissions == 0
    finally:
        core_factory.kw["bind"].dispose()
        repo._factory.kw["bind"].dispose()
