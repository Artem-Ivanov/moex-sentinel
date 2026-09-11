import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from sentinel_contracts.broker_execution import BrokerPosition, OrderBookLevel
from sentinel_contracts.streaming_market import (
    InstrumentMarketState,
    MarketBatchSnapshot,
    StreamOrderBook,
    StreamTradingStatus,
)
from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.services.account_cash_reservation import AccountCashReservationService
from trading_automaton.services.active_intent_gate import ActiveIntentGateService
from trading_automaton.services.batch_runtime import BatchTradingRuntimeService, PostCommitBatchError
from trading_automaton.services.decision import TradeDecision
from trading_automaton.services.market_indicators import MarketIndicators
from trading_automaton.services.position_batch_scheduler import (
    PositionBatchSchedulerService,
    PositionEvaluationResult,
    PreparedDecision,
)
from trading_automaton.services.position_state_hydration import PositionStateCacheService
from trading_automaton.services.streaming_batch_tick import StreamingBatchTickService
from trading_automaton.services.streaming_cycle_transition import StreamingCycleTransitionService
from trading_automaton.services.streaming_position_decision import HydratedPositionState
from trading_automaton.storage.repository import IntentHistory, TradingCycleState

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


@pytest.mark.parametrize("after_load", [False, True])
def test_remote_snapshot_expiry_prevents_durable_batch(after_load):
    async def scenario():
        clock = [NOW if after_load else NOW + timedelta(seconds=3)]

        class DelayedStates(States):
            async def get(self, automation_id):
                value = await super().get(automation_id)
                clock[0] = NOW + timedelta(seconds=3)
                return value

        batch = Batch()
        service = StreamingBatchTickService(Scheduler(), DelayedStates(), Commissions(), batch, now=lambda: clock[0])
        snapshot = MarketBatchSnapshot.immutable("remote", NOW, {}).model_copy(
            update={"expires_at": NOW + timedelta(seconds=2)}
        )
        await service.run_tick((command(),), snapshot)
        assert batch.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["empty", "nonfinite", "crossed", "inconsistent", "stale"])
def test_invalid_book_isolated_from_valid_position_batch(invalid: str, caplog, monkeypatch) -> None:
    # App logging setup in integration tests may disable pre-existing loggers.
    monkeypatch.setattr("trading_automaton.services.streaming_batch_tick.LOGGER.disabled", False)

    class WaitDecider:
        async def decide(self, item, market):
            return PositionEvaluationResult(
                TradeDecision(DecisionKind.WAIT, 0, None, "NO_THRESHOLD"), item.state, Decimal()
            )

    async def scenario():
        batch = Batch()
        states = States()
        tick = StreamingBatchTickService(
            PositionBatchSchedulerService(WaitDecider()),
            states,
            Commissions(),
            batch,
            cycles=StreamingCycleTransitionService(now=lambda: NOW),
            now=lambda: NOW,
        )
        valid_book = StreamOrderBook(
            "instrument", (OrderBookLevel(Decimal("99"), 1),), (OrderBookLevel(Decimal("100"), 1),), NOW, True
        )
        changes = {
            "empty": {"bids": ()},
            "nonfinite": {"bids": (valid_book.best_bid.model_copy(update={"price": Decimal("NaN")}),)},
            "crossed": {"asks": valid_book.bids},
            "inconsistent": {"is_consistent": False},
            "stale": {"captured_at": NOW - timedelta(seconds=3)},
        }
        bad = InstrumentMarketState(
            "instrument",
            order_book=valid_book.model_copy(update=changes[invalid]),
            trading_status=StreamTradingStatus("instrument", "NORMAL_TRADING", True, True, NOW),
        )
        good = bad.model_copy(update={"instrument_id": "good", "order_book": valid_book})
        commands = (command(), baseline_command(automation="good", instrument="good"))
        await tick.run_tick(commands, MarketBatchSnapshot.immutable("batch", NOW, {"instrument": bad, "good": good}))
        return batch

    batch = asyncio.run(scenario())
    items, requests, _ = batch.calls[0]
    assert items[-1].instrument_id == "good"
    if invalid in {"empty", "nonfinite"}:
        assert len(items) == 1
        assert any(getattr(record, "reason_code", None) == "INVALID_ORDER_BOOK" for record in caplog.records)
    else:
        assert len(items) == 2
        assert items[0].decision == "WAIT"
        assert (
            items[0].reason_code
            == {"crossed": "CROSSED_ORDER_BOOK", "inconsistent": "ORDER_BOOK_UNAVAILABLE", "stale": "STALE_ORDER_BOOK"}[
                invalid
            ]
        )
        assert items[0].cycle_state["pending_low"] is None
    assert requests == ()


def test_snapshot_clock_is_shared_by_cycle_and_decision() -> None:
    async def scenario():
        states = States()
        batch = Batch()
        service = StreamingBatchTickService(
            Scheduler(),
            states,
            Commissions(),
            batch,
            cycles=StreamingCycleTransitionService(now=lambda: NOW + timedelta(seconds=3)),
            now=lambda: NOW + timedelta(seconds=3),
        )
        market = InstrumentMarketState(
            "instrument",
            order_book=StreamOrderBook(
                "instrument",
                (OrderBookLevel(Decimal("99"), 1),),
                (OrderBookLevel(Decimal("99.1"), 1),),
                NOW,
                True,
            ),
        )
        await service.run_tick((command(),), MarketBatchSnapshot.immutable("delayed", NOW, {"instrument": market}))
        return batch

    batch = asyncio.run(scenario())
    item = batch.calls[0][0][0]
    assert item.cycle_state["pending_low"] == Decimal("99")
    assert item.cycle_state["updated_at"] == NOW


def command() -> AutomationCommand:
    return baseline_command()


class Scheduler:
    async def prepare(self, items, snapshot):
        return (
            PreparedDecision(
                items[0].command,
                snapshot.snapshot_id,
                snapshot.created_at,
                PositionEvaluationResult(
                    TradeDecision(DecisionKind.BUY_MORE, 2, Decimal("100.1"), "BUY"),
                    items[0].state,
                    Decimal("1.25"),
                ),
            ),
        )


class States:
    def __init__(self) -> None:
        self.updated = []

    async def get(self, automation_id):
        return HydratedPositionState(
            BrokerPosition("instrument", Decimal("1"), Decimal("100"), Decimal("100"), "RUB"),
            IntentHistory(Decimal("100"), 0, Decimal(), 1, Decimal()),
            (),
            TradingCycleState("automation", None, None, True, None, NOW),
            MarketIndicators(Decimal("0.5"), Decimal("0.5"), "TEST", None, None, None, NOW),
        )

    async def update(self, automation_id, value):
        self.updated.append((automation_id, value))


class Commissions:
    def estimate(self, key, side, order_amount, *, now):
        return Decimal("1.25")


class ForbiddenCommissionEstimate:
    def estimate(self, *args, **kwargs):
        raise AssertionError("batch must use PreparedDecision.evaluation.estimated_commission")


class Batch:
    def __init__(self) -> None:
        self.calls = []

    async def run_batch(self, items, requests, *, snapshot_at):
        self.calls.append((items, requests, snapshot_at))
        return "result"


class Audit:
    def __init__(self) -> None:
        self.calls = []

    def record_decision_process(self, **values):
        self.calls.append(values)


def test_assembles_decision_intent_and_dispatch_from_one_snapshot() -> None:
    async def scenario():
        batch = Batch()
        audit = Audit()
        cash = AccountCashReservationService()
        await cash.replace_snapshot("account", "RUB", Decimal("5000"))
        service = StreamingBatchTickService(
            Scheduler(),
            States(),
            Commissions(),
            batch,
            cash=cash,
            now=lambda: NOW,
            audit=audit,
            id_factory=lambda: "intent-1",
        )
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot-1",
            NOW,
            {
                "instrument": InstrumentMarketState(
                    "instrument",
                    order_book=StreamOrderBook(
                        "instrument",
                        (OrderBookLevel(Decimal("100"), 1),),
                        (OrderBookLevel(Decimal("100.1"), 1),),
                        NOW - timedelta(milliseconds=250),
                        True,
                    ),
                    trading_status=StreamTradingStatus(
                        "instrument",
                        "NORMAL_TRADING",
                        True,
                        True,
                        NOW - timedelta(hours=1),
                    ),
                )
            },
        )
        result = await service.run_tick((command(),), snapshot)
        return audit, batch, result

    audit, batch, result = asyncio.run(scenario())

    items, requests, snapshot_at = batch.calls[0]
    assert result == "result"
    assert snapshot_at == NOW
    assert items[0].estimated_commission == Decimal("1.25")
    assert items[0].intent.idempotency_key == "intent-1"
    assert requests[0].idempotency_key == "intent-1"
    assert requests[0].quantity_lots == 2
    assert audit.calls[0]["order_book_age_ms"] == 250
    assert audit.calls[0]["trading_status"] == "NORMAL_TRADING"
    assert audit.calls[0]["free_cash"] == "5000"
    assert audit.calls[0]["reserved_cash"] == "0"
    assert audit.calls[0]["required_order_cash"] == "2003.25"
    assert audit.calls[0]["estimated_buy_commission"] == "1.25"
    assert audit.calls[0]["available_after_reserve"] == "2996.75"
    assert "minimum_free_cash" not in audit.calls[0]
    assert items[0].strategy_snapshot == {
        "strategy_code": "ADAPTIVE_SCALPING",
        "strategy_version": "1.0",
        "buy_order_lots": 1,
        "stop_loss_percent": "5",
        "take_profit_percent": "6",
        "averaging_step_percent": "0.5",
        "partial_take_profit_percent": "0.5",
        "partial_sell_percent": "25",
        "max_partial_sell_steps": 3,
        "order_ttl_seconds": 10,
        "order_retry_limit": 3,
        "core_retry_limit": 5,
        "enabled": True,
    }


def test_batch_reuses_prepared_commission_for_persistence_dispatch_and_audit() -> None:
    async def scenario():
        batch = Batch()
        audit = Audit()
        service = StreamingBatchTickService(
            Scheduler(),
            States(),
            ForbiddenCommissionEstimate(),
            batch,
            now=lambda: NOW,
            audit=audit,
            id_factory=lambda: "intent-1",
        )
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot-1",
            NOW,
            {
                "instrument": InstrumentMarketState(
                    "instrument",
                    order_book=StreamOrderBook(
                        "instrument",
                        (OrderBookLevel(Decimal("100"), 1),),
                        (OrderBookLevel(Decimal("100.1"), 1),),
                        NOW,
                        True,
                    ),
                )
            },
        )
        await service.run_tick((command(),), snapshot)
        return batch, audit

    batch, audit = asyncio.run(scenario())
    items, requests, _snapshot_at = batch.calls[0]

    assert items[0].estimated_commission == Decimal("1.25")
    assert requests[0].required_cash == Decimal("2003.25")
    assert audit.calls[0]["decision"] == "BUY_MORE"


def test_batch_never_reestimates_prepared_buy_or_sell_commission() -> None:
    class BuyAndSellScheduler:
        async def prepare(self, items, snapshot):
            return (
                PreparedDecision(
                    items[0].command,
                    snapshot.snapshot_id,
                    snapshot.created_at,
                    PositionEvaluationResult(
                        TradeDecision(DecisionKind.BUY_MORE, 1, Decimal("100"), "BUY"),
                        items[0].state,
                        Decimal("1.25"),
                    ),
                ),
                PreparedDecision(
                    items[1].command,
                    snapshot.snapshot_id,
                    snapshot.created_at,
                    PositionEvaluationResult(
                        TradeDecision(DecisionKind.SELL_PART, 1, Decimal("100"), "SELL"),
                        items[1].state,
                        Decimal("2.50"),
                    ),
                ),
            )

    async def scenario():
        batch = Batch()
        audit = Audit()
        ids = iter(("process-buy", "intent-buy", "process-sell", "intent-sell"))
        service = StreamingBatchTickService(
            BuyAndSellScheduler(),
            States(),
            ForbiddenCommissionEstimate(),
            batch,
            now=lambda: NOW,
            audit=audit,
            id_factory=lambda: next(ids),
        )
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot-1",
            NOW,
            {
                "instrument": InstrumentMarketState(
                    "instrument",
                    order_book=StreamOrderBook(
                        "instrument",
                        (OrderBookLevel(Decimal("100"), 1),),
                        (OrderBookLevel(Decimal("100.1"), 1),),
                        NOW,
                        True,
                    ),
                )
            },
        )
        await service.run_tick((command(), command()), snapshot)
        return batch, audit

    batch, audit = asyncio.run(scenario())
    items, requests, _snapshot_at = batch.calls[0]

    assert [item.estimated_commission for item in items] == [Decimal("1.25"), Decimal("2.50")]
    assert [request.required_cash for request in requests] == [Decimal("1001.25"), Decimal()]
    assert [values["decision"] for values in audit.calls] == ["BUY_MORE", "SELL_PART"]


def test_applies_cycle_transition_before_decision_and_persists_updated_cycle() -> None:
    class Cycles:
        def apply(self, command, state, market, *, snapshot_at=None):
            return state.model_copy(
                update={
                    "cycle": state.cycle.model_copy(
                        update={"pending_low": Decimal("99"), "updated_at": NOW},
                    )
                }
            )

    async def scenario():
        states = States()
        batch = Batch()
        service = StreamingBatchTickService(
            Scheduler(),
            states,
            Commissions(),
            batch,
            cycles=Cycles(),
            now=lambda: NOW,
            id_factory=lambda: "intent-1",
        )
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot",
            NOW,
            {
                "instrument": InstrumentMarketState(
                    "instrument",
                    order_book=StreamOrderBook(
                        "instrument",
                        (OrderBookLevel(Decimal("99"), 1),),
                        (OrderBookLevel(Decimal("99.1"), 1),),
                        NOW,
                        True,
                    ),
                )
            },
        )
        await service.run_tick((command(),), snapshot)
        return states, batch

    states, batch = asyncio.run(scenario())

    assert states.updated[0][1].cycle.pending_low == Decimal("99")
    assert batch.calls[0][0][0].cycle_state["pending_low"] == Decimal("99")
    assert batch.calls[0][0][0].cycle_state["expected_state"]["pending_low"] is None


def test_batch_rollback_does_not_publish_cycle_transition_to_hot_state() -> None:
    class Cycles:
        def apply(self, command, state, market, *, snapshot_at=None):
            return state.model_copy(
                update={
                    "cycle": state.cycle.model_copy(
                        update={"pending_low": Decimal("99"), "updated_at": NOW},
                    )
                }
            )

    class FailingBatch:
        async def run_batch(self, items, requests, *, snapshot_at):
            raise ValueError("persistence failed")

    async def scenario():
        states = States()
        service = StreamingBatchTickService(
            Scheduler(),
            states,
            Commissions(),
            FailingBatch(),
            cycles=Cycles(),
            now=lambda: NOW,
            id_factory=lambda: "intent-1",
        )
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot",
            NOW,
            {
                "instrument": InstrumentMarketState(
                    "instrument",
                    order_book=StreamOrderBook(
                        "instrument",
                        (OrderBookLevel(Decimal("99"), 1),),
                        (OrderBookLevel(Decimal("99.1"), 1),),
                        NOW,
                        True,
                    ),
                )
            },
        )
        with suppress(ValueError):
            await service.run_tick((command(),), snapshot)
        return states

    states = asyncio.run(scenario())

    assert states.updated == []


def test_postcommit_failure_publishes_committed_cycle_before_propagating() -> None:
    class Cycles:
        def apply(self, command, state, market, *, snapshot_at=None):
            return state.model_copy(
                update={
                    "cycle": state.cycle.model_copy(
                        update={"pending_low": Decimal("99"), "updated_at": NOW},
                    )
                }
            )

    class PostCommitFailingBatch:
        async def run_batch(self, items, requests, *, snapshot_at):
            persisted = SimpleNamespace(decisions=(), intents=())
            raise PostCommitBatchError(persisted, ValueError("cash publication failed"))

    async def scenario():
        states = States()
        service = StreamingBatchTickService(
            Scheduler(),
            states,
            Commissions(),
            PostCommitFailingBatch(),
            cycles=Cycles(),
            now=lambda: NOW,
            id_factory=lambda: "intent-1",
        )
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot",
            NOW,
            {
                "instrument": InstrumentMarketState(
                    "instrument",
                    order_book=StreamOrderBook(
                        "instrument",
                        (OrderBookLevel(Decimal("99"), 1),),
                        (OrderBookLevel(Decimal("99.1"), 1),),
                        NOW,
                        True,
                    ),
                )
            },
        )
        with pytest.raises(PostCommitBatchError, match="cash publication failed"):
            await service.run_tick((command(),), snapshot)
        return states

    states = asyncio.run(scenario())

    assert states.updated[-1][1].cycle.pending_low == Decimal("99")


def test_budgets_parallel_buy_intents_without_publishing_precommit_reservation() -> None:
    class TwoBuyScheduler:
        async def prepare(self, items, snapshot):
            return tuple(
                PreparedDecision(
                    item.command,
                    snapshot.snapshot_id,
                    snapshot.created_at,
                    PositionEvaluationResult(
                        TradeDecision(DecisionKind.BUY_MORE, 1, Decimal("100"), "BUY"),
                        item.state,
                        Decimal("1.25"),
                    ),
                )
                for item in items
            )

    class MultiStates:
        async def get(self, automation_id):
            return HydratedPositionState(
                BrokerPosition(automation_id, Decimal("1"), Decimal("100"), Decimal("100"), "RUB"),
                IntentHistory(Decimal("100"), 0, Decimal(), 1, Decimal()),
                (),
                TradingCycleState(automation_id, None, None, True, None, NOW),
                MarketIndicators(Decimal("0.5"), Decimal("0.5"), "TEST", None, None, None, NOW),
            )

    async def scenario():
        cash = AccountCashReservationService()
        await cash.replace_snapshot("account", "RUB", Decimal("1500"))
        batch = Batch()
        audit = Audit()
        ids = iter(("process-1", "intent-1", "process-2", "intent-2"))
        service = StreamingBatchTickService(
            TwoBuyScheduler(),
            MultiStates(),
            Commissions(),
            batch,
            cash=cash,
            now=lambda: NOW,
            audit=audit,
            id_factory=lambda: next(ids),
        )
        second = baseline_command(automation="automation-2", instrument="instrument-2")

        def book(instrument):
            return InstrumentMarketState(
                instrument,
                order_book=StreamOrderBook(
                    instrument,
                    (OrderBookLevel(Decimal("99.9"), 1),),
                    (OrderBookLevel(Decimal("100"), 1),),
                    NOW,
                    True,
                ),
            )

        snapshot = MarketBatchSnapshot.immutable(
            "snapshot",
            NOW,
            {"instrument": book("instrument"), "instrument-2": book("instrument-2")},
        )
        await service.run_tick((command(), second), snapshot)
        return batch, cash, audit

    batch, cash, audit = asyncio.run(scenario())
    items, requests, _snapshot_at = batch.calls[0]

    assert len(requests) == 1
    assert [item.decision for item in items] == ["BUY_MORE", "WAIT"]
    assert items[1].reason_code == "INSUFFICIENT_FREE_CASH"
    assert [item.estimated_commission for item in items] == [Decimal("1.25"), Decimal()]
    assert audit.calls[1]["estimated_buy_commission"] == "0"
    assert asyncio.run(cash.reserved("account", "RUB")) == Decimal()


def test_does_not_publish_decision_audit_when_batch_persistence_fails() -> None:
    class FailingBatch:
        async def run_batch(self, items, requests, *, snapshot_at):
            raise ValueError("persistence failed")

    async def scenario():
        audit = Audit()
        service = StreamingBatchTickService(
            Scheduler(),
            States(),
            Commissions(),
            FailingBatch(),
            now=lambda: NOW,
            audit=audit,
            id_factory=lambda: "intent-1",
        )
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot",
            NOW,
            {
                "instrument": InstrumentMarketState(
                    "instrument",
                    order_book=StreamOrderBook(
                        "instrument",
                        (OrderBookLevel(Decimal("100"), 1),),
                        (OrderBookLevel(Decimal("100.1"), 1),),
                        NOW,
                        True,
                    ),
                )
            },
        )
        with suppress(ValueError):
            await service.run_tick((command(),), snapshot)
        return audit

    audit = asyncio.run(scenario())

    assert audit.calls == []


@pytest.mark.parametrize("kind", [DecisionKind.WAIT, DecisionKind.SELL_PART])
def test_does_not_read_cash_for_non_buy_decisions(kind: DecisionKind) -> None:
    class NonBuyScheduler:
        async def prepare(self, items, snapshot):
            return (
                PreparedDecision(
                    items[0].command,
                    snapshot.snapshot_id,
                    snapshot.created_at,
                    PositionEvaluationResult(
                        TradeDecision(
                            kind,
                            0 if kind is DecisionKind.WAIT else 1,
                            None if kind is DecisionKind.WAIT else Decimal("100"),
                            kind.value,
                        ),
                        items[0].state,
                        Decimal("1.25") if kind is DecisionKind.SELL_PART else Decimal(),
                    ),
                ),
            )

    class ForbiddenCash:
        async def available(self, account_id, currency):
            raise AssertionError("cash.available must not be read")

        async def reserved(self, account_id, currency):
            raise AssertionError("cash.reserved must not be read")

    async def scenario():
        service = StreamingBatchTickService(
            NonBuyScheduler(),
            States(),
            Commissions(),
            Batch(),
            cash=ForbiddenCash(),
            now=lambda: NOW,
            id_factory=lambda: "intent-1",
        )
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot",
            NOW,
            {
                "instrument": InstrumentMarketState(
                    "instrument",
                    order_book=StreamOrderBook(
                        "instrument",
                        (OrderBookLevel(Decimal("99.9"), 1),),
                        (OrderBookLevel(Decimal("100"), 1),),
                        NOW,
                        True,
                    ),
                )
            },
        )
        await service.run_tick((command(),), snapshot)

    asyncio.run(scenario())


def test_serializes_concurrent_ticks_before_cash_budgeting() -> None:
    class BlockingBatch(Batch):
        def __init__(self):
            super().__init__()
            self.first_started = asyncio.Event()
            self.second_started = asyncio.Event()
            self.release = asyncio.Event()

        async def run_batch(self, items, requests, *, snapshot_at):
            self.calls.append((items, requests, snapshot_at))
            self.first_started.set()
            if len(self.calls) == 2:
                self.second_started.set()
            await self.release.wait()
            return "result"

    async def scenario():
        cash = AccountCashReservationService()
        await cash.replace_snapshot("account", "RUB", Decimal("1500"))
        batch = BlockingBatch()
        counter = iter(range(100))
        service = StreamingBatchTickService(
            Scheduler(),
            States(),
            Commissions(),
            batch,
            cash=cash,
            now=lambda: NOW,
            id_factory=lambda: f"id-{next(counter)}",
        )
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot",
            NOW,
            {
                "instrument": InstrumentMarketState(
                    "instrument",
                    order_book=StreamOrderBook(
                        "instrument",
                        (OrderBookLevel(Decimal("99.9"), 1),),
                        (OrderBookLevel(Decimal("100.1"), 1),),
                        NOW,
                        True,
                    ),
                )
            },
        )
        first = asyncio.create_task(service.run_tick((command(),), snapshot))
        await batch.first_started.wait()
        second = asyncio.create_task(service.run_tick((command(),), snapshot))
        with suppress(TimeoutError):
            await asyncio.wait_for(batch.second_started.wait(), timeout=0.05)
        calls_while_first_is_open = len(batch.calls)
        batch.release.set()
        await asyncio.gather(first, second)
        return calls_while_first_is_open

    assert asyncio.run(scenario()) == 1


def test_marks_position_as_having_active_intent_immediately_after_persistence() -> None:
    automation_id = str(command().automation_id)

    class PersistingBatch(Batch):
        async def run_batch(self, items, requests, *, snapshot_at):
            self.calls.append((items, requests, snapshot_at))
            return SimpleNamespace(
                persisted=SimpleNamespace(
                    decisions=(),
                    intents=(SimpleNamespace(automation_id=automation_id),),
                )
            )

    async def scenario():
        states = States()
        service = StreamingBatchTickService(
            Scheduler(),
            states,
            Commissions(),
            PersistingBatch(),
            now=lambda: NOW,
            id_factory=lambda: "intent-1",
        )
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot",
            NOW,
            {
                "instrument": InstrumentMarketState(
                    "instrument",
                    order_book=StreamOrderBook(
                        "instrument",
                        (OrderBookLevel(Decimal("100"), 1),),
                        (OrderBookLevel(Decimal("100.1"), 1),),
                        NOW,
                        True,
                    ),
                )
            },
        )
        await service.run_tick((command(),), snapshot)
        return states

    states = asyncio.run(scenario())

    assert states.updated[-1][0] == automation_id
    assert states.updated[-1][1].has_active_intent is True


def test_stale_hydration_after_persistence_does_not_create_second_intent() -> None:
    automation_id = str(command().automation_id)

    class AlwaysBuyDecider:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, item, state):
            self.calls += 1
            return PositionEvaluationResult(
                TradeDecision(DecisionKind.BUY_MORE, 1, Decimal("100.1"), "BUY"),
                item.state,
                Decimal("1.25"),
            )

    class Repository:
        def __init__(self) -> None:
            self.batches = []

        def save_decision_batch(self, items, *, occurred_at):
            self.batches.append(items)
            return SimpleNamespace(
                decisions=(),
                intents=tuple(
                    SimpleNamespace(
                        automation_id=item.automation_id,
                        idempotency_key=item.intent.idempotency_key,
                    )
                    for item in items
                    if item.intent is not None
                ),
            )

    class Dispatcher:
        async def dispatch(self, request, started):
            started.set_result(NOW)

    class Tracking:
        def track(self, intent_id, task, *, request=None):
            pass

    async def scenario():
        state = HydratedPositionState(
            BrokerPosition("instrument", Decimal("1"), Decimal("100"), Decimal("100"), "RUB"),
            IntentHistory(Decimal("100"), 0, Decimal(), 1, Decimal()),
            (),
            TradingCycleState("automation", None, None, True, None, NOW),
            MarketIndicators(Decimal("0.5"), Decimal("0.5"), "TEST", None, None, None, NOW),
        )
        states = PositionStateCacheService()
        await states.replace({automation_id: state})
        active_intents = ActiveIntentGateService()
        decider = AlwaysBuyDecider()
        scheduler = PositionBatchSchedulerService(decider, active_intents=active_intents)
        repository = Repository()
        batch = BatchTradingRuntimeService(
            repository,
            Dispatcher(),
            Tracking(),
            now=lambda: NOW,
            active_intents=active_intents,
        )
        ids = iter(("process-1", "intent-1", "process-2"))
        tick = StreamingBatchTickService(
            scheduler,
            states,
            Commissions(),
            batch,
            now=lambda: NOW,
            id_factory=lambda: next(ids),
        )
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot",
            NOW,
            {
                "instrument": InstrumentMarketState(
                    "instrument",
                    order_book=StreamOrderBook(
                        "instrument",
                        (OrderBookLevel(Decimal("100"), 1),),
                        (OrderBookLevel(Decimal("100.1"), 1),),
                        NOW,
                        True,
                    ),
                    trading_status=StreamTradingStatus(
                        "instrument",
                        "NORMAL_TRADING",
                        True,
                        True,
                        NOW,
                    ),
                )
            },
        )

        await tick.run_tick((command(),), snapshot)
        await states.replace({automation_id: state})
        await tick.run_tick((command(),), snapshot)
        return decider, repository

    decider, repository = asyncio.run(scenario())

    assert decider.calls == 1
    assert [sum(item.intent is not None for item in batch) for batch in repository.batches] == [1, 0]
