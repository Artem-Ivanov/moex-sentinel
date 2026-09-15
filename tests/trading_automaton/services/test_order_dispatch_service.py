import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from sentinel_contracts.broker_execution import BrokerOrderState, OrderSide
from sentinel_contracts.business_audit import BusinessAuditStage
from trading_automaton.services.order_dispatch import (
    DispatchRequest,
    OrderDispatchService,
)

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


class Broker:
    def __init__(
        self,
        release: asyncio.Event,
        *,
        error: Exception | None = None,
        reconciled: BrokerOrderState | None = None,
    ) -> None:
        self.release = release
        self.error = error
        self.called = asyncio.Event()
        self.reconciled = reconciled
        self.reconciliation_calls = []
        self.position_calls = []
        self.operation_calls = []

    async def dispatch_limit_order(self, request):
        self.called.set()
        await self.release.wait()
        if self.error:
            raise self.error
        return BrokerOrderState(
            "order-1",
            request.idempotency_key,
            "ACCEPTED",
            1,
            0,
            Decimal("1000"),
            Decimal(),
            Decimal("1"),
            Decimal(),
            "RUB",
        )

    async def find_by_idempotency_key(self, account_id, idempotency_key):
        self.reconciliation_calls.append((account_id, idempotency_key))
        return self.reconciled

    async def inspect_position(self, account_id, instrument_id):
        self.position_calls.append((account_id, instrument_id))
        return {"quantity_lots": "2", "average_price": "100"}

    async def inspect_recent_operations(self, account_id, instrument_id, limit):
        self.operation_calls.append((account_id, instrument_id, limit))
        return ({"type": "BUY", "state": "EXECUTED", "quantity": "1"},)


class Repository:
    def __init__(self) -> None:
        self.updates = []

    def update_intent(self, idempotency_key, **values):
        self.updates.append((idempotency_key, values))


def request() -> DispatchRequest:
    return DispatchRequest("intent-1", "account-1", "instrument-1", OrderSide.BUY, 1, Decimal("100"))


@pytest.mark.parametrize("delay_in_audit", [False, True])
def test_expired_market_cancels_persisted_intent_without_broker_call(delay_in_audit):
    async def scenario():
        clock = [NOW if delay_in_audit else NOW + timedelta(seconds=3)]

        class Audit:
            def record_order_stage(self, **values):
                clock[0] = NOW + timedelta(seconds=3)

        release = asyncio.Event()
        release.set()
        broker = Broker(release)
        repository = Repository()
        service = OrderDispatchService(repository, broker, now=lambda: clock[0], audit=Audit())
        started = asyncio.get_running_loop().create_future()
        value = request().model_copy(update={"market_valid_until": NOW + timedelta(seconds=2), "process_id": "process"})
        result = await service.dispatch(value, started)
        assert result.status == "CANCELLED"
        assert result.executed_lots == 0
        assert not broker.called.is_set()
        assert not broker.reconciliation_calls
        assert repository.updates[-1][1]["state"] == "CANCELLED"
        assert started.done()

    asyncio.run(scenario())


def test_marks_sdk_start_before_waiting_for_broker_response() -> None:
    async def scenario():
        release = asyncio.Event()
        broker = Broker(release)
        started = asyncio.get_running_loop().create_future()
        repository = Repository()
        service = OrderDispatchService(repository, broker, now=lambda: NOW)
        task = asyncio.create_task(service.dispatch(request(), started))
        await broker.called.wait()
        assert started.result() == NOW
        assert repository.updates == [
            (
                "intent-1",
                {
                    "state": "SUBMITTING",
                    "occurred_at": NOW,
                    "dispatch_started_at": NOW,
                    "process_id": None,
                },
            )
        ]
        assert not task.done()
        release.set()
        result = await task
        return result, repository

    result, repository = asyncio.run(scenario())

    assert result.status == "ACCEPTED"
    assert repository.updates[-1] == (
        "intent-1",
        {
            "state": "ACCEPTED",
            "occurred_at": NOW,
            "broker_order_id": "order-1",
            "broker_responded_at": NOW,
            "process_id": None,
        },
    )


def test_ambiguous_sdk_failure_marks_intent_uncertain() -> None:
    async def scenario():
        release = asyncio.Event()
        release.set()
        repository = Repository()
        broker = Broker(release, error=TimeoutError("synthetic timeout"))
        started = asyncio.get_running_loop().create_future()
        service = OrderDispatchService(repository, broker, now=lambda: NOW)
        with pytest.raises(TimeoutError):
            await service.dispatch(request(), started)
        return repository

    repository = asyncio.run(scenario())

    assert repository.updates[-1] == (
        "intent-1",
        {"state": "UNCERTAIN", "occurred_at": NOW, "process_id": None},
    )


def test_sdk_failure_recovers_order_by_idempotency_key() -> None:
    async def scenario():
        release = asyncio.Event()
        release.set()
        reconciled = BrokerOrderState(
            "order-1",
            "intent-1",
            "FILLED",
            1,
            1,
            Decimal("100"),
            Decimal("100"),
            Decimal("1"),
            Decimal("1"),
            "RUB",
            Decimal("100"),
            NOW,
        )
        repository = Repository()
        broker = Broker(release, error=TimeoutError("synthetic timeout"), reconciled=reconciled)
        service = OrderDispatchService(repository, broker, now=lambda: NOW)
        started = asyncio.get_running_loop().create_future()
        result = await service.dispatch(request(), started)
        return result, repository, broker

    result, repository, broker = asyncio.run(scenario())

    assert result.status == "FILLED"
    assert repository.updates[-1] == (
        "intent-1",
        {
            "state": "FILLED",
            "occurred_at": NOW,
            "broker_order_id": "order-1",
            "broker_responded_at": NOW,
            "process_id": None,
        },
    )
    assert broker.reconciliation_calls == [("account-1", "intent-1")]
    assert broker.position_calls == [("account-1", "instrument-1")]
    assert broker.operation_calls == [("account-1", "instrument-1", 20)]


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_details"),
    [
        pytest.param(
            TInvestAdapterError("BROKER_UNAVAILABLE", "Safe broker message", retryable=True),
            "BROKER_UNAVAILABLE",
            "Safe broker message",
            id="typed-broker-error",
        ),
        pytest.param(
            RuntimeError("SECRET_UNEXPECTED_DISPATCH_DETAIL"),
            None,
            "Unexpected dispatch failure",
            id="unexpected-error",
        ),
    ],
)
def test_ambiguous_dispatch_audit_preserves_only_safe_error_details(failure, expected_code, expected_details):
    async def scenario():
        release = asyncio.Event()
        release.set()
        broker = Broker(release, error=failure)
        records = []

        class OrderedRepository(Repository):
            def update_intent(self, idempotency_key, **values):
                if values["state"] == "UNCERTAIN":
                    assert broker.reconciliation_calls == [("account-1", "intent-1")]
                    assert broker.position_calls == [("account-1", "instrument-1")]
                    assert broker.operation_calls == [("account-1", "instrument-1", 20)]
                super().update_intent(idempotency_key, **values)

        class Audit:
            def record_order_stage(self, **values):
                records.append(values)

        repository = OrderedRepository()
        service = OrderDispatchService(repository, broker, now=lambda: NOW, audit=Audit())
        value = request().model_copy(update={"process_id": "process"})
        started = asyncio.get_running_loop().create_future()

        with pytest.raises(type(failure)) as caught:
            await service.dispatch(value, started)

        assert caught.value is failure
        assert started.result() == NOW
        assert [update[1]["state"] for update in repository.updates] == ["SUBMITTING", "UNCERTAIN"]
        assert [record["stage"] for record in records] == [
            BusinessAuditStage.BROKER_ORDER_DISPATCH_STARTED,
            BusinessAuditStage.TRADING_STEP_FAILED,
        ]
        failed = records[-1]
        assert failed["broker_order_state"] == "UNCERTAIN"
        assert failed["exception_type"] == type(failure).__name__
        assert failed["broker_error_code"] == expected_code
        assert failed["broker_error_details"] == expected_details
        assert failed["position_snapshot"] == {"quantity_lots": "2", "average_price": "100"}
        assert failed["recent_operations"] == [{"type": "BUY", "state": "EXECUTED", "quantity": "1"}]
        assert "SECRET_UNEXPECTED_DISPATCH_DETAIL" not in repr(records)

    asyncio.run(scenario())
