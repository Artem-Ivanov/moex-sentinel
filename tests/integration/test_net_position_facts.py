"""Worker LIFO valuation and distinct strategy basis survive Core fact ingress."""

import asyncio
from datetime import timedelta
from decimal import Decimal

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
from tests.storage.test_trading_facts_models import automation_model
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model
from tests.trading_automaton.command_factory import command, decision_item
from tests.trading_automaton.storage.test_local_repository import NOW, filled_buy, repository
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


@pytest.mark.parametrize(
    ("lot_size", "entry_fee", "exit_fee", "expected_net"),
    [(1, "0", "0", "140"), (10, "2", "1", "1395"), (100, "2", "1", "13995")],
)
def test_next_tick_keeps_lifo_basis_when_broker_average_differs_after_partial_sale(
    lot_size,
    entry_fee,
    exit_fee,
    expected_net,
):
    repo, factory = repository()
    cmd = command(broker="broker-1", account="account-1", instrument="instrument-1", lot_size=lot_size)
    aid = str(cmd.automation_id)
    repo.cache_command(cmd)
    core_engine = create_engine("sqlite:///:memory:")
    CoreBase.metadata.create_all(core_engine)
    core_factory = sessionmaker(core_engine, expire_on_commit=False)
    with core_factory.begin() as session:
        session.add(user_broker_model(str(cmd.user_broker_id), cmd.account_id))
        instrument = instrument_model(str(cmd.instrument_id), str(cmd.user_broker_id))
        instrument.lot_size = lot_size
        session.add(instrument)
        session.add(automation_model(aid, user_broker_id=str(cmd.user_broker_id), instrument_id=str(cmd.instrument_id)))
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

    try:
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
            DecisionMaterializerService().materialize(
                prepared,
                PositionWorkItem(cmd, False),
                InstrumentMarketState(
                    cmd.external_instrument_id,
                    StreamOrderBook(
                        cmd.external_instrument_id,
                        (OrderBookLevel(Decimal("220"), 10),),
                        (OrderBookLevel(Decimal("221"), 10),),
                        NOW,
                        True,
                    ),
                ),
                None,
                cash=None,
                pending_cash={},
                snapshot_at=NOW,
            )
        )
        snapshot = result.item.position_snapshot
        assert Decimal(snapshot["net_pnl"]) == Decimal(expected_net)
        assert Decimal(snapshot["average_price"]) == Decimal("100")
        assert Decimal(snapshot["invested_amount"]) == Decimal("100") * lot_size
        assert result.item.average_price == Decimal("150")  # Strategy input is unchanged.
        repo.save_decision_batch((result.item,), occurred_at=NOW + timedelta(seconds=4))
        facts = publish()
        assert ingress.publish(facts).failures == ()  # Exact retry remains immutable.
        with core_factory() as session:
            cycle = session.scalar(select(PositionCycleModel))
            assert cycle.average_entry_price == Decimal("100")
            assert cycle.invested_amount == Decimal("100") * lot_size
            assert cycle.net_pnl == Decimal(expected_net)
            decisions = session.scalars(select(TradeDecisionModel)).all()
            assert len(decisions) == 4
            assert next(item for item in decisions if item.decision == "WAIT").average_price == Decimal("150")
    finally:
        factory.kw["bind"].dispose()
        core_engine.dispose()
