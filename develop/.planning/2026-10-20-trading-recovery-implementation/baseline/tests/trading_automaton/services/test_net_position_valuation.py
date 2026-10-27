"""Actual Net P&L accounts for each paid commission exactly once."""

import asyncio
from decimal import Decimal

import pytest

from sentinel_contracts.broker_execution import BrokerPosition, OrderBookLevel
from sentinel_contracts.streaming_market import InstrumentMarketState, StreamOrderBook
from sentinel_contracts.trading import DecisionKind
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
from trading_automaton.storage.models import LotAllocationModel, TradeLotModel
from trading_automaton.storage.repository import IntentBatchItem

BUY = "00000000-0000-4000-8000-000000000405"
PART = "00000000-0000-4000-8000-000000000451"
CLOSE = "00000000-0000-4000-8000-000000000452"


@pytest.mark.parametrize(
    ("lot_size", "entry_fee", "exit_fee", "buy_net", "partial_net", "closed_net"),
    [(10, "8", "1", "-8", "391", "388"), (100, "8", "1", "-8", "3991", "3988"), (10, "0", "0", "0", "400", "400")],
)
def test_terminal_and_next_tick_net_agree_after_buy_partial_sell_and_close(
    lot_size,
    entry_fee,
    exit_fee,
    buy_net,
    partial_net,
    closed_net,
    worker_repository_factory,
):
    repo, factory = worker_repository_factory()
    cmd = command(broker="broker-1", account="account-1", instrument="instrument-1", lot_size=lot_size)
    automation_id = str(cmd.automation_id)
    repo.cache_command(cmd)

    def execute(intent_id, side, quantity, price, fee):
        kind = "BUY_MORE" if side == "BUY" else "SELL_PART"
        price = Decimal(price)
        item = decision_item(cmd, intent_id=intent_id).model_copy(
            update={
                "decision": kind,
                "decision_quantity_lots": quantity,
                "limit_price": price,
                "intent": IntentBatchItem(intent_id, kind, side, quantity, price),
            }
        )
        repo.save_decision_batch((item,), occurred_at=NOW)
        return repo.finalize_execution(
            filled_buy().model_copy(
                update={
                    "intent_id": intent_id,
                    "side": side,
                    "quantity_lots": quantity,
                    "requested_price": price,
                    "requested_amount": price * lot_size * quantity,
                    "executed_amount": price * lot_size * quantity,
                    "executed_lots": quantity,
                    "executed_price": price,
                    "executed_commission": Decimal(fee),
                    "lot_size": lot_size,
                }
            )
        ).authoritative_position_snapshot

    async def next_tick(snapshot, mark):
        state = HydratedPositionState(
            BrokerPosition(
                cmd.external_instrument_id,
                Decimal(snapshot["quantity_lots"]),
                Decimal(snapshot["average_price"]),
                Decimal(mark),
                "RUB",
            ),
            repo.intent_history(automation_id),
            tuple(repo.list_open_lots(automation_id)),
            repo.get_cycle_state(automation_id, now=NOW),
            MarketIndicators(Decimal("0.5"), Decimal("0.5"), "TEST", None, None, None, NOW),
            realized_pnl=repo.realized_pnl(automation_id),
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
        result = await DecisionMaterializerService(settings=StrategySettings()).materialize(
            prepared,
            PositionWorkItem(cmd, False),
            InstrumentMarketState(
                cmd.external_instrument_id,
                StreamOrderBook(
                    cmd.external_instrument_id,
                    (OrderBookLevel(Decimal(mark), 10),),
                    (OrderBookLevel(Decimal(mark) + 1, 10),),
                    NOW,
                    True,
                ),
            ),
            None,
            cash=None,
            pending_cash={},
            snapshot_at=NOW,
        )
        return result.item.position_snapshot

    bought = execute(BUY, "BUY", 4, "100", entry_fee)
    assert Decimal(bought["net_pnl"]) == Decimal(buy_net)
    assert Decimal(asyncio.run(next_tick(bought, "100"))["net_pnl"]) == Decimal(buy_net)

    partial = execute(PART, "SELL", 1, "110", exit_fee)
    assert partial["quantity_lots"] == 3
    assert Decimal(partial["net_pnl"]) == Decimal(partial_net)
    assert Decimal(asyncio.run(next_tick(partial, "110"))["net_pnl"]) == Decimal(partial_net)

    closed = execute(CLOSE, "SELL", 3, "110", str(Decimal(exit_fee) * 3))
    assert closed["quantity_lots"] == 0
    assert Decimal(closed["net_pnl"]) == Decimal(closed_net)
    assert Decimal(closed["net_pnl"]) == Decimal(closed["realized_pnl"])
    assert Decimal(asyncio.run(next_tick(closed, "110"))["net_pnl"]) == Decimal(closed_net)


def test_finalization_valuation_retains_held_allocation_decimal_precision(worker_repository_factory):
    repo, factory = worker_repository_factory()
    realized = Decimal("10") - Decimal("1") / Decimal("3")
    with factory() as session:
        lot = TradeLotModel(
            id="held-lot",
            automation_id="held-automation",
            source_intent_id="held-buy",
            source="EXECUTED",
            original_lots=3,
            remaining_lots=2,
            entry_price=Decimal("100"),
            entry_commission=Decimal("1"),
            opened_at=NOW,
        )
        allocation = LotAllocationModel(
            id="held-allocation",
            automation_id="held-automation",
            sell_intent_id="held-sell",
            lot_id=lot.id,
            quantity_lots=1,
            exit_price=Decimal("101"),
            exit_commission=Decimal("0"),
            realized_pnl=realized,
            closed_at=NOW,
        )
        session.add_all([lot, allocation])
        session.flush()

        snapshot = repo._authoritative_position_snapshot(
            session,
            "held-automation",
            lot_size=10,
            mark_price=Decimal("101"),
        )

        assert allocation.realized_pnl == realized
        assert Decimal(snapshot["realized_pnl"]) == realized
        assert Decimal(snapshot["actual_commissions"]) == Decimal("1")
        assert Decimal(snapshot["net_pnl"]) == realized + Decimal("20") - Decimal("1") * 2 / 3
