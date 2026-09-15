import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from sentinel_contracts.broker_execution import OrderSide
from trading_automaton.domain.errors import DurableDecisionPersistenceError
from trading_automaton.services.active_intent_gate import ActiveIntentGateService
from trading_automaton.services.batch_runtime import (
    BatchTradingRuntimeService,
    PostCommitBatchError,
)
from trading_automaton.services.order_dispatch import DispatchRequest
from trading_automaton.storage.repository import DecisionBatchItem, IntentBatchItem

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


class Repository:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def save_decision_batch(self, items, *, occurred_at):
        self.calls += 1
        if self.error:
            raise self.error
        intents = tuple(
            SimpleNamespace(automation_id=item.automation_id, idempotency_key=item.intent.idempotency_key)
            for item in items
            if item.intent is not None
        )
        return SimpleNamespace(decisions=(), intents=intents)


class Dispatcher:
    def __init__(self) -> None:
        self.calls = []
        self.release = asyncio.Event()

    async def dispatch(self, request, started):
        self.calls.append(request)
        started.set_result(NOW)
        await self.release.wait()


class Tracking:
    def __init__(self) -> None:
        self.calls = []

    def track(self, intent_id, task, *, request=None):
        self.calls.append((intent_id, task, request))


class Cash:
    def __init__(self) -> None:
        self.released = []
        self.committed = []

    async def release(self, intent_id):
        self.released.append(intent_id)

    async def reserve_committed_batch(self, reservations):
        self.committed.extend((item.account_id, item.currency, item.intent_id, item.amount) for item in reservations)


def batch_pair(
    intent_id: str = "intent-1",
    *,
    automation_id: str = "automation-1",
) -> tuple[DecisionBatchItem, DispatchRequest]:
    intent = IntentBatchItem(intent_id, "BUY_MORE", "BUY", 1, Decimal("100"))
    item = DecisionBatchItem(
        automation_id,
        "broker-1",
        "account-1",
        "instrument-1",
        "SHARE",
        0,
        1,
        Decimal(),
        Decimal("100"),
        Decimal("99.9"),
        Decimal("100"),
        Decimal(),
        Decimal("0.1"),
        "BUY_MORE",
        "BUY",
        1,
        Decimal("100"),
        {},
        "process-1",
        intent,
    )
    request = DispatchRequest(
        intent_id,
        "account-1",
        "instrument-1",
        OrderSide.BUY,
        1,
        Decimal("100"),
        automation_id=automation_id,
        process_id="process-1",
        broker_id="broker-1",
        reservation_currency="RUB",
        required_cash=Decimal("100.1"),
    )
    return item, request


def test_commits_once_then_waits_only_for_sdk_dispatch_start() -> None:
    async def scenario():
        repository = Repository()
        dispatcher = Dispatcher()
        tracking = Tracking()
        service = BatchTradingRuntimeService(repository, dispatcher, tracking, now=lambda: NOW)
        item, request = batch_pair()
        result = await service.run_batch((item,), (request,), snapshot_at=NOW)
        assert len(dispatcher.calls) == 1
        assert len(tracking.calls) == 1
        assert not tracking.calls[0][1].done()
        dispatcher.release.set()
        await tracking.calls[0][1]
        return repository, result

    repository, result = asyncio.run(scenario())

    assert repository.calls == 1
    assert result.sla[0].code == "OK"


def test_batch_rollback_prevents_every_sdk_dispatch() -> None:
    async def scenario():
        dispatcher = Dispatcher()
        service = BatchTradingRuntimeService(
            Repository(error=ValueError("rollback")),
            dispatcher,
            Tracking(),
            now=lambda: NOW,
        )
        item, request = batch_pair()
        with pytest.raises(DurableDecisionPersistenceError) as captured:
            await service.run_batch((item,), (request,), snapshot_at=NOW)
        return dispatcher, captured.value

    dispatcher, error = asyncio.run(scenario())

    assert dispatcher.calls == []
    assert isinstance(error.__cause__, ValueError)


def test_batch_rollback_leaves_cash_reservations_unchanged() -> None:
    async def scenario():
        cash = Cash()
        service = BatchTradingRuntimeService(
            Repository(error=ValueError("rollback")),
            Dispatcher(),
            Tracking(),
            now=lambda: NOW,
            cash=cash,
        )
        item, request = batch_pair()
        with suppress(DurableDecisionPersistenceError):
            await service.run_batch((item,), (request,), snapshot_at=NOW)
        return cash

    cash = asyncio.run(scenario())

    assert cash.released == []


def test_committed_cash_reservation_is_published_before_sdk_dispatch() -> None:
    class ObservingDispatcher(Dispatcher):
        def __init__(self, cash):
            super().__init__()
            self.cash = cash
            self.observed_committed = None

        async def dispatch(self, request, started):
            self.observed_committed = list(self.cash.committed)
            started.set_result(NOW)
            return SimpleNamespace()

    async def scenario():
        cash = Cash()
        dispatcher = ObservingDispatcher(cash)
        service = BatchTradingRuntimeService(
            Repository(),
            dispatcher,
            Tracking(),
            now=lambda: NOW,
            cash=cash,
        )
        item, request = batch_pair()
        result = await service.run_batch((item,), (request,), snapshot_at=NOW)
        return cash, dispatcher, result

    cash, dispatcher, _result = asyncio.run(scenario())

    assert cash.committed == [("account-1", "RUB", "intent-1", Decimal("100.1"))]
    assert dispatcher.observed_committed == cash.committed


def test_dispatch_failure_before_started_is_propagated_without_hanging() -> None:
    class FailingDispatcher:
        async def dispatch(self, request, started):
            raise ValueError("SUBMITTING persistence failed")

    async def scenario():
        item, request = batch_pair()
        service = BatchTradingRuntimeService(Repository(), FailingDispatcher(), Tracking(), now=lambda: NOW)
        with pytest.raises(PostCommitBatchError, match="SUBMITTING persistence failed") as captured:
            await asyncio.wait_for(service.run_batch((item,), (request,), snapshot_at=NOW), timeout=0.1)
        assert isinstance(captured.value.cause, ValueError)

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["duplicate", "missing", "unmatched", "mismatch"])
def test_rejects_invalid_intent_request_mapping_before_commit(case: str) -> None:
    item, request = batch_pair()
    if case == "duplicate":
        requests = (request, request)
    elif case == "missing":
        requests = ()
    elif case == "unmatched":
        _other_item, other_request = batch_pair("intent-2")
        requests = (request, other_request)
    else:
        requests = (DispatchRequest(**{**request.model_dump(), "quantity_lots": 2}),)
    repository = Repository()
    service = BatchTradingRuntimeService(repository, Dispatcher(), Tracking(), now=lambda: NOW)

    with pytest.raises(ValueError, match="intent.*request"):
        asyncio.run(service.run_batch((item,), requests, snapshot_at=NOW))

    assert repository.calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("broker_id", "other-broker"),
        ("required_cash", Decimal("99")),
        ("reservation_currency", "USD"),
    ],
)
def test_rejects_mismatched_dispatch_fields_before_commit(field: str, value: object) -> None:
    item, request = batch_pair()
    item = DecisionBatchItem(**{**item.model_dump(), "strategy_snapshot": {"currency": "RUB"}})
    repository = Repository()
    service = BatchTradingRuntimeService(repository, Dispatcher(), Tracking(), now=lambda: NOW)

    with pytest.raises(ValueError, match="intent request mapping fields"):
        asyncio.run(
            service.run_batch((item,), (DispatchRequest(**{**request.model_dump(), field: value}),), snapshot_at=NOW)
        )

    assert repository.calls == 0


def test_gate_protects_committed_intent_when_cash_publication_fails() -> None:
    class FailingCash(Cash):
        async def reserve_committed_batch(self, reservations):
            raise ValueError("cash cache unavailable")

    async def scenario():
        item, request = batch_pair()
        gate = ActiveIntentGateService()
        dispatcher = Dispatcher()
        service = BatchTradingRuntimeService(
            Repository(),
            dispatcher,
            Tracking(),
            now=lambda: NOW,
            cash=FailingCash(),
            active_intents=gate,
        )
        with pytest.raises(PostCommitBatchError, match="cash cache unavailable") as captured:
            await service.run_batch((item,), (request,), snapshot_at=NOW)
        assert isinstance(captured.value.cause, ValueError)
        return gate, dispatcher

    gate, dispatcher = asyncio.run(scenario())

    assert gate.has_active_intent("automation-1") is True
    assert dispatcher.calls == []


def test_committed_cash_reservations_are_published_sequentially() -> None:
    class SequentialCash(Cash):
        def __init__(self):
            super().__init__()
            self.in_call = False

        async def reserve_committed_batch(self, reservations):
            assert self.in_call is False
            self.in_call = True
            await asyncio.sleep(0)
            self.committed.extend(item.intent_id for item in reservations)
            self.in_call = False

    async def scenario():
        first_item, first_request = batch_pair("intent-1", automation_id="automation-1")
        second_item, second_request = batch_pair("intent-2", automation_id="automation-2")
        cash = SequentialCash()
        dispatcher = Dispatcher()
        service = BatchTradingRuntimeService(
            Repository(),
            dispatcher,
            Tracking(),
            now=lambda: NOW,
            cash=cash,
        )
        task = asyncio.create_task(
            service.run_batch(
                (first_item, second_item),
                (first_request, second_request),
                snapshot_at=NOW,
            )
        )
        await asyncio.sleep(0)
        dispatcher.release.set()
        await task
        return cash

    assert asyncio.run(scenario()).committed == ["intent-1", "intent-2"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("account_id", "other-account"), ("instrument_type", "BOND")],
)
def test_rejects_wrong_request_identity_before_commit(field: str, value: str) -> None:
    item, request = batch_pair()
    repository = Repository()
    service = BatchTradingRuntimeService(repository, Dispatcher(), Tracking(), now=lambda: NOW)

    with pytest.raises(ValueError, match="intent request mapping fields"):
        asyncio.run(
            service.run_batch((item,), (DispatchRequest(**{**request.model_dump(), field: value}),), snapshot_at=NOW)
        )

    assert repository.calls == 0
