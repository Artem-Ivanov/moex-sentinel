import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from grpc import StatusCode
from t_tech.invest import AioRequestError

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from sentinel_contracts.broker_execution import OrderSide
from trading_automaton.adapters.tinvest_broker_session import BrokerSdkSession, _order_state
from trading_automaton.services.account_commission_profile import CommissionRefreshRequest
from trading_automaton.services.order_dispatch import DispatchRequest
from trading_automaton.storage.repository import AccountCommissionProfileKey


class FakeClient:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0
        self.orders = FakeOrders()
        self.sandbox = FakeSandbox()
        self.market_data = FakeMarketData()
        self.operations = FakeOperations()
        self.manager = object()
        self.services = SimpleNamespace(
            orders=self.orders,
            sandbox=self.sandbox,
            market_data=self.market_data,
            operations=self.operations,
            create_market_data_stream=lambda: self.manager,
        )

    async def __aenter__(self):
        self.entered += 1
        return self.services

    async def __aexit__(self, *_args):
        self.exited += 1


def test_reuses_one_async_client_context_for_broker_lifetime() -> None:
    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-target",
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        first = session.services
        second = session.services
        await session.close()
        return client, first, second

    client, first, second = asyncio.run(scenario())

    assert first is second
    assert client.entered == 1
    assert client.exited == 1


class FakeOrders:
    def __init__(self) -> None:
        self.requests = []

    async def get_order_price(self, request):
        self.requests.append(request)

        def money(value):
            return SimpleNamespace(units=int(value), nano=0, currency="rub")

        return SimpleNamespace(
            initial_order_amount=money(1000),
            total_order_amount=money(1001),
            executed_commission=money(1),
            service_commission=money(0),
            deal_commission=money(1),
        )

    async def post_order(self, **values):
        self.posted = values
        return SimpleNamespace(
            order_id="order-1",
            order_request_id=values["order_id"],
            execution_report_status="EXECUTION_REPORT_STATUS_NEW",
            lots_requested=values["quantity"],
            lots_executed=0,
            initial_order_price=SimpleNamespace(units=1000, nano=0, currency="rub"),
            total_order_amount=SimpleNamespace(units=0, nano=0, currency="rub"),
            initial_commission=SimpleNamespace(units=1, nano=0, currency="rub"),
            executed_commission=SimpleNamespace(units=0, nano=0, currency="rub"),
            executed_order_price=SimpleNamespace(units=0, nano=0, currency="rub"),
        )

    async def get_orders(self, *, account_id):
        del account_id
        return SimpleNamespace(
            orders=[
                await self.post_order(
                    instrument_id="instrument",
                    quantity=1,
                    price=None,
                    direction=None,
                    account_id="account",
                    order_type=None,
                    order_id="intent-1",
                    price_type=None,
                )
            ]
        )

    async def get_order_state(self, **values):
        return SimpleNamespace(
            order_id=values["order_id"],
            order_request_id="intent-1",
            execution_report_status="EXECUTION_REPORT_STATUS_FILL",
            lots_requested=1,
            lots_executed=1,
            initial_order_price=SimpleNamespace(units=100, nano=0, currency="rub"),
            total_order_amount=SimpleNamespace(units=100, nano=0, currency="rub"),
            initial_commission=SimpleNamespace(units=1, nano=0, currency="rub"),
            executed_commission=SimpleNamespace(units=1, nano=0, currency="rub"),
            executed_order_price=SimpleNamespace(units=100, nano=0, currency="rub"),
            average_position_price=SimpleNamespace(units=100, nano=0, currency="rub"),
            order_date=NOW,
            stages=(SimpleNamespace(execution_time=NOW),),
        )


class FakeSandbox:
    def __init__(self) -> None:
        self.portfolio_calls = 0
        self.money_calls = 0
        self.fail_portfolio = False
        self.rate_limit_portfolio = False

    async def get_sandbox_portfolio(self, *, account_id):
        self.portfolio_calls += 1
        if self.rate_limit_portfolio:
            raise AioRequestError(StatusCode.RESOURCE_EXHAUSTED, "rate limited", None)
        if self.fail_portfolio:
            raise RuntimeError("portfolio refresh failed")
        position = SimpleNamespace(
            instrument_uid="instrument",
            quantity_lots=SimpleNamespace(units=2, nano=0),
            average_position_price=SimpleNamespace(units=100, nano=0, currency="rub"),
            current_price=SimpleNamespace(units=101, nano=0, currency="rub"),
        )
        return SimpleNamespace(positions=[position])

    async def get_sandbox_positions(self, *, account_id):
        self.money_calls += 1
        del account_id
        return SimpleNamespace(
            money=[
                SimpleNamespace(units=5000, nano=500000000, currency="rub"),
                SimpleNamespace(units=10, nano=0, currency="usd"),
            ]
        )


class FakeMarketData:
    async def get_candles(self, **values):
        self.values = values
        candle = SimpleNamespace(
            open=SimpleNamespace(units=100, nano=0),
            high=SimpleNamespace(units=102, nano=0),
            low=SimpleNamespace(units=99, nano=0),
            close=SimpleNamespace(units=101, nano=0),
            volume=10,
            time=NOW,
            is_complete=True,
        )
        return SimpleNamespace(candles=[candle])


class FakeOperations:
    def __init__(self) -> None:
        self.rate_limited = False

    async def get_operations_by_cursor(self, request):
        if self.rate_limited:
            raise AioRequestError(StatusCode.RESOURCE_EXHAUSTED, "rate limited", None)
        self.request = request
        money = SimpleNamespace(units=1, nano=0, currency="rub")
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    id="operation-1",
                    type="OPERATION_TYPE_BUY",
                    state="OPERATION_STATE_EXECUTED",
                    date=NOW,
                    quantity=1,
                    quantity_done=1,
                    price=money,
                    commission=money,
                )
            ]
        )


NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def test_quotes_commission_through_existing_sdk_context() -> None:
    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-target",
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        request = CommissionRefreshRequest(
            AccountCommissionProfileKey("broker", "account", "SHARE", "RUB"),
            "instrument",
            1,
            Decimal("100"),
        )
        quote = await session.quote(request, "BUY")
        await session.close()
        return client, quote

    client, quote = asyncio.run(scenario())

    assert client.entered == 1
    assert len(client.orders.requests) == 1
    assert quote.order_amount == Decimal("1000")
    assert quote.total_commission == Decimal("1")
    assert quote.deal_commission == Decimal("1")


def test_bootstrap_dispatch_and_stream_reuse_existing_sdk_context() -> None:
    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-target",
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        positions = await session.get_positions("account")
        candles = await session.get_candles(
            "instrument",
            NOW - timedelta(hours=2),
            NOW,
        )
        order = await session.dispatch_limit_order(
            DispatchRequest(
                "intent-1",
                "account",
                "instrument",
                OrderSide.BUY,
                1,
                Decimal("100"),
            )
        )
        manager = session.create_market_data_stream()
        await session.close()
        return client, positions, candles, order, manager

    client, positions, candles, order, manager = asyncio.run(scenario())

    assert client.entered == 1
    assert positions[0].quantity_lots == Decimal("2")
    assert candles[0].close == Decimal("101")
    assert order.status == "ACCEPTED"
    assert order.executed_at is None
    assert client.orders.posted["order_id"] == "intent-1"
    assert manager is client.manager


def test_order_state_prefers_latest_execution_stage_time() -> None:
    earlier = datetime(2026, 8, 7, 11, 59, tzinfo=UTC)
    later = datetime(2026, 8, 7, 12, 1, tzinfo=UTC)
    response = SimpleNamespace(
        order_id="order-1",
        order_request_id="intent-1",
        execution_report_status="EXECUTION_REPORT_STATUS_FILL",
        lots_requested=2,
        lots_executed=2,
        initial_order_price=SimpleNamespace(units=200, nano=0, currency="rub"),
        total_order_amount=SimpleNamespace(units=200, nano=0, currency="rub"),
        initial_commission=SimpleNamespace(units=1, nano=0, currency="rub"),
        executed_commission=SimpleNamespace(units=1, nano=0, currency="rub"),
        executed_order_price=SimpleNamespace(units=100, nano=0, currency="rub"),
        order_date=earlier,
        stages=(
            SimpleNamespace(execution_time=earlier),
            SimpleNamespace(execution_time=later),
        ),
    )

    assert _order_state(response).executed_at == later


def test_order_state_falls_back_to_order_date_without_execution_stages() -> None:
    placed_at = datetime(2026, 8, 7, 11, 59, tzinfo=UTC)
    response = SimpleNamespace(
        order_id="order-1",
        order_request_id="intent-1",
        execution_report_status="EXECUTION_REPORT_STATUS_NEW",
        lots_requested=1,
        lots_executed=0,
        initial_order_price=SimpleNamespace(units=100, nano=0, currency="rub"),
        total_order_amount=SimpleNamespace(units=0, nano=0, currency="rub"),
        initial_commission=SimpleNamespace(units=1, nano=0, currency="rub"),
        executed_commission=SimpleNamespace(units=0, nano=0, currency="rub"),
        executed_order_price=SimpleNamespace(units=0, nano=0, currency="rub"),
        order_date=placed_at,
        stages=(),
    )

    assert _order_state(response).executed_at == placed_at


def test_order_state_normalizes_broker_timestamp_to_milliseconds() -> None:
    broker_time = datetime(2026, 8, 7, 12, 1, 2, 123456, tzinfo=UTC)
    response = SimpleNamespace(
        order_id="order-1",
        order_request_id="intent-1",
        execution_report_status="EXECUTION_REPORT_STATUS_FILL",
        lots_requested=1,
        lots_executed=1,
        initial_order_price=SimpleNamespace(units=100, nano=0, currency="rub"),
        total_order_amount=SimpleNamespace(units=100, nano=0, currency="rub"),
        initial_commission=SimpleNamespace(units=1, nano=0, currency="rub"),
        executed_commission=SimpleNamespace(units=1, nano=0, currency="rub"),
        executed_order_price=SimpleNamespace(units=100, nano=0, currency="rub"),
        order_date=broker_time,
        stages=(SimpleNamespace(execution_time=broker_time),),
    )

    state = _order_state(response)

    assert state.executed_at == broker_time.replace(microsecond=123000)


def test_reads_free_cash_for_requested_currency_from_sandbox_positions() -> None:
    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-target",
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        rub = await session.get_free_cash("account", "RUB")
        eur = await session.get_free_cash("account", "EUR")
        await session.close()
        return rub, eur

    rub, eur = asyncio.run(scenario())

    assert rub == Decimal("5000.5")
    assert eur == Decimal()


def test_reuses_account_snapshot_for_one_minute() -> None:
    async def scenario():
        clock = [10.0]
        client = FakeClient()
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-target",
            client_factory=lambda *_args, **_kwargs: client,
            snapshot_ttl_seconds=60,
            monotonic=lambda: clock[0],
        )
        await session.start()
        await session.get_positions("account")
        await session.get_positions("account")
        await session.inspect_position("account", "instrument")
        await session.get_free_cash("account", "RUB")
        await session.get_free_cash("account", "USD")
        await session.get_free_cash("account", "RUB")
        before_expiry = (client.sandbox.portfolio_calls, client.sandbox.money_calls)
        clock[0] = 70.001
        await session.get_positions("account")
        await session.get_free_cash("account", "RUB")
        after_expiry = (client.sandbox.portfolio_calls, client.sandbox.money_calls)
        await session.close()
        return before_expiry, after_expiry

    before_expiry, after_expiry = asyncio.run(scenario())

    assert before_expiry == (1, 1)
    assert after_expiry == (2, 2)


def test_expired_snapshot_is_not_returned_when_refresh_fails() -> None:
    async def scenario():
        clock = [10.0]
        client = FakeClient()
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-target",
            client_factory=lambda *_args, **_kwargs: client,
            snapshot_ttl_seconds=60,
            monotonic=lambda: clock[0],
        )
        await session.start()
        await session.get_positions("account")
        clock[0] = 70.001
        client.sandbox.fail_portfolio = True
        with pytest.raises(RuntimeError, match="portfolio refresh failed"):
            await session.get_positions("account")
        with pytest.raises(RuntimeError, match="portfolio refresh failed"):
            await session.get_positions("account")
        await session.close()
        return client.sandbox.portfolio_calls

    assert asyncio.run(scenario()) == 3


def test_terminal_order_state_invalidates_account_snapshot() -> None:
    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-target",
            client_factory=lambda *_args, **_kwargs: client,
            snapshot_ttl_seconds=60,
            monotonic=lambda: 10.0,
        )
        await session.start()
        await session.get_positions("account")
        await session.get_free_cash("account", "RUB")
        state = await session.get_order_state("account", "order-1")
        await session.get_positions("account")
        await session.get_free_cash("account", "RUB")
        await session.close()
        return state.status, client.sandbox.portfolio_calls, client.sandbox.money_calls

    assert asyncio.run(scenario()) == ("FILLED", 2, 2)


def test_maps_account_snapshot_rate_limit_to_retryable_broker_error() -> None:
    async def scenario():
        client = FakeClient()
        client.sandbox.rate_limit_portfolio = True
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-target",
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        with pytest.raises(TInvestAdapterError) as captured:
            await session.get_positions("account")
        await session.close()
        return captured.value

    error = asyncio.run(scenario())

    assert error.code == "BROKER_RATE_LIMITED"
    assert error.retryable is True


def test_maps_reconciliation_rate_limit_to_retryable_broker_error() -> None:
    async def scenario():
        client = FakeClient()
        client.operations.rate_limited = True
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-target",
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        with pytest.raises(TInvestAdapterError) as captured:
            await session.inspect_recent_operations("account", "instrument", 20)
        await session.close()
        return captured.value

    error = asyncio.run(scenario())

    assert error.code == "BROKER_RATE_LIMITED"
    assert error.retryable is True


def test_finds_recent_order_by_client_idempotency_key() -> None:
    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession("synthetic-token", "sandbox-target", client_factory=lambda *_args, **_kwargs: client)
        await session.start()
        result = await session.find_by_idempotency_key("account", "intent-1")
        await session.close()
        return result

    result = asyncio.run(scenario())

    assert result is not None
    assert result.idempotency_key == "intent-1"


def test_inspects_position_and_recent_operations_after_dispatch_failure() -> None:
    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession("synthetic-token", "sandbox-target", client_factory=lambda *_args, **_kwargs: client)
        await session.start()
        position = await session.inspect_position("account", "instrument")
        operations = await session.inspect_recent_operations("account", "instrument", 20)
        await session.close()
        return client, position, operations

    client, position, operations = asyncio.run(scenario())

    assert position == {
        "quantity_lots": "2",
        "average_price": "100",
        "current_price": "101",
        "currency": "RUB",
    }
    assert operations[0]["state"] == "OPERATION_STATE_EXECUTED"
    assert operations[0]["operation_id"] == "operation-1"
    assert operations[0]["currency"] == "RUB"
    assert client.operations.request.limit == 20


@pytest.mark.parametrize(
    ("operation", "status", "details", "retryable"),
    [
        ("positions", StatusCode.UNKNOWN, "Stream removed (Unwrap failed (TSI_DATA_CORRUPTED))", True),
        ("cash", StatusCode.UNKNOWN, "Stream removed (Unwrap failed (TSI_DATA_CORRUPTED))", True),
        ("positions", StatusCode.UNKNOWN, "  Stream removed (Unwrap failed (TSI_DATA_CORRUPTED))\n", True),
        ("cash", StatusCode.UNKNOWN, "Stream removed", False),
        ("positions", StatusCode.UNKNOWN, "Stream removed (Unwrap failed (TSI_INTERNAL_ERROR))", False),
        ("cash", StatusCode.UNKNOWN, "Stream removed (Unwrap failed (TSI_DATA_CORRUPTED)): private-token", False),
        ("cash", StatusCode.UNKNOWN, "prefix Stream removed (Unwrap failed (TSI_DATA_CORRUPTED))", False),
        ("positions", StatusCode.UNKNOWN, "different private-token", False),
        ("positions", StatusCode.UNAUTHENTICATED, "Stream removed (Unwrap failed (TSI_DATA_CORRUPTED))", False),
        ("cash", StatusCode.PERMISSION_DENIED, "Stream removed (Unwrap failed (TSI_DATA_CORRUPTED))", False),
        ("submit", StatusCode.UNKNOWN, "Stream removed (Unwrap failed (TSI_DATA_CORRUPTED))", False),
    ],
)
def test_only_snapshot_exact_tls_unwrap_failure_is_retryable(
    operation, status, details, retryable, caplog, monkeypatch
):
    logger = logging.getLogger("trading_automaton.adapters.tinvest_broker_session")
    monkeypatch.setattr(logger, "disabled", False)

    async def scenario():
        client = FakeClient()
        original = AioRequestError(status, details, None)
        calls = []

        async def fail(**kwargs):
            calls.append(kwargs)
            raise original

        client.sandbox.get_sandbox_portfolio = fail
        client.sandbox.get_sandbox_positions = fail
        client.orders.post_order = fail
        session = BrokerSdkSession("synthetic-token", "sandbox-target", client_factory=lambda *_a, **_k: client)
        await session.start()
        try:
            if operation == "positions":
                request = session.get_positions("account")
            elif operation == "cash":
                request = session.get_free_cash("account", "RUB")
            else:
                request = session.dispatch_limit_order(
                    DispatchRequest("intent", "account", "instrument", OrderSide.BUY, 1, Decimal("100"))
                )
            with pytest.raises(TInvestAdapterError) as caught:
                await request
            assert caught.value.retryable is retryable
            assert caught.value.__cause__ is original
            assert "private-token" not in str(caught.value)
            assert len(calls) == 1  # Runtime owns retries; adapter must not resubmit here.
        finally:
            await session.close()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    records = [r for r in caplog.records if getattr(r, "reason_code", None) == "BROKER_SDK_REQUEST_FAILED"]
    assert len(records) == 1
    assert records[0].data["grpc_status"] == status.name
    assert records[0].data["retryable"] is retryable
    assert "private-token" not in str(records[0].__dict__)
    assert records[0].exc_info is None


@pytest.mark.parametrize("operation", ["positions", "cash"])
def test_snapshot_cancellation_is_not_converted_to_broker_error(operation):
    async def scenario():
        client = FakeClient()
        cancellation = asyncio.CancelledError()

        async def cancelled(**kwargs):
            raise cancellation

        client.sandbox.get_sandbox_portfolio = cancelled
        client.sandbox.get_sandbox_positions = cancelled
        session = BrokerSdkSession("synthetic-token", "sandbox-target", client_factory=lambda *_a, **_k: client)
        await session.start()
        try:
            request = (
                session.get_positions("account")
                if operation == "positions"
                else session.get_free_cash("account", "RUB")
            )
            with pytest.raises(asyncio.CancelledError) as caught:
                await request
            assert caught.value is cancellation
        finally:
            await session.close()

    asyncio.run(scenario())
