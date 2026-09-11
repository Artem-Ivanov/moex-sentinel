"""T-Invest Sandbox adapter for limit-order execution."""

from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, cast

from t_tech.invest import AsyncClient
from t_tech.invest.schemas import (
    GetOrderPriceRequest,
    OrderDirection,
    OrderType,
    PriceType,
    Quotation,
)

from moex_sentinel.adapters.tinvest.converters import (
    enum_name,
    execution_unit_price,
    quotation_to_decimal,
)
from moex_sentinel.adapters.tinvest.portfolio import SANDBOX_TARGET, TInvestPortfolioAdapter
from moex_sentinel.domain.portfolio import ActiveBrokerOrder, ExternalPosition
from sentinel_contracts.broker_execution import (
    BrokerOrderState,
    BrokerPosition,
    BrokerTradingStatus,
    LimitOrderEstimate,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderSide,
)

ClientFactory = Callable[..., Any]


def _default_client_factory(token: str, *, target: str) -> AsyncClient:
    return AsyncClient(token, target=target)


class TInvestOrderExecutionAdapter:
    def __init__(
        self,
        token: str,
        target: str,
        client_factory: ClientFactory = _default_client_factory,
    ) -> None:
        if target != SANDBOX_TARGET:
            raise ValueError("Only T-Invest Sandbox execution is allowed.")
        self._token = token
        self._target = target
        self._client_factory = client_factory

    async def get_order_book(self, instrument_id: str, *, depth: int = 20) -> OrderBookSnapshot:
        async with self._client_factory(self._token, target=self._target) as services:
            response = await services.market_data.get_order_book(instrument_id=instrument_id, depth=depth)
        return OrderBookSnapshot(
            bids=tuple(OrderBookLevel(quotation_to_decimal(item.price), item.quantity) for item in response.bids),
            asks=tuple(OrderBookLevel(quotation_to_decimal(item.price), item.quantity) for item in response.asks),
            captured_at=response.orderbook_ts,
        )

    async def estimate_limit_order(
        self,
        account_id: str,
        instrument_id: str,
        side: OrderSide,
        quantity_lots: int,
        price: Decimal,
    ) -> LimitOrderEstimate:
        request = GetOrderPriceRequest(
            account_id=account_id,
            instrument_id=instrument_id,
            price=_quotation(price),
            direction=_direction(side),
            quantity=quantity_lots,
        )
        async with self._client_factory(self._token, target=self._target) as services:
            response = await services.orders.get_order_price(request)
        return LimitOrderEstimate(
            total_amount=quotation_to_decimal(response.total_order_amount),
            estimated_commission=quotation_to_decimal(response.executed_commission),
            currency=str(response.total_order_amount.currency).upper(),
        )

    async def submit_limit_order(
        self,
        account_id: str,
        instrument_id: str,
        side: OrderSide,
        quantity_lots: int,
        price: Decimal,
        idempotency_key: str,
    ) -> BrokerOrderState:
        async with self._client_factory(self._token, target=self._target) as services:
            response = await services.orders.post_order(
                instrument_id=instrument_id,
                quantity=quantity_lots,
                price=_quotation(price),
                direction=_direction(side),
                account_id=account_id,
                order_type=OrderType.ORDER_TYPE_LIMIT,
                order_id=idempotency_key,
                price_type=PriceType.PRICE_TYPE_CURRENCY,
            )
        return _order_state(response)

    async def get_order_state(self, account_id: str, broker_order_id: str) -> BrokerOrderState:
        async with self._client_factory(self._token, target=self._target) as services:
            response = await services.orders.get_order_state(
                account_id=account_id,
                order_id=broker_order_id,
                price_type=PriceType.PRICE_TYPE_CURRENCY,
            )
        return _order_state(response, order_state=True)

    async def cancel_order(self, account_id: str, broker_order_id: str) -> datetime:
        async with self._client_factory(self._token, target=self._target) as services:
            response = await services.orders.cancel_order(account_id=account_id, order_id=broker_order_id)
        return cast(datetime, response.time)

    async def find_by_idempotency_key(self, account_id: str, idempotency_key: str) -> BrokerOrderState | None:
        async with self._client_factory(self._token, target=self._target) as services:
            response = await services.orders.get_orders(account_id=account_id)
        for order in response.orders:
            if order.order_request_id == idempotency_key:
                return _order_state(order, order_state=True)
        return None

    async def list_active_orders(
        self,
        account_id: str,
        instrument_id: str,
    ) -> tuple[ActiveBrokerOrder, ...]:
        async with self._client_factory(self._token, target=self._target) as services:
            response = await services.orders.get_orders(account_id=account_id)
        terminal = {
            "EXECUTION_REPORT_STATUS_FILL",
            "EXECUTION_REPORT_STATUS_CANCELLED",
            "EXECUTION_REPORT_STATUS_REJECTED",
        }
        return tuple(
            ActiveBrokerOrder(
                account_id=account_id,
                instrument_id=instrument_id,
                broker_order_id=str(order.order_id),
                status=enum_name(order.execution_report_status),
            )
            for order in response.orders
            if str(getattr(order, "instrument_uid", "")) == instrument_id
            and enum_name(order.execution_report_status) not in terminal
        )

    async def get_positions(self, account_id: str) -> tuple[ExternalPosition, ...]:
        return await TInvestPortfolioAdapter(
            self._token,
            self._target,
            client_factory=self._client_factory,
        ).get_positions(account_id)

    async def get_position(self, account_id: str, instrument_id: str) -> BrokerPosition | None:
        async with self._client_factory(self._token, target=self._target) as services:
            response = await services.sandbox.get_sandbox_portfolio(account_id=account_id)
        for position in response.positions:
            if position.instrument_uid == instrument_id:
                average = position.average_position_price
                current = position.current_price
                return BrokerPosition(
                    instrument_id=instrument_id,
                    quantity_lots=quotation_to_decimal(position.quantity_lots),
                    average_price=quotation_to_decimal(average),
                    current_price=quotation_to_decimal(current),
                    currency=str(average.currency).upper(),
                )
        return None

    async def get_trading_status(self, instrument_id: str) -> BrokerTradingStatus:
        async with self._client_factory(self._token, target=self._target) as services:
            response = await services.market_data.get_trading_status(instrument_id=instrument_id)
        return BrokerTradingStatus(
            status=enum_name(response.trading_status),
            limit_order_available=bool(response.limit_order_available_flag),
            market_order_available=bool(response.market_order_available_flag),
            api_trade_available=bool(response.api_trade_available_flag),
        )


def _quotation(value: Decimal) -> Quotation:
    units = int(value.to_integral_value(rounding=ROUND_DOWN))
    nano = int((value - Decimal(units)) * Decimal("1000000000"))
    return Quotation(units=units, nano=nano)


def _direction(side: OrderSide) -> OrderDirection:
    return OrderDirection.ORDER_DIRECTION_BUY if side is OrderSide.BUY else OrderDirection.ORDER_DIRECTION_SELL


def _amount(value: Any | None) -> Decimal:
    return Decimal() if value is None else quotation_to_decimal(value)


def _broker_timestamp(response: Any) -> datetime | None:
    stage_times = tuple(
        value
        for stage in getattr(response, "stages", ())
        if (value := getattr(stage, "execution_time", None)) is not None
    )
    if stage_times:
        value = max(stage_times)
        return value if isinstance(value, datetime) else None
    value = getattr(response, "order_date", None)
    return value if isinstance(value, datetime) else None


def _order_state(response: Any, *, order_state: bool = False) -> BrokerOrderState:
    status_name = enum_name(response.execution_report_status)
    statuses = {
        "EXECUTION_REPORT_STATUS_NEW": "ACCEPTED",
        "EXECUTION_REPORT_STATUS_PARTIALLYFILL": "PARTIALLY_FILLED",
        "EXECUTION_REPORT_STATUS_FILL": "FILLED",
        "EXECUTION_REPORT_STATUS_CANCELLED": "CANCELLED",
        "EXECUTION_REPORT_STATUS_REJECTED": "REJECTED",
    }
    initial = getattr(response, "initial_order_price", None)
    currency = str(getattr(initial, "currency", "")).upper()
    return BrokerOrderState(
        broker_order_id=response.order_id,
        idempotency_key=response.order_request_id,
        status=statuses.get(status_name, status_name),
        requested_lots=response.lots_requested,
        executed_lots=response.lots_executed,
        requested_amount=_amount(initial),
        executed_amount=_amount(getattr(response, "total_order_amount", None)),
        estimated_commission=_amount(getattr(response, "initial_commission", None)),
        executed_commission=_amount(getattr(response, "executed_commission", None)),
        currency=currency,
        executed_price=execution_unit_price(response, order_state=order_state),
        executed_at=_broker_timestamp(response),
    )
