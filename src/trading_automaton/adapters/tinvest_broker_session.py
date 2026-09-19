"""Long-lived T-Invest async client session owned by one broker runtime."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from time import monotonic as system_monotonic
from typing import Any, Literal

from grpc import StatusCode
from t_tech.invest import AioRequestError, AsyncClient
from t_tech.invest.schemas import (
    CandleInterval,
    GetOperationsByCursorRequest,
    GetOrderPriceRequest,
    OrderDirection,
    OrderType,
    PriceType,
    Quotation,
)

from moex_sentinel.adapters.tinvest.converters import enum_name, execution_unit_price, quotation_to_decimal
from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.domain.market_data import HistoricCandle
from sentinel_contracts.broker_execution import BrokerOrderState, BrokerPosition
from sentinel_contracts.time import floor_utc_millisecond
from trading_automaton.domain.dtos import CommissionQuote, CommissionRefreshRequest, DispatchRequest

LOGGER = logging.getLogger(__name__)
_TLS_UNWRAP_FAILURE = "Stream removed (Unwrap failed (TSI_DATA_CORRUPTED))"


def _default_client_factory(token: str, *, target: str) -> AsyncClient:
    return AsyncClient(token, target=target)


class BrokerSdkSession:
    def __init__(
        self,
        token: str,
        target: str,
        *,
        client_factory: Callable[..., Any] = _default_client_factory,
        snapshot_ttl_seconds: float = 60.0,
        monotonic: Callable[[], float] = system_monotonic,
    ) -> None:
        if snapshot_ttl_seconds <= 0:
            raise ValueError("Account snapshot TTL must be positive.")
        self._token = token
        self._target = target
        self._client_factory = client_factory
        self._snapshot_ttl_seconds = snapshot_ttl_seconds
        self._monotonic = monotonic
        self._client: Any | None = None
        self._services: Any | None = None
        self._snapshot_lock = asyncio.Lock()
        self._positions_cache: dict[str, tuple[float, tuple[BrokerPosition, ...]]] = {}
        self._money_cache: dict[str, tuple[float, dict[str, Decimal]]] = {}

    @property
    def services(self) -> Any:
        if self._services is None:
            raise RuntimeError("Broker SDK session is not started.")
        return self._services

    async def start(self) -> None:
        if self._services is not None:
            return
        self._client = self._client_factory(self._token, target=self._target)
        self._services = await self._client.__aenter__()

    async def close(self) -> None:
        if self._client is None:
            return
        await self._client.__aexit__(None, None, None)
        self._client = None
        self._services = None
        async with self._snapshot_lock:
            self._positions_cache.clear()
            self._money_cache.clear()

    async def quote(self, request: CommissionRefreshRequest, side: str) -> CommissionQuote:
        direction = OrderDirection.ORDER_DIRECTION_BUY if side == "BUY" else OrderDirection.ORDER_DIRECTION_SELL
        response = await self._broker_request(
            self.services.orders.get_order_price(
                GetOrderPriceRequest(
                    account_id=request.key.account_id,
                    instrument_id=request.instrument_id,
                    price=_quotation(request.price),
                    direction=direction,
                    quantity=request.quantity_lots,
                )
            )
        )
        return CommissionQuote(
            order_amount=quotation_to_decimal(response.initial_order_amount),
            total_commission=quotation_to_decimal(response.executed_commission),
            service_commission=quotation_to_decimal(response.service_commission),
            deal_commission=quotation_to_decimal(response.deal_commission),
        )

    def create_market_data_stream(self) -> Any:
        return self.services.create_market_data_stream()

    async def get_positions(self, account_id: str) -> tuple[BrokerPosition, ...]:
        async with self._snapshot_lock:
            cached = self._positions_cache.get(account_id)
            now = self._monotonic()
            if cached is not None and cached[0] > now:
                return cached[1]
            response = await self._broker_request(
                self.services.sandbox.get_sandbox_portfolio(account_id=account_id),
                snapshot_operation="GetSandboxPortfolio",
            )
            positions = tuple(
                BrokerPosition(
                    instrument_id=item.instrument_uid,
                    quantity_lots=quotation_to_decimal(item.quantity_lots),
                    average_price=quotation_to_decimal(item.average_position_price),
                    current_price=quotation_to_decimal(item.current_price),
                    currency=str(item.average_position_price.currency).upper(),
                )
                for item in response.positions
            )
            self._positions_cache[account_id] = (now + self._snapshot_ttl_seconds, positions)
            return positions

    async def get_free_cash(self, account_id: str, currency: str) -> Decimal:
        async with self._snapshot_lock:
            cached = self._money_cache.get(account_id)
            now = self._monotonic()
            if cached is None or cached[0] <= now:
                response = await self._broker_request(
                    self.services.sandbox.get_sandbox_positions(account_id=account_id),
                    snapshot_operation="GetSandboxPositions",
                )
                values: dict[str, Decimal] = {}
                for item in response.money:
                    item_currency = str(item.currency).upper()
                    values[item_currency] = values.get(item_currency, Decimal()) + quotation_to_decimal(item)
                cached = (now + self._snapshot_ttl_seconds, values)
                self._money_cache[account_id] = cached
            return cached[1].get(currency.upper(), Decimal())

    async def invalidate_account_snapshot(self, account_id: str) -> None:
        async with self._snapshot_lock:
            self._positions_cache.pop(account_id, None)
            self._money_cache.pop(account_id, None)

    async def get_candles(
        self,
        instrument_id: str,
        start: datetime,
        end: datetime,
        interval: object | None = None,
    ) -> tuple[HistoricCandle, ...]:
        del interval
        response = await self._broker_request(
            self.services.market_data.get_candles(
                instrument_id=instrument_id,
                from_=start,
                to=end,
                interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
            )
        )
        return tuple(
            HistoricCandle(
                instrument_id=instrument_id,
                open=quotation_to_decimal(item.open),
                high=quotation_to_decimal(item.high),
                low=quotation_to_decimal(item.low),
                close=quotation_to_decimal(item.close),
                volume=item.volume,
                started_at=item.time,
                is_complete=item.is_complete,
            )
            for item in response.candles
        )

    async def dispatch_limit_order(self, request: DispatchRequest) -> BrokerOrderState:
        response = await self._broker_request(
            self.services.orders.post_order(
                instrument_id=request.instrument_id,
                quantity=request.quantity_lots,
                price=_quotation(request.limit_price),
                direction=(
                    OrderDirection.ORDER_DIRECTION_BUY
                    if request.side.value == "BUY"
                    else OrderDirection.ORDER_DIRECTION_SELL
                ),
                account_id=request.account_id,
                order_type=OrderType.ORDER_TYPE_LIMIT,
                order_id=request.idempotency_key,
                price_type=PriceType.PRICE_TYPE_CURRENCY,
            )
        )
        state = _order_state(response)
        await self._invalidate_if_terminal(request.account_id, state)
        return state

    async def get_order_state(
        self,
        account_id: str,
        broker_order_id: str,
    ) -> BrokerOrderState:
        response = await self._broker_request(
            self.services.orders.get_order_state(
                account_id=account_id,
                order_id=broker_order_id,
                price_type=PriceType.PRICE_TYPE_CURRENCY,
            )
        )
        state = _order_state(response, order_state=True)
        await self._invalidate_if_terminal(account_id, state)
        return state

    async def find_by_idempotency_key(
        self,
        account_id: str,
        idempotency_key: str,
    ) -> BrokerOrderState | None:
        response = await self._broker_request(self.services.orders.get_orders(account_id=account_id))
        for order in response.orders:
            if order.order_request_id == idempotency_key:
                state = _order_state(order, order_state=True)
                await self._invalidate_if_terminal(account_id, state)
                return state
        return None

    async def _invalidate_if_terminal(self, account_id: str, state: BrokerOrderState) -> None:
        if state.status in {"FILLED", "REJECTED", "CANCELLED", "EXPIRED"}:
            await self.invalidate_account_snapshot(account_id)

    @staticmethod
    def _snapshot_error(error: AioRequestError, *, snapshot_read: bool = False) -> TInvestAdapterError:
        """Retry the observed TLS unwrap failure only for side-effect-free account reads."""
        status = enum_name(error.code)
        mapping = {
            "UNAUTHENTICATED": ("BROKER_AUTH_FAILED", "Проверка токена не пройдена.", False),
            "PERMISSION_DENIED": ("BROKER_FORBIDDEN", "Недостаточно прав доступа.", False),
            "RESOURCE_EXHAUSTED": ("BROKER_RATE_LIMITED", "Превышен лимит запросов.", True),
            "UNAVAILABLE": ("BROKER_UNAVAILABLE", "Площадка временно недоступна.", True),
            "DEADLINE_EXCEEDED": ("BROKER_UNAVAILABLE", "Площадка временно недоступна.", True),
        }
        code, message, retryable = mapping.get(
            status,
            ("BROKER_UNAVAILABLE", "Не удалось получить данные площадки.", False),  # noqa: RUF001
        )
        if snapshot_read and error.code is StatusCode.UNKNOWN and error.details.strip() == _TLS_UNWRAP_FAILURE:
            retryable = True
        return TInvestAdapterError(code, message, retryable=retryable)

    async def _broker_request(
        self,
        request: Awaitable[Any],
        *,
        snapshot_operation: Literal["GetSandboxPortfolio", "GetSandboxPositions"] | None = None,
    ) -> Any:
        """Translate SDK failures once; the runtime owns retry delays and recovery probes."""
        try:
            return await request
        except AioRequestError as error:
            mapped = self._snapshot_error(error, snapshot_read=snapshot_operation is not None)
            LOGGER.warning(
                "Broker SDK request failed",
                extra={
                    "reason_code": "BROKER_SDK_REQUEST_FAILED",
                    "data": {
                        "operation": snapshot_operation or "broker_request",
                        "grpc_status": error.code.name if isinstance(error.code, StatusCode) else "UNRECOGNIZED",
                        "code": mapped.code,
                        "retryable": mapped.retryable,
                        "tls_unwrap_failure": error.details.strip() == _TLS_UNWRAP_FAILURE,
                    },
                },
            )
            raise mapped from error

    async def inspect_position(self, account_id: str, instrument_id: str) -> dict[str, object] | None:
        positions = await self.get_positions(account_id)
        for position in positions:
            if position.instrument_id == instrument_id:
                return {
                    "quantity_lots": str(position.quantity_lots),
                    "average_price": str(position.average_price),
                    "current_price": str(position.current_price),
                    "currency": position.currency,
                }
        return None

    async def inspect_recent_operations(
        self,
        account_id: str,
        instrument_id: str,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        response = await self._broker_request(
            self.services.operations.get_operations_by_cursor(
                GetOperationsByCursorRequest(
                    account_id=account_id,
                    instrument_id=instrument_id,
                    limit=limit,
                )
            )
        )
        return tuple(
            {
                "operation_id": item.id,
                "operation_type": enum_name(item.type),
                "state": enum_name(item.state),
                "occurred_at": item.date.isoformat(),
                "quantity": str(item.quantity),
                "quantity_done": str(item.quantity_done),
                "price": str(quotation_to_decimal(item.price)),
                "commission": str(quotation_to_decimal(item.commission)),
                "currency": str(item.price.currency).upper(),
            }
            for item in response.items
        )


def _quotation(value: Decimal) -> Quotation:
    units = int(value.to_integral_value(rounding=ROUND_DOWN))
    nano = int((value - Decimal(units)) * Decimal("1000000000"))
    return Quotation(units=units, nano=nano)


def _broker_timestamp(response: Any) -> datetime | None:
    stage_times = tuple(
        value
        for stage in getattr(response, "stages", ())
        if (value := getattr(stage, "execution_time", None)) is not None
    )
    if stage_times:
        value = max(stage_times)
        return floor_utc_millisecond(value) if isinstance(value, datetime) else None
    value = getattr(response, "order_date", None)
    return floor_utc_millisecond(value) if isinstance(value, datetime) else None


def _order_state(response: Any, *, order_state: bool = False) -> BrokerOrderState:
    statuses = {
        "EXECUTION_REPORT_STATUS_NEW": "ACCEPTED",
        "EXECUTION_REPORT_STATUS_PARTIALLYFILL": "PARTIALLY_FILLED",
        "EXECUTION_REPORT_STATUS_FILL": "FILLED",
        "EXECUTION_REPORT_STATUS_CANCELLED": "CANCELLED",
        "EXECUTION_REPORT_STATUS_REJECTED": "REJECTED",
    }
    status = enum_name(response.execution_report_status)
    initial = response.initial_order_price
    return BrokerOrderState(
        broker_order_id=response.order_id,
        idempotency_key=response.order_request_id,
        status=statuses.get(status, status),
        requested_lots=response.lots_requested,
        executed_lots=response.lots_executed,
        requested_amount=quotation_to_decimal(initial),
        executed_amount=quotation_to_decimal(response.total_order_amount),
        estimated_commission=quotation_to_decimal(response.initial_commission),
        executed_commission=quotation_to_decimal(response.executed_commission),
        currency=str(initial.currency).upper(),
        executed_price=execution_unit_price(response, order_state=order_state),
        executed_at=_broker_timestamp(response),
    )
