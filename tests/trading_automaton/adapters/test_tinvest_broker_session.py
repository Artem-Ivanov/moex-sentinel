import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from grpc import StatusCode
from t_tech.invest import AioRequestError
from t_tech.invest.schemas import OrderIdType

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from sentinel_contracts.broker_execution import BrokerConnection, OrderSide
from sentinel_contracts.trading_admission import (
    PreRpcAbortProof,
    SubmitAuthorizationRequest,
    SubmitDenialReason,
    SubmitDenied,
    SubmitPermit,
    submit_request_fingerprint,
)
from trading_automaton.adapters.tinvest_broker_session import BrokerSdkSession, _order_state
from trading_automaton.domain.errors import CoreOperationError
from trading_automaton.services.account_commission_profile import CommissionRefreshRequest
from trading_automaton.services.order_dispatch import DispatchRequest
from trading_automaton.services.trading_admission import SubmitGate
from trading_automaton.storage.repository import AccountCommissionProfileKey

PROD_BROKER_ID = "00000000-0000-4000-8000-000000000001"
PROD_TOKEN = "synthetic-token"
PROD_TARGET = "invest-public-api.tbank.ru:443"
PROD_CONFIGURATION_FINGERPRINT = "a" * 64


def prod_connection(fingerprint=PROD_CONFIGURATION_FINGERPRINT):
    return BrokerConnection(
        broker_id=PROD_BROKER_ID,
        adapter_code="TINVEST_PROD",
        target=PROD_TARGET,
        token=PROD_TOKEN,
        is_test=False,
        environment="PROD",
        access_mode="TRADE",
        account_id="account",
        configuration_fingerprint=fingerprint,
    )


def prod_dispatch_request(configuration_fingerprint=PROD_CONFIGURATION_FINGERPRINT):
    deadline = datetime(2099, 1, 1, tzinfo=UTC)
    return DispatchRequest(
        idempotency_key="00000000-0000-4000-8000-000000000003",
        account_id="account",
        instrument_id="uid-1",
        side=OrderSide.BUY,
        quantity_lots=1,
        limit_price=Decimal("100"),
        lot_size=10,
        reservation_currency="RUB",
        broker_id=PROD_BROKER_ID,
        automation_id="00000000-0000-4000-8000-000000000004",
        market_valid_until=deadline,
        submit_deadline=deadline,
        admission_runtime_instance_id="00000000-0000-4000-8000-000000000002",
        admission_generation=1,
        admission_revision=1,
        configuration_fingerprint=configuration_fingerprint,
    )


def prod_request_fingerprint(request):
    return submit_request_fingerprint(
        SubmitAuthorizationRequest(
            broker_id=UUID(request.broker_id),
            account_id=request.account_id,
            automation_id=UUID(request.automation_id),
            idempotency_key=UUID(request.idempotency_key),
            generation=request.admission_generation,
            revision=request.admission_revision,
            configuration_fingerprint=request.configuration_fingerprint,
            instrument_uid=request.instrument_id,
            lot_size=request.lot_size,
            currency=request.reservation_currency,
            side=request.side.value,
            quantity_lots=request.quantity_lots,
            limit_price=request.limit_price,
            market_valid_until=request.market_valid_until,
            submit_deadline=request.submit_deadline,
        )
    )


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
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            access_mode="TRADE",
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
        self.cancel_requests = []

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

    async def cancel_order(self, **values):
        self.cancel_requests.append(values)
        return SimpleNamespace(time=NOW)


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
            instrument_type="INSTRUMENT_TYPE_SHARE",
            quantity_lots=SimpleNamespace(units=2, nano=0),
            average_position_price=SimpleNamespace(units=100, nano=0, currency="rub"),
            current_price=SimpleNamespace(units=101, nano=0, currency="rub"),
        )
        currency = SimpleNamespace(
            instrument_uid="rub-currency",
            instrument_type="INSTRUMENT_TYPE_CURRENCY",
            quantity_lots=SimpleNamespace(units=5000, nano=0),
            average_position_price=SimpleNamespace(units=1, nano=0, currency="rub"),
            current_price=SimpleNamespace(units=1, nano=0, currency="rub"),
        )
        return SimpleNamespace(positions=[position, currency])

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
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            access_mode="TRADE",
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


def test_positions_dispatch_and_quotes_reuse_existing_sdk_context() -> None:
    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            access_mode="TRADE",
        )
        await session.start()
        positions = await session.get_positions("account")
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
        await session.close()
        return client, positions, order

    client, positions, order = asyncio.run(scenario())

    assert client.entered == 1
    assert positions[0].quantity_lots == Decimal("2")
    assert len(positions) == 1
    assert order.status == "ACCEPTED"
    assert order.executed_at is None
    assert client.orders.posted["order_id"] == "intent-1"


def test_prod_submit_denial_never_enters_sdk_post():
    class Admission:
        async def authorize_submit(self, request):
            return SubmitGate(
                denial=SubmitDenied(reason=SubmitDenialReason.DISARMED, outcome_uncertain=False),
                authorization_elapsed_ms=25,
            )

    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            PROD_TOKEN,
            PROD_TARGET,
            environment="PROD",
            access_mode="TRADE",
            account_id="account",
            broker_connection=prod_connection(),
            submit_admission=Admission(),
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        state = await session.dispatch_limit_order(prod_dispatch_request())
        await session.close()
        return client, state

    client, state = asyncio.run(scenario())
    assert state.status == "CANCELLED"
    assert state.executed_lots == 0
    assert state.pre_rpc_abort_proof is None
    assert state.submit_denial_reason is SubmitDenialReason.DISARMED
    assert state.submit_authorization_elapsed_ms == 25
    assert not hasattr(client.orders, "posted")


def test_prod_successful_post_records_bounded_authorization_and_remaining_permit_timing():
    request = prod_dispatch_request()
    permit = SubmitPermit(
        permit_id=uuid4(),
        runtime_instance_id=UUID(request.admission_runtime_instance_id),
        idempotency_key=UUID(request.idempotency_key),
        generation=request.admission_generation,
        revision=request.admission_revision,
        configuration_fingerprint=PROD_CONFIGURATION_FINGERPRINT,
        request_fingerprint=prod_request_fingerprint(request),
        issued_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
        expires_at=datetime(2026, 8, 7, 12, 0, 1, tzinfo=UTC),
        server_response_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
        risk_snapshot_id=uuid4(),
    )

    class Admission:
        async def authorize_submit(self, authorization):
            return SubmitGate(
                permit=permit,
                expires_monotonic=10.9,
                authorization_elapsed_ms=60,
                permit_remaining_ms=900,
            )

    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            PROD_TOKEN,
            PROD_TARGET,
            environment="PROD",
            access_mode="TRADE",
            account_id="account",
            broker_connection=prod_connection(),
            submit_admission=Admission(),
            monotonic=lambda: 10.0,
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        state = await session.dispatch_limit_order(request)
        await session.close()
        return client, state

    client, state = asyncio.run(scenario())
    assert client.orders.posted["order_id"] == request.idempotency_key
    assert state.submit_authorization_elapsed_ms == 60
    assert state.permit_remaining_ms == 900


def test_prod_already_consumed_submit_is_uncertain_and_never_fake_cancelled():
    class Admission:
        async def authorize_submit(self, request):
            return SubmitGate(denial=SubmitDenied(reason=SubmitDenialReason.ALREADY_CONSUMED, outcome_uncertain=True))

    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            PROD_TOKEN,
            PROD_TARGET,
            environment="PROD",
            access_mode="TRADE",
            account_id="account",
            broker_connection=prod_connection(),
            submit_admission=Admission(),
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        with pytest.raises(CoreOperationError) as error:
            await session.dispatch_limit_order(prod_dispatch_request())
        await session.close()
        return client, error.value

    client, error = asyncio.run(scenario())

    assert error.retryable is True
    assert not hasattr(client.orders, "posted")


def test_prod_sdk_boundary_rejects_request_fingerprint_different_from_session_binding():
    class Admission:
        async def authorize_submit(self, request):
            return SubmitGate(permit=permit, expires_monotonic=100.0)

    permit = SubmitPermit(
        permit_id=uuid4(),
        runtime_instance_id=UUID("00000000-0000-4000-8000-000000000002"),
        idempotency_key=UUID("00000000-0000-4000-8000-000000000003"),
        generation=1,
        revision=1,
        configuration_fingerprint="a" * 64,
        request_fingerprint="b" * 64,
        issued_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
        expires_at=datetime(2026, 8, 7, 12, 0, 1, tzinfo=UTC),
        server_response_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
        risk_snapshot_id=uuid4(),
    )

    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            PROD_TOKEN,
            PROD_TARGET,
            environment="PROD",
            access_mode="TRADE",
            account_id="account",
            broker_connection=prod_connection(),
            submit_admission=Admission(),
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        try:
            await session.dispatch_limit_order(prod_dispatch_request(configuration_fingerprint="c" * 64))
        finally:
            await session.close()
        return client

    with pytest.raises(CoreOperationError):
        asyncio.run(scenario())


def test_prod_sdk_permit_fingerprint_binds_actual_lot_size_before_sdk_post():
    request = prod_dispatch_request()
    permit = SubmitPermit(
        permit_id=uuid4(),
        runtime_instance_id=UUID("00000000-0000-4000-8000-000000000002"),
        idempotency_key=UUID(request.idempotency_key),
        generation=1,
        revision=1,
        configuration_fingerprint=PROD_CONFIGURATION_FINGERPRINT,
        request_fingerprint=prod_request_fingerprint(request),
        issued_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
        expires_at=datetime(2026, 8, 7, 12, 0, 1, tzinfo=UTC),
        server_response_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
        risk_snapshot_id=uuid4(),
    )
    observed = []
    clients = []

    class Admission:
        async def authorize_submit(self, authorization):
            observed.append(authorization)
            return SubmitGate(permit=permit, expires_monotonic=100.0)

    async def scenario():
        client = FakeClient()
        clients.append(client)
        session = BrokerSdkSession(
            PROD_TOKEN,
            PROD_TARGET,
            environment="PROD",
            access_mode="TRADE",
            account_id="account",
            broker_connection=prod_connection(),
            submit_admission=Admission(),
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        try:
            await session.dispatch_limit_order(request.model_copy(update={"lot_size": 1}))
        finally:
            await session.close()
        return client

    with pytest.raises(CoreOperationError):
        asyncio.run(scenario())

    assert observed[0].lot_size == 1
    assert observed[0].reservation_currency == "RUB"
    assert not hasattr(clients[0].orders, "posted")


def test_accepted_permit_expiry_before_sdk_entry_emits_zero_fill_proof():
    class Admission:
        async def authorize_submit(self, request):
            return SubmitGate(permit=permit, expires_monotonic=1.0)

        def abort_proof(self, request, granted, reason):
            return PreRpcAbortProof(
                permit_id=granted.permit_id,
                idempotency_key=granted.idempotency_key,
                request_fingerprint=granted.request_fingerprint,
                runtime_instance_id=granted.runtime_instance_id,
                generation=granted.generation,
                configuration_fingerprint=granted.configuration_fingerprint,
                aborted_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
                reason=reason,
            )

    request = prod_dispatch_request()
    permit = SubmitPermit(
        permit_id=uuid4(),
        runtime_instance_id=UUID("00000000-0000-4000-8000-000000000002"),
        idempotency_key=UUID("00000000-0000-4000-8000-000000000003"),
        generation=1,
        revision=1,
        configuration_fingerprint="a" * 64,
        request_fingerprint=prod_request_fingerprint(request),
        issued_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
        expires_at=datetime(2026, 8, 7, 12, 0, 1, tzinfo=UTC),
        server_response_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
        risk_snapshot_id=uuid4(),
    )

    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            PROD_TOKEN,
            PROD_TARGET,
            environment="PROD",
            access_mode="TRADE",
            account_id="account",
            broker_connection=prod_connection(),
            submit_admission=Admission(),
            monotonic=lambda: 2.0,
            now=lambda: datetime(2026, 8, 7, 12, tzinfo=UTC),
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        state = await session.dispatch_limit_order(request)
        await session.close()
        return client, state

    client, state = asyncio.run(scenario())
    assert state.status == "CANCELLED"
    assert state.requested_amount == Decimal("1000")
    assert state.pre_rpc_abort_proof.reason == "PERMIT_EXPIRED_BEFORE_POST"
    assert state.pre_rpc_abort_proof.idempotency_key == permit.idempotency_key
    assert not hasattr(client.orders, "posted")


def test_permit_expiring_before_scheduled_sdk_entry_aborts_without_post():
    base_time = datetime(2026, 8, 7, 12, tzinfo=UTC)
    expires_at = base_time + timedelta(seconds=1)
    market_expiry = base_time + timedelta(minutes=1)
    request = prod_dispatch_request().model_copy(
        update={"market_valid_until": market_expiry, "submit_deadline": market_expiry}
    )
    permit = SubmitPermit(
        permit_id=uuid4(),
        runtime_instance_id=UUID("00000000-0000-4000-8000-000000000002"),
        idempotency_key=UUID("00000000-0000-4000-8000-000000000003"),
        generation=1,
        revision=1,
        configuration_fingerprint="a" * 64,
        request_fingerprint=prod_request_fingerprint(request),
        issued_at=base_time,
        expires_at=expires_at,
        server_response_at=base_time,
        risk_snapshot_id=uuid4(),
    )
    clock = [0.0]
    observed_now = [base_time]

    class Admission:
        async def authorize_submit(self, _request):
            return SubmitGate(permit=permit, expires_monotonic=1.0)

        def abort_proof(self, _request, granted, reason):
            return PreRpcAbortProof(
                permit_id=granted.permit_id,
                idempotency_key=granted.idempotency_key,
                request_fingerprint=granted.request_fingerprint,
                runtime_instance_id=granted.runtime_instance_id,
                generation=granted.generation,
                configuration_fingerprint=granted.configuration_fingerprint,
                aborted_at=observed_now[0],
                reason=reason,
            )

    async def scenario():
        client = FakeClient()
        post_entries = []
        original_post = client.orders.post_order

        async def record_post(**values):
            post_entries.append(values)
            return await original_post(**values)

        client.orders.post_order = record_post
        session = BrokerSdkSession(
            PROD_TOKEN,
            PROD_TARGET,
            environment="PROD",
            access_mode="TRADE",
            account_id="account",
            broker_connection=prod_connection(),
            submit_admission=Admission(),
            client_factory=lambda *_args, **_kwargs: client,
            monotonic=lambda: clock[0],
            now=lambda: observed_now[0],
        )
        await session.start()

        def expire_permit():
            clock[0] = 2.0
            observed_now[0] = base_time + timedelta(seconds=2)

        asyncio.get_running_loop().call_soon(expire_permit)
        state = await session.dispatch_limit_order(request)
        await session.close()
        return client, state, post_entries

    client, state, post_entries = asyncio.run(scenario())

    assert state.status == "CANCELLED"
    assert state.pre_rpc_abort_proof is not None
    assert state.pre_rpc_abort_proof.reason == "PERMIT_EXPIRED_BEFORE_POST"
    assert post_entries == []
    assert not hasattr(client.orders, "posted")


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


def test_unknown_sdk_execution_status_maps_to_uncertain():
    money = SimpleNamespace(units=100, nano=0, currency="rub")
    response = SimpleNamespace(
        order_id="order-1",
        order_request_id="intent-1",
        execution_report_status="EXECUTION_REPORT_STATUS_UNSPECIFIED",
        lots_requested=1,
        lots_executed=0,
        initial_order_price=money,
        total_order_amount=SimpleNamespace(units=0, nano=0, currency="rub"),
        initial_commission=SimpleNamespace(units=0, nano=0, currency="rub"),
        executed_commission=SimpleNamespace(units=0, nano=0, currency="rub"),
        executed_order_price=SimpleNamespace(units=0, nano=0, currency="rub"),
    )

    assert _order_state(response).status == "UNCERTAIN"


def test_prod_recovery_looks_up_exact_request_id_without_listing_orders():
    async def scenario():
        client = FakeClient()
        requests = []

        async def get_order_state(**values):
            requests.append(values)
            return await FakeOrders().get_order_state(**values)

        async def unexpected_get_orders(**_values):
            pytest.fail("PROD recovery must query the attributable request key directly")

        client.orders.get_order_state = get_order_state
        client.orders.get_orders = unexpected_get_orders
        session = BrokerSdkSession(
            "synthetic-token",
            "invest-public-api.tbank.ru:443",
            environment="PROD",
            account_id="account",
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        state = await session.find_by_idempotency_key("account", "intent-1")
        await session.close()
        return state, requests

    state, requests = asyncio.run(scenario())
    assert state.status == "FILLED"
    assert requests[0]["order_id"] == "intent-1"
    assert requests[0]["order_id_type"] is OrderIdType.ORDER_ID_TYPE_REQUEST


@pytest.mark.parametrize("status", [StatusCode.NOT_FOUND, StatusCode.UNIMPLEMENTED])
def test_prod_request_key_lookup_treats_sdk_absence_as_unknown(status):
    async def scenario():
        client = FakeClient()

        async def missing(**_values):
            raise AioRequestError(status, "request key not visible", None)

        client.orders.get_order_state = missing
        session = BrokerSdkSession(
            "synthetic-token",
            "invest-public-api.tbank.ru:443",
            environment="PROD",
            account_id="account",
            client_factory=lambda *_args, **_kwargs: client,
        )
        await session.start()
        try:
            return await session.find_by_idempotency_key("account", "intent-1")
        finally:
            await session.close()

    assert asyncio.run(scenario()) is None


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
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            access_mode="TRADE",
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
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            snapshot_ttl_seconds=60,
            monotonic=lambda: clock[0],
            access_mode="TRADE",
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
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            snapshot_ttl_seconds=60,
            monotonic=lambda: clock[0],
            access_mode="TRADE",
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
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            snapshot_ttl_seconds=60,
            monotonic=lambda: 10.0,
            access_mode="TRADE",
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


def test_never_completing_sdk_status_rpc_times_out_and_session_closes() -> None:
    async def scenario():
        client = FakeClient()
        entered = asyncio.Event()

        async def never_returns(**_values):
            entered.set()
            await asyncio.Event().wait()

        client.orders.get_order_state = never_returns
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            request_timeout_seconds=0.01,
        )
        await session.start()
        with pytest.raises(TInvestAdapterError, match="timed out"):
            await asyncio.wait_for(session.get_order_state("account", "order-1"), timeout=0.2)
        assert entered.is_set()
        await asyncio.wait_for(session.close(), timeout=0.2)
        return client

    assert asyncio.run(scenario()).exited == 1


def test_cancellation_ignoring_sdk_calls_have_a_pending_call_cap() -> None:
    async def scenario():
        client = FakeClient()
        release = asyncio.Event()
        calls = []

        async def ignores_cancellation(**_values):
            calls.append(None)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise

        client.orders.get_order_state = ignores_cancellation
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            request_timeout_seconds=0.01,
        )
        await session.start()
        for _attempt in range(2):
            with pytest.raises(TInvestAdapterError, match="timed out"):
                await session.get_order_state("account", "order-1")
        with pytest.raises(TInvestAdapterError, match="still pending"):
            await session.get_order_state("account", "order-1")
        await asyncio.wait_for(session.close(), timeout=0.2)
        release.set()
        await asyncio.sleep(0)
        return len(calls), client.exited

    assert asyncio.run(scenario()) == (2, 1)


def test_cancel_order_uses_scoped_exchange_id_and_requires_trade_mode():
    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            access_mode="TRADE",
            account_id="account",
        )
        await session.start()
        await session.cancel_order("account", "order-42")
        await session.close()
        return client.orders.cancel_requests

    assert asyncio.run(scenario()) == [{"account_id": "account", "order_id": "order-42"}]


def test_cancel_order_rejects_account_mismatch_before_sdk_call():
    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            access_mode="TRADE",
            account_id="account",
        )
        await session.start()
        with pytest.raises(ValueError, match="Broker account mismatch"):
            await session.cancel_order("other-account", "order-42")
        await session.close()
        return client.orders.cancel_requests

    assert asyncio.run(scenario()) == []


def test_cancel_order_rejects_read_only_before_sdk_call():
    async def scenario():
        client = FakeClient()
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            access_mode="READ_ONLY",
            account_id="account",
        )
        await session.start()
        with pytest.raises(ValueError, match="READ_ONLY forbids broker mutation"):
            await session.cancel_order("account", "order-42")
        await session.close()
        return client.orders.cancel_requests

    assert asyncio.run(scenario()) == []


def test_maps_account_snapshot_rate_limit_to_retryable_broker_error() -> None:
    async def scenario():
        client = FakeClient()
        client.sandbox.rate_limit_portfolio = True
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            access_mode="TRADE",
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
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            access_mode="TRADE",
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
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            access_mode="TRADE",
        )
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
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_args, **_kwargs: client,
            access_mode="TRADE",
        )
        await session.start()
        position = await session.inspect_position("account", "instrument")
        operations = await session.inspect_recent_operations("account", "instrument", 20)
        await session.close()
        return client, position, operations

    client, position, operations = asyncio.run(scenario())

    assert position.quantity_lots == Decimal(2)
    assert position.average_price == Decimal(100)
    assert position.current_price == Decimal(101)
    assert position.currency == "RUB"
    assert operations[0].executed is True
    assert operations[0].operation_id == "operation-1"
    assert operations[0].currency == "RUB"
    assert operations[0].quantity_units == Decimal(1)
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
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_a, **_k: client,
            access_mode="TRADE",
        )
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
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *_a, **_k: client,
            access_mode="TRADE",
        )
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


@pytest.mark.parametrize(
    ("vendor_type", "vendor_state", "side", "executed"),
    [
        ("OPERATION_TYPE_BUY", "OPERATION_STATE_EXECUTED", OrderSide.BUY, True),
        ("OPERATION_TYPE_SELL", "OPERATION_STATE_EXECUTED", OrderSide.SELL, True),
        ("OPERATION_TYPE_BUY", "OPERATION_STATE_CANCELED", OrderSide.BUY, False),
        ("OPERATION_TYPE_BROKER_FEE", "OPERATION_STATE_EXECUTED", None, True),
    ],
)
def test_sdk_maps_vendor_recovery_observation_to_neutral_units(vendor_type, vendor_state, side, executed):
    async def scenario():
        client = FakeClient()

        class Operations(FakeOperations):
            async def get_operations_by_cursor(self, request):
                response = await super().get_operations_by_cursor(request)
                item = response.items[0]
                item.type, item.state = vendor_type, vendor_state
                item.quantity_done = 13
                item.date = NOW.replace(microsecond=123456)
                return response

        client.services.operations = Operations()
        session = BrokerSdkSession(
            "synthetic-token",
            "sandbox-invest-public-api.tbank.ru:443",
            client_factory=lambda *args, **kwargs: client,
            access_mode="TRADE",
        )
        await session.start()
        try:
            values = await session.inspect_recent_operations("account", "instrument", 20)
        finally:
            await session.close()
        assert len(values) == 1
        observed = values[0]
        assert observed.side == side
        assert observed.executed is executed
        assert observed.quantity_units == Decimal(13)
        assert observed.occurred_at == NOW.replace(microsecond=123000)
        assert observed.price == observed.commission == Decimal(1)

    asyncio.run(scenario())
