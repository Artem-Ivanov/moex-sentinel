"""Read-only T-Invest data mapping for PROD admission."""

import asyncio
import asyncio as asyncio_module
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from grpc import StatusCode
from t_tech.invest.exceptions import AioRequestError
from t_tech.invest.schemas import (
    GetLastPricesResponse,
    GetOperationsByCursorResponse,
    GetOrderBookResponse,
    GetOrderPriceResponse,
    GetOrdersResponse,
    GetStopOrdersResponse,
    Instrument,
    InstrumentIdType,
    InstrumentResponse,
    InstrumentType,
    LastPrice,
    MoneyValue,
    OperationItem,
    OperationState,
    OperationType,
    Order,
    OrderDirection,
    OrderExecutionReportStatus,
    OrderIdType,
    OrderState,
    PortfolioPosition,
    PortfolioResponse,
    PositionsResponse,
    PositionsSecurities,
    PriceType,
    Quotation,
    StopOrder,
    StopOrderStatusOption,
)

from moex_sentinel.adapters.tinvest.admission_risk import TInvestAdmissionRiskAdapter
from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from sentinel_contracts.broker_execution import BrokerConnection, OrderSide

ACCOUNT = "account-1"
UID = "uid-1"
REQUEST_ID = "request-1"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def quote(value: str) -> Quotation:
    amount = Decimal(value)
    units = int(amount)
    return Quotation(units=units, nano=int((amount - units) * Decimal("1000000000")))


def money(value: str, currency: str = "rub") -> MoneyValue:
    quoted = quote(value)
    return MoneyValue(currency=currency, units=quoted.units, nano=quoted.nano)


def order_book_response(
    *,
    instrument_uid: str = UID,
    timestamp: datetime | None = NOW,
    bids: tuple[Order, ...] | None = None,
    asks: tuple[Order, ...] | None = None,
) -> GetOrderBookResponse:
    timestamp_value = NOW if timestamp is None else timestamp
    return GetOrderBookResponse(
        figi="figi-1",
        depth=20,
        bids=list(
            (Order(price=quote("99.9"), quantity=2), Order(price=quote("100"), quantity=1)) if bids is None else bids
        ),
        asks=list(
            (Order(price=quote("100.1"), quantity=1), Order(price=quote("100.05"), quantity=2))
            if asks is None
            else asks
        ),
        last_price=quote("100"),
        close_price=quote("99"),
        limit_up=quote("120"),
        limit_down=quote("80"),
        last_price_ts=timestamp_value,
        close_price_ts=timestamp_value,
        orderbook_ts=timestamp,
        instrument_uid=instrument_uid,
    )


def raw_sdk_quotation(units: int, nano: int) -> Quotation:
    value = Quotation(units=0, nano=0)
    value.units = units
    value.nano = nano
    return value


def order_state(request_id: str = REQUEST_ID) -> OrderState:
    return OrderState(
        order_id="broker-order-1",
        execution_report_status=OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL,
        lots_requested=3,
        lots_executed=2,
        initial_order_price=money("300"),
        executed_order_price=money("2000"),
        total_order_amount=money("2000.6"),
        average_position_price=money("100"),
        initial_commission=money("0.9"),
        executed_commission=money("0.6"),
        figi="figi-1",
        direction=OrderDirection.ORDER_DIRECTION_BUY,
        initial_security_price=money("300"),
        stages=[],
        service_commission=money("0.2"),
        currency="rub",
        order_date=NOW,
        instrument_uid=UID,
        order_request_id=request_id,
    )


class Services:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.operation_pages: list[GetOperationsByCursorResponse] = [
            GetOperationsByCursorResponse(
                has_next=False,
                next_cursor="",
                items=[
                    OperationItem(
                        cursor="",
                        broker_account_id=ACCOUNT,
                        id="op-1",
                        parent_operation_id="",
                        name="input",
                        date=NOW - timedelta(minutes=1),
                        type=OperationType.OPERATION_TYPE_INPUT,
                        description="",
                        state=OperationState.OPERATION_STATE_EXECUTED,
                        instrument_uid="",
                        figi="",
                        instrument_type="",
                        position_uid="",
                        payment=money("500"),
                        price=money("500"),
                        commission=money("0"),
                        yield_=money("0"),
                        quantity=0,
                        quantity_rest=0,
                        quantity_done=0,
                        cancel_date_time=NOW,
                        cancel_reason="",
                        asset_uid="",
                    )
                ],
            )
        ]
        self.request_states: dict[str, OrderState] = {REQUEST_ID: order_state()}
        self.instrument_kind = InstrumentType.INSTRUMENT_TYPE_SHARE
        self.not_found_requests: set[str] = set()
        self.positions_response = PositionsResponse(
            money=[money("700")],
            blocked=[money("20")],
            securities=[PositionsSecurities(instrument_uid=UID, blocked=1, balance=24, exchange_blocked=False)],
            limits_loading_in_progress=False,
            futures=[],
            options=[],
            account_id=ACCOUNT,
        )
        self.portfolio_response = PortfolioResponse(
            total_amount_portfolio=money("1000"),
            expected_yield=quote("0.1"),
            account_id=ACCOUNT,
            positions=[
                PortfolioPosition(
                    instrument_uid=UID,
                    quantity=quote("25"),
                    quantity_lots=quote("2.5"),
                    current_price=money("100"),
                    average_position_price=money("90"),
                    blocked=False,
                    blocked_lots=quote("0"),
                )
            ],
        )
        self.orders_response = GetOrdersResponse(orders=[order_state()])
        self.last_price_time = NOW
        self.order_book_response = order_book_response()
        self.orders_response.orders[0].execution_report_status = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
        self.stops_response = GetStopOrdersResponse(
            stop_orders=[
                StopOrder(
                    stop_order_id="stop-1",
                    lots_requested=1,
                    figi="figi-1",
                    direction=OrderDirection.ORDER_DIRECTION_SELL,
                    currency="rub",
                    create_date=NOW,
                    activation_date_time=NOW,
                    expiration_time=NOW + timedelta(days=1),
                    price=money("100"),
                    stop_price=money("95"),
                    instrument_uid=UID,
                    status=StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE,
                )
            ]
        )
        self.operations = self
        self.orders = self
        self.stop_orders = self
        self.instruments = self
        self.market_data = self

    async def get_portfolio(self, *, account_id: str):
        self.calls.append(("get_portfolio", account_id))
        return self.portfolio_response

    async def get_positions(self, *, account_id: str):
        self.calls.append(("get_positions", account_id))
        return self.positions_response

    async def get_orders(self, *, account_id: str):
        self.calls.append(("get_orders", account_id))
        return self.orders_response

    async def get_stop_orders(self, *, account_id: str, status):
        self.calls.append(("get_stop_orders", (account_id, status)))
        return self.stops_response

    async def get_instrument_by(self, *, id_type, id):
        self.calls.append(("get_instrument_by", (id_type, id)))
        assert id_type == InstrumentIdType.INSTRUMENT_ID_TYPE_UID
        return InstrumentResponse(
            instrument=Instrument(
                instrument_type="share",
                currency="rub",
                lot=10,
                min_price_increment=quote("0.01"),
                api_trade_available_flag=True,
                uid=UID,
                instrument_kind=self.instrument_kind,
            )
        )

    async def get_last_prices(self, *, instrument_id):
        self.calls.append(("get_last_prices", tuple(instrument_id)))
        return GetLastPricesResponse(
            last_prices=[
                LastPrice(
                    instrument_uid=UID,
                    price=quote("100"),
                    time=self.last_price_time,
                )
            ]
        )

    async def get_order_book(self, *, instrument_id: str, depth: int):
        self.calls.append(("get_order_book", (instrument_id, depth)))
        return self.order_book_response

    async def get_operations_by_cursor(self, request):
        self.calls.append(("get_operations_by_cursor", request))
        return self.operation_pages.pop(0)

    async def get_order_state(self, **request):
        self.calls.append(("get_order_state", request.copy()))
        assert request["order_id_type"] == OrderIdType.ORDER_ID_TYPE_REQUEST
        assert request["price_type"] == PriceType.PRICE_TYPE_CURRENCY
        if request["order_id"] in self.not_found_requests:
            raise AioRequestError(StatusCode.NOT_FOUND, "private SDK detail", ())
        return self.request_states[request["order_id"]]

    async def get_order_price(self, request):
        self.calls.append(("get_order_price", request))
        assert request.direction == OrderDirection.ORDER_DIRECTION_BUY
        assert request.quantity == 3
        assert request.price == quote("100")
        return GetOrderPriceResponse(
            total_order_amount=money("300"),
            initial_order_amount=money("300"),
            lots_requested=3,
            executed_commission=money("0.9"),
            executed_commission_rub=money("0.9"),
            service_commission=money("0.1"),
            deal_commission=money("0.8"),
        )


class ClientContext:
    def __init__(self, services: Services) -> None:
        self.services = services

    async def __aenter__(self):
        return self.services

    async def __aexit__(self, *_args):
        return None


def connection(**changes) -> BrokerConnection:
    values = {
        "broker_id": "broker-1",
        "adapter_code": "TINVEST_PROD",
        "target": "invest-public-api.tbank.ru:443",
        "token": "synthetic-token",
        "is_test": False,
        "environment": "PROD",
        "access_mode": "TRADE",
        "account_id": ACCOUNT,
    }
    values.update(changes)
    return BrokerConnection(**values)


def make_adapter(
    services: Services,
    *,
    times: tuple[datetime, ...] = (
        NOW,
        NOW + timedelta(milliseconds=10),
        NOW + timedelta(milliseconds=20),
    ),
):
    values = iter(times)
    factories: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def factory(*args, **kwargs):
        factories.append((args, kwargs))
        return ClientContext(services)

    return TInvestAdmissionRiskAdapter(client_factory=factory, clock=lambda: next(values)), factories


def test_collect_maps_real_account_risk_and_attributed_request_state() -> None:
    services = Services()
    adapter, factories = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
            request_ids=(REQUEST_ID,),
        )
    )

    assert snapshot.account_id == ACCOUNT
    assert snapshot.equity == Decimal("1000")
    assert snapshot.equity_currency == "RUB"
    assert snapshot.money == Decimal("700")
    assert snapshot.blocked_cash == Decimal("20")
    assert snapshot.money_loaded is True
    assert snapshot.instrument.instrument_type == "SHARE"
    assert snapshot.instrument.lot_size == 10
    assert snapshot.instrument.mark_price == Decimal("100.05")
    assert snapshot.instrument.mark_captured_at == NOW
    assert snapshot.positions[0].quantity_units == Decimal("25")
    assert snapshot.positions[0].blocked_units == Decimal("1")
    assert snapshot.stop_orders[0].broker_order_id == "stop-1"
    assert snapshot.ordinary_orders[0].status == "FILLED"
    assert snapshot.order_states[0].attributable is True
    assert snapshot.order_states[0].requested_lots == 3
    assert snapshot.order_states[0].executed_lots == 2
    assert snapshot.order_states[0].executed_amount == Decimal("2000")
    assert snapshot.order_states[0].executed_commission == Decimal("0.6")
    assert snapshot.operations_complete is True
    assert snapshot.operations[0].payment == Decimal("500")
    assert snapshot.operations_through == snapshot.captured_at
    assert snapshot.unknown_reasons == ()
    assert factories == [(("synthetic-token",), {"target": "invest-public-api.tbank.ru:443"})]
    assert ("get_order_book", (UID, 20)) in services.calls
    assert not any(name == "get_last_prices" for name, _ in services.calls)
    mutation_calls = {"post_order", "cancel_order", "replace_order", "post_stop_order"}
    assert all(name not in mutation_calls for name, _ in services.calls)


def test_collect_marks_each_positive_holding_and_maps_its_own_lot_size() -> None:
    second_uid = "uid-2"
    services = Services()
    services.positions_response.securities.append(
        PositionsSecurities(instrument_uid=second_uid, blocked=0, balance=10, exchange_blocked=False)
    )
    services.portfolio_response.positions.append(
        PortfolioPosition(
            instrument_uid=second_uid,
            quantity=quote("10"),
            quantity_lots=quote("2"),
            current_price=money("40"),
            average_position_price=money("39"),
            blocked=False,
            blocked_lots=quote("0"),
        )
    )
    original_get_instrument = services.get_instrument_by
    original_get_book = services.get_order_book

    async def get_instrument_by(*, id_type, id):
        if id != second_uid:
            return await original_get_instrument(id_type=id_type, id=id)
        services.calls.append(("get_instrument_by", (id_type, id)))
        return InstrumentResponse(
            instrument=Instrument(
                instrument_type="share",
                currency="rub",
                lot=5,
                min_price_increment=quote("0.01"),
                api_trade_available_flag=True,
                uid=second_uid,
                instrument_kind=InstrumentType.INSTRUMENT_TYPE_SHARE,
            )
        )

    async def get_order_book(*, instrument_id: str, depth: int):
        if instrument_id != second_uid:
            return await original_get_book(instrument_id=instrument_id, depth=depth)
        services.calls.append(("get_order_book", (instrument_id, depth)))
        return order_book_response(
            instrument_uid=second_uid,
            bids=(Order(price=quote("39.9"), quantity=2),),
            asks=(Order(price=quote("40.1"), quantity=2),),
        )

    services.get_instrument_by = get_instrument_by
    services.get_order_book = get_order_book
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    positions = {item.instrument_uid: item for item in snapshot.positions}
    assert positions[UID].mark_price == Decimal("100.05")
    assert positions[second_uid].mark_price == Decimal("40.1")
    assert positions[UID].lot_size == 10
    assert positions[second_uid].lot_size == 5
    assert positions[UID].mark_captured_at == NOW
    assert positions[second_uid].mark_captured_at == NOW
    assert ("get_order_book", (second_uid, 20)) in services.calls


def test_collect_marks_snapshot_unknown_for_stale_nonselected_holding() -> None:
    second_uid = "uid-2"
    services = Services()
    services.positions_response.securities.append(
        PositionsSecurities(instrument_uid=second_uid, blocked=0, balance=10, exchange_blocked=False)
    )
    services.portfolio_response.positions.append(
        PortfolioPosition(
            instrument_uid=second_uid,
            quantity=quote("10"),
            quantity_lots=quote("2"),
            current_price=money("40"),
            average_position_price=money("39"),
            blocked=False,
            blocked_lots=quote("0"),
        )
    )
    original_get_instrument = services.get_instrument_by
    original_get_book = services.get_order_book

    async def get_instrument_by(*, id_type, id):
        if id != second_uid:
            return await original_get_instrument(id_type=id_type, id=id)
        return InstrumentResponse(
            instrument=Instrument(
                instrument_type="share",
                currency="rub",
                lot=5,
                min_price_increment=quote("0.01"),
                api_trade_available_flag=True,
                uid=second_uid,
                instrument_kind=InstrumentType.INSTRUMENT_TYPE_SHARE,
            )
        )

    async def get_order_book(*, instrument_id: str, depth: int):
        if instrument_id != second_uid:
            return await original_get_book(instrument_id=instrument_id, depth=depth)
        return order_book_response(
            instrument_uid=second_uid,
            timestamp=NOW - timedelta(seconds=2),
            bids=(Order(price=quote("39.9"), quantity=2),),
            asks=(Order(price=quote("40.1"), quantity=2),),
        )

    services.get_instrument_by = get_instrument_by
    services.get_order_book = get_order_book
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert "POSITION_MARK_STALE" in snapshot.unknown_reasons


def test_collect_normalizes_sdk_etf_to_catalog_fund_type() -> None:
    services = Services()
    services.instrument_kind = InstrumentType.INSTRUMENT_TYPE_ETF
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert snapshot.instrument.instrument_type == "FUND"


def test_currency_portfolio_position_is_not_a_security_position() -> None:
    services = Services()
    services.positions_response = PositionsResponse(
        money=[money("5001.1")],
        blocked=[money("0")],
        securities=[PositionsSecurities(instrument_uid=UID, blocked=0, balance=18, exchange_blocked=False)],
        limits_loading_in_progress=False,
        futures=[],
        options=[],
        account_id=ACCOUNT,
    )
    services.portfolio_response.positions[0] = replace(
        services.portfolio_response.positions[0],
        quantity=quote("18"),
        quantity_lots=quote("18"),
        average_position_price=money("3.1066"),
        current_price=money("2.88"),
        blocked=False,
        blocked_lots=quote("0"),
    )
    cash_position = replace(
        services.portfolio_response.positions[0],
        instrument_type="currency",
        instrument_uid="cash-uid",
        quantity=quote("5001.1"),
        quantity_lots=quote("5001.1"),
        average_position_price=money("1"),
        current_price=money("1"),
        blocked=False,
        blocked_lots=quote("0"),
    )
    assert cash_position.instrument_type == "currency"
    assert cash_position.instrument_uid == "cash-uid"
    services.portfolio_response.positions.append(cash_position)
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert snapshot.money == Decimal("5001.1")
    assert snapshot.blocked_cash == Decimal("0")
    assert snapshot.money_loaded is True
    assert [position.instrument_uid for position in snapshot.positions] == [UID]
    assert snapshot.positions[0].quantity_units == Decimal("18")
    assert snapshot.positions[0].blocked_units == Decimal("0")
    assert snapshot.unknown_reasons == ()


def test_untyped_portfolio_position_without_security_balance_stays_unknown() -> None:
    services = Services()
    services.portfolio_response.positions.append(
        replace(
            services.portfolio_response.positions[0],
            instrument_type="",
            instrument_uid="unmatched-security-uid",
            quantity=quote("1"),
            quantity_lots=quote("1"),
        )
    )
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert "POSITION_QUANTITY_UNKNOWN" in snapshot.unknown_reasons
    assert [position.instrument_uid for position in snapshot.positions] == [UID]


def test_request_id_mismatch_is_explicitly_unattributable() -> None:
    services = Services()
    services.request_states[REQUEST_ID] = order_state("different-request")
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
            request_ids=(REQUEST_ID,),
        )
    )

    state = snapshot.order_states[0]
    assert state.attributable is False
    assert state.unknown_reason == "REQUEST_ID_MISMATCH"


def test_request_not_found_stays_unattributable_and_does_not_expose_sdk_details() -> None:
    services = Services()
    services.not_found_requests.add(REQUEST_ID)
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=None,
            operations_from=NOW - timedelta(days=1),
            request_ids=(REQUEST_ID,),
        )
    )

    state = snapshot.order_states[0]
    assert (state.status, state.attributable, state.unknown_reason) == (
        "UNKNOWN",
        False,
        "REQUEST_NOT_FOUND",
    )
    assert "private SDK detail" not in repr(snapshot)


def test_missing_or_non_rub_cash_is_unknown_instead_of_zero() -> None:
    services = Services()
    services.positions_response = PositionsResponse(
        money=[money("700", "usd")],
        blocked=[money("20")],
        securities=[],
        limits_loading_in_progress=True,
        futures=[],
        options=[],
        account_id=ACCOUNT,
    )
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=None,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert snapshot.money is None
    assert snapshot.blocked_cash == Decimal("20")
    assert snapshot.money_loaded is False
    assert {"MONEY_CURRENCY_UNKNOWN", "POSITIONS_LOADING"} <= set(snapshot.unknown_reasons)


def test_repeated_operation_cursor_marks_history_incomplete_and_deduplicates_same_operation() -> None:
    services = Services()
    operation = services.operation_pages[0].items[0]
    services.operation_pages = [
        GetOperationsByCursorResponse(has_next=True, next_cursor="cursor-1", items=[operation]),
        GetOperationsByCursorResponse(has_next=True, next_cursor="cursor-1", items=[operation]),
    ]
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=None,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert len(snapshot.operations) == 1
    assert snapshot.operations_complete is False
    assert "OPERATIONS_CURSOR_REPEATED" in snapshot.unknown_reasons
    requests = [value for name, value in services.calls if name == "get_operations_by_cursor"]
    assert {request.to for request in requests} == {snapshot.captured_at}


def test_unusable_next_cursor_marks_history_incomplete() -> None:
    services = Services()
    services.operation_pages = [GetOperationsByCursorResponse(has_next=True, next_cursor="", items=[])]
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=None,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert snapshot.operations_complete is False
    assert "OPERATIONS_PAGINATION_INCOMPLETE" in snapshot.unknown_reasons


@pytest.mark.parametrize(
    "operation_type",
    [
        pytest.param(OperationType.OPERATION_TYPE_INPUT, id="input"),
        pytest.param(OperationType.OPERATION_TYPE_INPUT_SWIFT, id="input-swift"),
        pytest.param(OperationType.OPERATION_TYPE_INPUT_ACQUIRING, id="input-acquiring"),
        pytest.param(OperationType.OPERATION_TYPE_INP_MULTI, id="input-multi"),
        pytest.param(OperationType.OPERATION_TYPE_OUTPUT, id="output"),
        pytest.param(OperationType.OPERATION_TYPE_OUTPUT_SWIFT, id="output-swift"),
        pytest.param(OperationType.OPERATION_TYPE_OUTPUT_ACQUIRING, id="output-acquiring"),
        pytest.param(OperationType.OPERATION_TYPE_OUT_MULTI, id="output-multi"),
    ],
)
def test_operation_window_cutoff_is_fixed_after_non_cursor_reads(operation_type) -> None:
    services = Services()
    services.operation_pages[0].items[0].date = NOW + timedelta(milliseconds=50)
    services.operation_pages[0].items[0].type = operation_type
    adapter, _ = make_adapter(
        services,
        times=(NOW, NOW + timedelta(milliseconds=100), NOW + timedelta(milliseconds=200)),
    )

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=None,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert snapshot.captured_at == snapshot.operations_through
    assert snapshot.captured_at == NOW + timedelta(milliseconds=100)
    request = next(value for name, value in services.calls if name == "get_operations_by_cursor")
    assert request.to == snapshot.captured_at
    assert "EXTERNAL_CASH_FLOW_CAPTURE_WINDOW" in snapshot.unknown_reasons


def test_negative_trade_payment_is_not_classified_as_external_cash_flow() -> None:
    services = Services()
    operation = services.operation_pages[0].items[0]
    operation.date = NOW + timedelta(milliseconds=50)
    operation.type = OperationType.OPERATION_TYPE_BUY
    operation.payment = money("-500")
    adapter, _ = make_adapter(
        services,
        times=(NOW, NOW + timedelta(milliseconds=100), NOW + timedelta(milliseconds=200)),
    )

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=None,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert snapshot.operations[0].payment == Decimal("-500")
    assert "EXTERNAL_CASH_FLOW_CAPTURE_WINDOW" not in snapshot.unknown_reasons


def test_slow_cursor_keeps_data_cutoff_and_marks_total_elapsed_unknown() -> None:
    services = Services()
    async_get_operations = services.get_operations_by_cursor

    async def slow_get_operations(request):
        await asyncio_module.sleep(0)
        return await async_get_operations(request)

    services.get_operations_by_cursor = slow_get_operations
    adapter, _ = make_adapter(
        services,
        times=(NOW, NOW + timedelta(milliseconds=100), NOW + timedelta(seconds=2)),
    )

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=None,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert snapshot.captured_at == snapshot.operations_through == NOW + timedelta(milliseconds=100)
    assert snapshot.valid_until == snapshot.captured_at + timedelta(seconds=1)
    assert "SNAPSHOT_WINDOW_EXCEEDED" in snapshot.unknown_reasons


def test_unspecified_order_and_stop_statuses_are_unknown() -> None:
    services = Services()
    services.orders_response.orders[0].execution_report_status = (
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_UNSPECIFIED
    )
    services.orders_response.orders[0].lots_requested = 0
    services.stops_response.stop_orders[0].status = StopOrderStatusOption.STOP_ORDER_STATUS_CANCELED
    services.stops_response.stop_orders[0].lots_requested = 0
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=None,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert "ORDER_STATUS_UNKNOWN" in snapshot.unknown_reasons
    assert "ORDER_IDENTITY_UNKNOWN" in snapshot.unknown_reasons
    assert "STOP_ORDER_IDENTITY_UNKNOWN" in snapshot.unknown_reasons


def test_invalid_request_state_lots_and_negative_commission_are_unattributable() -> None:
    services = Services()
    invalid = order_state()
    invalid.lots_requested = 0
    invalid.lots_executed = 0
    invalid.executed_order_price = money("-2000")
    invalid.executed_commission = money("-0.1")
    services.request_states[REQUEST_ID] = invalid
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=None,
            operations_from=NOW - timedelta(days=1),
            request_ids=(REQUEST_ID,),
        )
    )

    assert snapshot.order_states[0].attributable is False
    assert snapshot.order_states[0].unknown_reason == "ORDER_STATE_INCOMPLETE"


def test_negative_cash_and_security_balance_mismatch_are_unknown() -> None:
    services = Services()
    services.positions_response = PositionsResponse(
        money=[money("-1")],
        blocked=[money("-2")],
        securities=[PositionsSecurities(instrument_uid=UID, blocked=1, balance=10, exchange_blocked=False)],
        limits_loading_in_progress=False,
        futures=[],
        options=[],
        account_id=ACCOUNT,
    )
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert {"MONEY_NEGATIVE", "BLOCKED_CASH_NEGATIVE", "POSITION_BALANCE_MISMATCH"} <= set(snapshot.unknown_reasons)


def test_exchange_blocked_position_and_non_integer_balance_are_unknown() -> None:
    services = Services()
    services.positions_response = PositionsResponse(
        money=[money("700")],
        blocked=[money("20")],
        securities=[PositionsSecurities(instrument_uid=UID, blocked=-1, balance=True, exchange_blocked=True)],
        limits_loading_in_progress=False,
        futures=[],
        options=[],
        account_id=ACCOUNT,
    )
    services.portfolio_response.positions[0].blocked = True
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert "POSITION_SECURITY_UNITS_UNKNOWN" in snapshot.unknown_reasons
    assert "POSITION_EXCHANGE_BLOCKED" in snapshot.unknown_reasons
    assert "POSITION_PORTFOLIO_BLOCKED" in snapshot.unknown_reasons


def test_negative_equity_position_price_blocked_lots_and_blocked_units_are_unknown() -> None:
    services = Services()
    services.portfolio_response.total_amount_portfolio = money("-1000")
    services.portfolio_response.positions[0].current_price = money("-100")
    services.portfolio_response.positions[0].blocked_lots = quote("-1")
    services.positions_response.securities[0].blocked = 1
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert {
        "EQUITY_NEGATIVE",
        "POSITION_MARK_UNKNOWN",
        "POSITION_BLOCKED_UNITS_UNKNOWN",
    } <= set(snapshot.unknown_reasons)


def test_connection_target_mismatch_fails_before_creating_sdk_client() -> None:
    services = Services()
    adapter, factories = make_adapter(services)

    with pytest.raises(ValueError, match="target"):
        asyncio.run(
            adapter.collect(
                connection(target="other.example:443"),
                instrument_uid=None,
                operations_from=NOW,
            )
        )

    assert factories == []


def test_slow_snapshot_marks_total_elapsed_unknown() -> None:
    services = Services()
    adapter, _ = make_adapter(
        services,
        times=(NOW, NOW + timedelta(milliseconds=10), NOW + timedelta(seconds=2)),
    )

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert "SNAPSHOT_WINDOW_EXCEEDED" in snapshot.unknown_reasons


def test_fresh_order_book_is_used_when_last_trade_is_73_seconds_old() -> None:
    services = Services()
    services.last_price_time = NOW - timedelta(seconds=73)
    book_time = NOW - timedelta(milliseconds=690)
    services.order_book_response = order_book_response(timestamp=book_time)
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert snapshot.instrument.mark_price == Decimal("100.05")
    assert snapshot.instrument.mark_captured_at == book_time
    assert "MARK_PRICE_STALE" not in snapshot.unknown_reasons
    assert snapshot.unknown_reasons == ()


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        (order_book_response(instrument_uid="different-uid"), "MARK_PRICE_UNKNOWN"),
        (order_book_response(timestamp=None), "MARK_PRICE_UNKNOWN"),
        (order_book_response(timestamp=NOW.replace(tzinfo=None)), "MARK_PRICE_UNKNOWN"),
        (order_book_response(timestamp=NOW + timedelta(milliseconds=11)), "MARK_PRICE_STALE"),
        (order_book_response(timestamp=NOW - timedelta(seconds=2)), "MARK_PRICE_STALE"),
        (order_book_response(bids=()), "MARK_PRICE_UNKNOWN"),
        (order_book_response(asks=()), "MARK_PRICE_UNKNOWN"),
        (
            order_book_response(
                bids=(Order(price=quote("100.05"), quantity=1),),
                asks=(Order(price=quote("100.05"), quantity=1),),
            ),
            "MARK_PRICE_UNKNOWN",
        ),
        (
            order_book_response(
                bids=(Order(price=quote("0"), quantity=1), Order(price=quote("99"), quantity=1)),
            ),
            "MARK_PRICE_UNKNOWN",
        ),
        (
            order_book_response(
                asks=(Order(price=quote("100.1"), quantity=1), Order(price=quote("100.2"), quantity=0)),
            ),
            "MARK_PRICE_UNKNOWN",
        ),
        (
            order_book_response(
                asks=(Order(price=quote("100.1"), quantity=True),),
            ),
            "MARK_PRICE_UNKNOWN",
        ),
    ],
    ids=(
        "uid-mismatch",
        "missing-timestamp",
        "naive-timestamp",
        "future-timestamp",
        "stale-timestamp",
        "empty-bids",
        "empty-asks",
        "crossed-book",
        "invalid-nonbest-price",
        "invalid-nonbest-quantity",
        "boolean-quantity",
    ),
)
def test_invalid_order_book_fails_closed(response, expected_reason) -> None:
    services = Services()
    services.order_book_response = response
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert expected_reason in snapshot.unknown_reasons
    assert snapshot.instrument.mark_price is None
    assert snapshot.instrument.mark_captured_at is None


def test_order_book_timestamp_one_second_old_at_collection_finish_is_accepted() -> None:
    services = Services()
    services.order_book_response = order_book_response(timestamp=NOW - timedelta(milliseconds=980))
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert snapshot.instrument.mark_price == Decimal("100.05")
    assert snapshot.instrument.mark_captured_at == NOW - timedelta(milliseconds=980)
    assert "MARK_PRICE_STALE" not in snapshot.unknown_reasons


@pytest.mark.parametrize(
    "price",
    [
        raw_sdk_quotation(units=1, nano=1_000_000_000),
        raw_sdk_quotation(units=2, nano=-1_000_000_000),
        raw_sdk_quotation(units=1, nano=-1),
        raw_sdk_quotation(units=-1, nano=2_000_000_000),
        raw_sdk_quotation(units=1 << 63, nano=0),
        raw_sdk_quotation(units=True, nano=0),
        raw_sdk_quotation(units=1, nano=True),
        None,
    ],
    ids=(
        "nano-overflow-positive",
        "nano-overflow-negative",
        "mixed-signs-positive-units",
        "mixed-signs-negative-units",
        "units-overflow",
        "bool-units",
        "bool-nano",
        "missing-price",
    ),
)
def test_noncanonical_sdk_quotation_fails_closed(price) -> None:
    services = Services()
    services.order_book_response = order_book_response(
        bids=(Order(price=quote("0.1"), quantity=1),),
        asks=(Order(price=price, quantity=1),),
    )
    adapter, _ = make_adapter(services)

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert "MARK_PRICE_UNKNOWN" in snapshot.unknown_reasons
    assert snapshot.instrument.mark_price is None


def test_order_book_staleness_uses_collection_finish_time() -> None:
    services = Services()
    services.order_book_response = order_book_response(timestamp=NOW - timedelta(milliseconds=600))
    adapter, _ = make_adapter(
        services,
        times=(NOW, NOW + timedelta(milliseconds=100), NOW + timedelta(milliseconds=800)),
    )

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert "MARK_PRICE_STALE" in snapshot.unknown_reasons
    assert snapshot.instrument.mark_price is None
    assert snapshot.instrument.mark_captured_at is None


def test_order_book_expiry_limits_snapshot_valid_until() -> None:
    services = Services()
    book_time = NOW - timedelta(milliseconds=100)
    services.order_book_response = order_book_response(timestamp=book_time)
    adapter, _ = make_adapter(
        services,
        times=(NOW, NOW + timedelta(milliseconds=100), NOW + timedelta(milliseconds=800)),
    )

    snapshot = asyncio.run(
        adapter.collect(
            connection(),
            instrument_uid=UID,
            operations_from=NOW - timedelta(days=1),
        )
    )

    assert snapshot.instrument.mark_price == Decimal("100.05")
    assert snapshot.valid_until == book_time + timedelta(seconds=1)


def test_limit_estimate_uses_gross_initial_amount_and_full_commission() -> None:
    services = Services()
    adapter, _ = make_adapter(services)

    estimate = asyncio.run(
        adapter.estimate_limit_order(
            connection(),
            instrument_uid=UID,
            side=OrderSide.BUY,
            quantity_lots=3,
            limit_price=Decimal("100"),
        )
    )

    assert estimate.initial_order_amount == Decimal("300")
    assert estimate.total_commission == Decimal("0.9")
    assert estimate.currency == "RUB"


def test_limit_estimate_with_missing_commission_fails_closed() -> None:
    services = Services()
    response = GetOrderPriceResponse(
        total_order_amount=money("300"),
        initial_order_amount=money("300"),
        lots_requested=3,
        executed_commission=None,
        executed_commission_rub=money("0.9"),
        service_commission=money("0.1"),
        deal_commission=money("0.8"),
    )

    async def get_order_price(_request):
        return response

    services.get_order_price = get_order_price
    adapter, _ = make_adapter(services)

    with pytest.raises(TInvestAdapterError, match="Не удалось получить данные площадки"):
        asyncio.run(
            adapter.estimate_limit_order(
                connection(),
                instrument_uid=UID,
                side=OrderSide.BUY,
                quantity_lots=3,
                limit_price=Decimal("100"),
            )
        )


def test_limit_estimate_with_negative_amount_or_commission_fails_closed() -> None:
    services = Services()
    response = GetOrderPriceResponse(
        total_order_amount=money("299"),
        initial_order_amount=money("300"),
        lots_requested=3,
        executed_commission=money("-1"),
        executed_commission_rub=money("-1"),
        service_commission=money("0"),
        deal_commission=money("0"),
    )

    async def get_order_price(_request):
        return response

    services.get_order_price = get_order_price
    adapter, _ = make_adapter(services)

    with pytest.raises(TInvestAdapterError, match="Не удалось получить данные площадки"):
        asyncio.run(
            adapter.estimate_limit_order(
                connection(),
                instrument_uid=UID,
                side=OrderSide.BUY,
                quantity_lots=3,
                limit_price=Decimal("100"),
            )
        )
