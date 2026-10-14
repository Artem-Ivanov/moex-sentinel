"""Read-only T-Invest snapshots for Core's production admission gate."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn
from uuid import uuid4

from grpc import StatusCode
from t_tech.invest import AsyncClient
from t_tech.invest.schemas import (
    GetOperationsByCursorRequest,
    GetOrderPriceRequest,
    InstrumentIdType,
    OrderDirection,
    OrderIdType,
    PriceType,
    Quotation,
    StopOrderStatusOption,
)

from moex_sentinel.adapters.tinvest.converters import quotation_to_decimal
from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.adapters.tinvest.request_errors import (
    SDK_REQUEST_ERRORS,
    invalid_response_error,
    map_request_error,
    request_status,
)
from moex_sentinel.domain.trading_summary import INPUT_OPERATION_TYPES, OUTPUT_OPERATION_TYPES
from moex_sentinel.services.trading_admission_ports import (
    AdmissionInstrument,
    AdmissionOperation,
    AdmissionOrder,
    AdmissionOrderEstimate,
    AdmissionOrderState,
    AdmissionPosition,
    AdmissionRiskSnapshot,
)
from sentinel_contracts.broker_execution import BrokerConnection, OrderSide
from sentinel_contracts.time import floor_utc_millisecond, utc_now_ms
from sentinel_contracts.tinvest import resolve_tinvest_endpoint

ClientFactory = Callable[..., Any]
MAX_OPERATION_PAGES = 100
OPERATIONS_PAGE_SIZE = 1000
SNAPSHOT_VALIDITY = timedelta(seconds=1)
MAX_QUOTATION_UNITS = (1 << 63) - 1
MIN_QUOTATION_UNITS = -(1 << 63)
MAX_QUOTATION_NANO = 999_999_999
QUOTATION_NANO_FACTOR = Decimal("1000000000")
EXTERNAL_CASH_FLOW_TYPES = INPUT_OPERATION_TYPES | OUTPUT_OPERATION_TYPES
ORDER_STATUS_MAP = {
    "EXECUTION_REPORT_STATUS_NEW": "NEW",
    "EXECUTION_REPORT_STATUS_PARTIALLYFILL": "PARTIALLYFILL",
    "EXECUTION_REPORT_STATUS_FILL": "FILLED",
    "EXECUTION_REPORT_STATUS_CANCELLED": "CANCELLED",
    "EXECUTION_REPORT_STATUS_REJECTED": "REJECTED",
}


def _default_client_factory(token: str, *, target: str) -> AsyncClient:
    return AsyncClient(token, target=target)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    result = quotation_to_decimal(value)
    return result if result.is_finite() else None


def _money(value: Any) -> tuple[Decimal | None, str | None]:
    if value is None:
        return None, None
    amount = _decimal(value)
    currency = str(getattr(value, "currency", "") or "").upper() or None
    return amount, currency


def _side(value: Any) -> OrderSide | None:
    name = _enum_name(value)
    if name.endswith("_BUY"):
        return OrderSide.BUY
    if name.endswith("_SELL"):
        return OrderSide.SELL
    return None


def _enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    return value if isinstance(value, str) and value else "UNKNOWN"


def _order_status(value: Any) -> str:
    status = _enum_name(value)
    return ORDER_STATUS_MAP.get(status, "UNKNOWN")


def _raise_read_failure(error: BaseException) -> NoReturn:
    raise error


def _echo_matches(response: Any, account_id: str) -> bool:
    echo = getattr(response, "account_id", None)
    return not echo or echo == account_id


class TInvestAdmissionRiskAdapter:
    """Collect fresh broker facts and price estimates; exposes no mutation RPCs."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], datetime] = utc_now_ms,
    ) -> None:
        self._client_factory = client_factory or _default_client_factory
        self._clock = clock

    @staticmethod
    def _validate_connection(connection: BrokerConnection) -> str:
        if connection.environment != "PROD" or connection.is_test:
            raise ValueError("Admission risk collection requires a PROD broker connection.")
        if not connection.token or not connection.account_id.strip():
            raise ValueError("Admission risk collection requires an available account connection.")
        return resolve_tinvest_endpoint("PROD", connection.adapter_code, connection.target)

    async def collect(
        self,
        connection: BrokerConnection,
        *,
        instrument_uid: str | None,
        operations_from: datetime,
        request_ids: tuple[str, ...] = (),
    ) -> AdmissionRiskSnapshot:
        target = self._validate_connection(connection)
        capture_started_at = floor_utc_millisecond(self._clock())
        unknown: set[str] = set()
        try:
            async with self._client_factory(connection.token, target=target) as services:
                read_results = await asyncio.gather(
                    services.operations.get_portfolio(account_id=connection.account_id),
                    services.operations.get_positions(account_id=connection.account_id),
                    services.orders.get_orders(account_id=connection.account_id),
                    services.stop_orders.get_stop_orders(
                        account_id=connection.account_id,
                        status=StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE,
                    ),
                    (
                        self._instrument(services, instrument_uid, unknown)
                        if instrument_uid is not None
                        else asyncio.sleep(0, result=None)
                    ),
                    *(
                        self._request_state(services, request_id, connection.account_id, unknown)
                        for request_id in dict.fromkeys(request_ids)
                    ),
                    return_exceptions=True,
                )
                for result in read_results:
                    if isinstance(result, BaseException):
                        _raise_read_failure(result)
                reads = read_results
                portfolio, positions_response, orders_response, stops_response, instrument = reads[:5]
                states = tuple(reads[5:])
                captured_at = floor_utc_millisecond(self._clock())
                operations, operations_complete = await self._operations(
                    services,
                    connection.account_id,
                    floor_utc_millisecond(operations_from),
                    captured_at,
                    unknown,
                )
        except SDK_REQUEST_ERRORS as error:
            raise map_request_error(error) from error
        except TInvestAdapterError:
            raise
        except Exception as error:
            raise invalid_response_error() from error

        finished_at = floor_utc_millisecond(self._clock())
        if finished_at < captured_at or finished_at - capture_started_at > SNAPSHOT_VALIDITY:
            unknown.add("SNAPSHOT_WINDOW_EXCEEDED")
        snapshot_valid_until = captured_at + SNAPSHOT_VALIDITY
        if instrument is not None and instrument.mark_captured_at is not None:
            mark_captured_at = instrument.mark_captured_at
            snapshot_valid_until = min(snapshot_valid_until, mark_captured_at + SNAPSHOT_VALIDITY)
            mark_age = finished_at - mark_captured_at
            if mark_captured_at > captured_at or mark_age < timedelta(0) or mark_age > SNAPSHOT_VALIDITY:
                unknown.add("MARK_PRICE_STALE")
                instrument = replace(instrument, mark_price=None, mark_captured_at=None)
        if any(
            (
                operation.operation_type in EXTERNAL_CASH_FLOW_TYPES
                or operation.operation_type.startswith(
                    ("INPUT", "OUTPUT", "OPERATION_TYPE_INPUT", "OPERATION_TYPE_OUTPUT")
                )
            )
            and capture_started_at <= operation.occurred_at <= captured_at
            for operation in operations
        ):
            unknown.add("EXTERNAL_CASH_FLOW_CAPTURE_WINDOW")
        if not _echo_matches(portfolio, connection.account_id) or not _echo_matches(
            positions_response, connection.account_id
        ):
            unknown.add("ACCOUNT_ID_MISMATCH")

        equity, equity_currency = _money(getattr(portfolio, "total_amount_portfolio", None))
        if equity is None or equity_currency is None:
            unknown.add("EQUITY_UNKNOWN")
        elif equity < 0:
            unknown.add("EQUITY_NEGATIVE")

        money, money_currency = self._sum_rub(getattr(positions_response, "money", None), unknown, "MONEY")
        blocked_cash, blocked_currency = self._sum_rub(
            getattr(positions_response, "blocked", None), unknown, "BLOCKED_CASH"
        )
        limits_loading = getattr(positions_response, "limits_loading_in_progress", None)
        money_loaded = limits_loading is False and money is not None and blocked_cash is not None
        if limits_loading is not False:
            unknown.add("POSITIONS_LOADING")
        if getattr(positions_response, "futures", None):
            unknown.add("DERIVATIVE_POSITIONS_UNSUPPORTED")
        if getattr(positions_response, "options", None):
            unknown.add("DERIVATIVE_POSITIONS_UNSUPPORTED")

        positions = self._positions(
            getattr(portfolio, "positions", None),
            getattr(positions_response, "securities", None),
            instrument_uid,
            instrument,
            unknown,
        )
        ordinary_orders = self._orders(getattr(orders_response, "orders", None), unknown)
        stop_orders = self._stop_orders(getattr(stops_response, "stop_orders", None), unknown)

        return AdmissionRiskSnapshot(
            snapshot_id=str(uuid4()),
            account_id=connection.account_id,
            capture_started_at=capture_started_at,
            captured_at=captured_at,
            valid_until=snapshot_valid_until,
            equity=equity,
            equity_currency=equity_currency,
            money=money,
            money_currency=money_currency,
            blocked_cash=blocked_cash,
            blocked_cash_currency=blocked_currency,
            money_loaded=money_loaded,
            positions=positions,
            ordinary_orders=ordinary_orders,
            stop_orders=stop_orders,
            order_states=states,
            operations=operations,
            operations_complete=operations_complete,
            operations_through=captured_at,
            instrument=instrument,
            unknown_reasons=tuple(sorted(unknown)),
        )

    async def estimate_limit_order(
        self,
        connection: BrokerConnection,
        *,
        instrument_uid: str,
        side: OrderSide,
        quantity_lots: int,
        limit_price: Decimal,
    ) -> AdmissionOrderEstimate:
        target = self._validate_connection(connection)
        if (
            not instrument_uid.strip()
            or quantity_lots <= 0
            or isinstance(quantity_lots, bool)
            or not isinstance(limit_price, Decimal)
            or not limit_price.is_finite()
            or limit_price <= 0
            or side not in {OrderSide.BUY, OrderSide.SELL}
        ):
            raise ValueError("Admission estimate requires a positive finite limit order.")
        request = GetOrderPriceRequest(
            account_id=connection.account_id,
            instrument_id=instrument_uid,
            price=_quotation(limit_price),
            direction=(
                OrderDirection.ORDER_DIRECTION_BUY if side == OrderSide.BUY else OrderDirection.ORDER_DIRECTION_SELL
            ),
            quantity=quantity_lots,
        )
        try:
            async with self._client_factory(connection.token, target=target) as services:
                response = await services.orders.get_order_price(request)
        except SDK_REQUEST_ERRORS as error:
            raise map_request_error(error) from error
        except Exception as error:
            raise invalid_response_error() from error

        gross, currency = _money(getattr(response, "initial_order_amount", None))
        commission, commission_currency = _money(getattr(response, "executed_commission", None))
        if (
            getattr(response, "lots_requested", None) != quantity_lots
            or gross is None
            or gross <= 0
            or commission is None
            or commission < 0
            or currency is None
            or commission_currency != currency
        ):
            raise invalid_response_error()
        return AdmissionOrderEstimate(
            initial_order_amount=gross,
            total_commission=commission,
            currency=currency,
            captured_at=floor_utc_millisecond(self._clock()),
        )

    async def _instrument(self, services: Any, instrument_uid: str, unknown: set[str]) -> AdmissionInstrument | None:
        response = await services.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
            id=instrument_uid,
        )
        item = getattr(response, "instrument", None)
        if item is None or getattr(item, "uid", None) != instrument_uid:
            unknown.add("INSTRUMENT_UID_MISMATCH")
            return None
        currency = str(getattr(item, "currency", "") or "").upper() or None
        lot_size = getattr(item, "lot", None)
        price_increment = _decimal(getattr(item, "min_price_increment", None))
        kind = _enum_name(getattr(item, "instrument_kind", None)).removeprefix("INSTRUMENT_TYPE_")
        if kind == "ETF":
            kind = "FUND"
        if kind == "UNKNOWN":
            unknown.add("INSTRUMENT_TYPE_UNKNOWN")
        api_trade_available = getattr(item, "api_trade_available_flag", None)
        if currency is None:
            unknown.add("INSTRUMENT_CURRENCY_UNKNOWN")
        if not isinstance(lot_size, int) or isinstance(lot_size, bool) or lot_size <= 0:
            unknown.add("INSTRUMENT_LOT_UNKNOWN")
        if price_increment is None or price_increment <= 0:
            unknown.add("INSTRUMENT_PRICE_INCREMENT_UNKNOWN")
        if not isinstance(api_trade_available, bool):
            unknown.add("INSTRUMENT_TRADE_STATUS_UNKNOWN")
            api_trade_available = False
        if (
            currency is None
            or not isinstance(lot_size, int)
            or lot_size <= 0
            or price_increment is None
            or price_increment <= 0
        ):
            return None
        mark_price, mark_captured_at = await self._order_book_mark(services, instrument_uid, unknown)
        return AdmissionInstrument(
            instrument_uid=instrument_uid,
            instrument_type=kind,
            currency=currency,
            lot_size=lot_size,
            price_increment=price_increment,
            api_trade_available=api_trade_available,
            mark_price=mark_price,
            mark_captured_at=mark_captured_at,
        )

    @staticmethod
    async def _order_book_mark(
        services: Any, instrument_uid: str, unknown: set[str]
    ) -> tuple[Decimal | None, datetime | None]:
        response = await services.market_data.get_order_book(instrument_id=instrument_uid, depth=20)
        if getattr(response, "instrument_uid", None) != instrument_uid:
            unknown.add("MARK_PRICE_UNKNOWN")
            return None, None

        captured_at = getattr(response, "orderbook_ts", None)
        if not isinstance(captured_at, datetime) or captured_at.utcoffset() is None:
            unknown.add("MARK_PRICE_UNKNOWN")
            return None, None

        bids = TInvestAdmissionRiskAdapter._book_levels(getattr(response, "bids", None))
        asks = TInvestAdmissionRiskAdapter._book_levels(getattr(response, "asks", None))
        if not bids or not asks:
            unknown.add("MARK_PRICE_UNKNOWN")
            return None, None

        best_bid = max(price for price, _ in bids)
        best_ask = min(price for price, _ in asks)
        if best_bid >= best_ask:
            unknown.add("MARK_PRICE_UNKNOWN")
            return None, None
        return best_ask, floor_utc_millisecond(captured_at)

    @staticmethod
    def _book_levels(values: Any) -> tuple[tuple[Decimal, int], ...] | None:
        levels: list[tuple[Decimal, int]] = []
        for item in values or ():
            price = TInvestAdmissionRiskAdapter._book_price(getattr(item, "price", None))
            quantity = getattr(item, "quantity", None)
            if (
                price is None
                or price <= 0
                or not isinstance(quantity, int)
                or isinstance(quantity, bool)
                or quantity <= 0
            ):
                return None
            levels.append((price, quantity))
        return tuple(levels)

    @staticmethod
    def _book_price(value: Any) -> Decimal | None:
        if not isinstance(value, Quotation):
            return None
        units = getattr(value, "units", None)
        nano = getattr(value, "nano", None)
        if (
            not isinstance(units, int)
            or isinstance(units, bool)
            or not MIN_QUOTATION_UNITS <= units <= MAX_QUOTATION_UNITS
            or not isinstance(nano, int)
            or isinstance(nano, bool)
            or not -MAX_QUOTATION_NANO <= nano <= MAX_QUOTATION_NANO
            or (units > 0 and nano < 0)
            or (units < 0 and nano > 0)
        ):
            return None
        price = Decimal(units) + Decimal(nano) / QUOTATION_NANO_FACTOR
        return price if price.is_finite() and price > 0 else None

    async def _request_state(
        self, services: Any, request_id: str, account_id: str, unknown: set[str]
    ) -> AdmissionOrderState:
        try:
            response = await services.orders.get_order_state(
                account_id=account_id,
                order_id=request_id,
                order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
                price_type=PriceType.PRICE_TYPE_CURRENCY,
            )
        except SDK_REQUEST_ERRORS as error:
            if request_status(error) in {StatusCode.NOT_FOUND, StatusCode.UNIMPLEMENTED}:
                reason = (
                    "REQUEST_NOT_FOUND"
                    if request_status(error) is StatusCode.NOT_FOUND
                    else "REQUEST_LOOKUP_UNSUPPORTED"
                )
                unknown.add(reason)
                return self._unknown_order_state(request_id, reason)
            raise

        if getattr(response, "order_request_id", None) != request_id:
            unknown.add("REQUEST_ID_MISMATCH")
            return self._unknown_order_state(request_id, "REQUEST_ID_MISMATCH", response)
        status = _order_status(getattr(response, "execution_report_status", None))
        side = _side(getattr(response, "direction", None))
        # OrderState.total_order_amount already includes commission; keep gross execution separate.
        executed_amount, amount_currency = _money(getattr(response, "executed_order_price", None))
        commission, commission_currency = _money(getattr(response, "executed_commission", None))
        currency = str(getattr(response, "currency", "") or amount_currency or "").upper() or None
        is_complete = (
            bool(getattr(response, "order_id", ""))
            and bool(getattr(response, "instrument_uid", ""))
            and side is not None
            and status != "UNKNOWN"
            and isinstance(getattr(response, "lots_requested", None), int)
            and not isinstance(getattr(response, "lots_requested", None), bool)
            and response.lots_requested > 0
            and isinstance(getattr(response, "lots_executed", None), int)
            and not isinstance(getattr(response, "lots_executed", None), bool)
            and 0 <= response.lots_executed <= response.lots_requested
            and executed_amount is not None
            and executed_amount >= 0
            and commission is not None
            and commission >= 0
            and currency is not None
            and amount_currency == currency
            and commission_currency == currency
        )
        if not is_complete:
            unknown.add("ORDER_STATE_INCOMPLETE")
        return AdmissionOrderState(
            request_id=request_id,
            broker_order_id=getattr(response, "order_id", None) or None,
            instrument_uid=getattr(response, "instrument_uid", None) or None,
            side=side,
            requested_lots=getattr(response, "lots_requested", None),
            executed_lots=getattr(response, "lots_executed", None),
            executed_amount=executed_amount,
            executed_commission=commission,
            currency=currency,
            status=status,
            attributable=is_complete,
            unknown_reason=None if is_complete else "ORDER_STATE_INCOMPLETE",
        )

    @staticmethod
    def _unknown_order_state(request_id: str, reason: str, response: Any | None = None) -> AdmissionOrderState:
        return AdmissionOrderState(
            request_id=request_id,
            broker_order_id=getattr(response, "order_id", None) or None,
            instrument_uid=getattr(response, "instrument_uid", None) or None,
            side=_side(getattr(response, "direction", None)),
            requested_lots=getattr(response, "lots_requested", None),
            executed_lots=getattr(response, "lots_executed", None),
            executed_amount=None,
            executed_commission=None,
            currency=None,
            status="UNKNOWN",
            attributable=False,
            unknown_reason=reason,
        )

    async def _operations(
        self,
        services: Any,
        account_id: str,
        from_at: datetime,
        through: datetime,
        unknown: set[str],
    ) -> tuple[tuple[AdmissionOperation, ...], bool]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        items: dict[str, AdmissionOperation] = {}
        complete = False
        for _ in range(MAX_OPERATION_PAGES):
            response = await services.operations.get_operations_by_cursor(
                GetOperationsByCursorRequest(
                    account_id=account_id,
                    from_=from_at,
                    to=through,
                    cursor=cursor,
                    limit=OPERATIONS_PAGE_SIZE,
                )
            )
            for item in getattr(response, "items", ()):
                operation_id = str(getattr(item, "id", "") or "")
                operation = self._operation(item, account_id, unknown)
                if operation is None:
                    continue
                if not operation_id:
                    unknown.add("OPERATION_ID_UNKNOWN")
                    continue
                prior = items.get(operation_id)
                if prior is not None and prior != operation:
                    unknown.add("OPERATION_ID_CONFLICT")
                else:
                    items[operation_id] = operation
            has_next = getattr(response, "has_next", None)
            next_cursor = getattr(response, "next_cursor", None)
            if has_next is False:
                complete = True
                break
            if has_next is not True or not next_cursor or next_cursor in seen_cursors:
                reason = (
                    "OPERATIONS_CURSOR_REPEATED" if next_cursor in seen_cursors else "OPERATIONS_PAGINATION_INCOMPLETE"
                )
                unknown.add(reason)
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            unknown.add("OPERATIONS_PAGE_LIMIT")
        if not complete:
            unknown.add("OPERATIONS_PAGINATION_INCOMPLETE")
        return tuple(items.values()), complete

    @staticmethod
    def _operation(item: Any, account_id: str, unknown: set[str]) -> AdmissionOperation | None:
        occurred_at = getattr(item, "date", None)
        if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
            unknown.add("OPERATION_TIMESTAMP_UNKNOWN")
            return None
        payment, currency = _money(getattr(item, "payment", None))
        if payment is None or currency is None:
            unknown.add("OPERATION_PAYMENT_UNKNOWN")
        operation_type = _enum_name(getattr(item, "type", None))
        state = _enum_name(getattr(item, "state", None))
        if not operation_type.startswith("OPERATION_TYPE_") or operation_type.endswith("UNSPECIFIED"):
            unknown.add("OPERATION_TYPE_UNKNOWN")
        if not state.startswith("OPERATION_STATE_") or state.endswith("UNSPECIFIED"):
            unknown.add("OPERATION_STATE_UNKNOWN")
        source_account = str(getattr(item, "broker_account_id", None) or "")
        if source_account != account_id:
            unknown.add("OPERATION_ACCOUNT_MISMATCH")
        return AdmissionOperation(
            operation_id=str(getattr(item, "id", "") or ""),
            occurred_at=floor_utc_millisecond(occurred_at),
            operation_type=operation_type,
            state=state,
            payment=payment,
            currency=currency,
            account_id=source_account,
        )

    @staticmethod
    def _sum_rub(values: Any, unknown: set[str], field: str) -> tuple[Decimal | None, str | None]:
        if values is None:
            unknown.add(f"{field}_UNKNOWN")
            return None, None
        total = Decimal()
        found = False
        for value in values:
            amount, currency = _money(value)
            if amount is None or currency is None:
                unknown.add(f"{field}_UNKNOWN")
                return None, None
            if currency != "RUB":
                unknown.add(f"{field}_CURRENCY_UNKNOWN")
                return None, None
            if amount < 0:
                unknown.add(f"{field}_NEGATIVE")
                return None, None
            total += amount
            found = True
        return total if found else Decimal(), "RUB"

    @staticmethod
    def _positions(
        values: Any,
        securities: Any,
        selected_uid: str | None,
        instrument: AdmissionInstrument | None,
        unknown: set[str],
    ) -> tuple[AdmissionPosition, ...]:
        if values is None or securities is None:
            unknown.add("POSITIONS_UNKNOWN")
            return ()
        security_units: dict[str, tuple[int, int]] = {}
        for security in securities:
            uid = getattr(security, "instrument_uid", "")
            balance = getattr(security, "balance", None)
            blocked = getattr(security, "blocked", None)
            exchange_blocked = getattr(security, "exchange_blocked", None)
            if not uid or uid in security_units:
                unknown.add("POSITION_SECURITY_IDENTITY_UNKNOWN")
                continue
            if exchange_blocked is not False:
                unknown.add("POSITION_EXCHANGE_BLOCKED")
            if (
                not isinstance(balance, int)
                or isinstance(balance, bool)
                or balance < 0
                or not isinstance(blocked, int)
                or isinstance(blocked, bool)
                or blocked < 0
            ):
                unknown.add("POSITION_SECURITY_UNITS_UNKNOWN")
                continue
            security_units[uid] = (balance, blocked)
        result = []
        seen_position_uids: set[str] = set()
        for position in values:
            # Cash is validated through GetPositions.money/blocked, not security balances.
            if str(getattr(position, "instrument_type", "") or "").lower() == "currency":
                continue
            uid = getattr(position, "instrument_uid", None) or ""
            quantity, _ = _money(getattr(position, "quantity", None))
            # Quantity is a Quotation in instrument units; it is not an order-lot count.
            if quantity is None:
                quantity = _decimal(getattr(position, "quantity", None))
            units = security_units.get(uid)
            portfolio_blocked = getattr(position, "blocked", None)
            if portfolio_blocked is not False:
                unknown.add("POSITION_PORTFOLIO_BLOCKED")
            blocked_lots = _decimal(getattr(position, "blocked_lots", None))
            if blocked_lots is None or blocked_lots < 0:
                unknown.add("POSITION_BLOCKED_UNITS_UNKNOWN")
            elif blocked_lots > 0:
                unknown.add("POSITION_PORTFOLIO_BLOCKED")
            if not uid or uid in seen_position_uids or quantity is None or units is None:
                unknown.add("POSITION_QUANTITY_UNKNOWN")
                continue
            seen_position_uids.add(uid)
            balance, blocked = units
            if quantity != Decimal(balance + blocked):
                unknown.add("POSITION_BALANCE_MISMATCH")
            price = instrument.mark_price if uid == selected_uid and instrument is not None else None
            currency = None
            current_price = getattr(position, "current_price", None)
            if current_price is not None:
                current_amount, currency = _money(current_price)
                if current_amount is None or current_amount <= 0:
                    unknown.add("POSITION_MARK_UNKNOWN")
            else:
                unknown.add("POSITION_MARK_UNKNOWN")
            currency = currency or (instrument.currency if uid == selected_uid and instrument is not None else None)
            if currency is None:
                unknown.add("POSITION_CURRENCY_UNKNOWN")
            if quantity < 0:
                unknown.add("POSITION_QUANTITY_INVALID")
            result.append(
                AdmissionPosition(
                    instrument_uid=uid,
                    quantity_units=quantity,
                    blocked_units=Decimal(blocked),
                    mark_price=price,
                    currency=currency,
                )
            )
        if set(security_units) - seen_position_uids:
            unknown.add("POSITION_BALANCE_MISMATCH")
        return tuple(result)

    @staticmethod
    def _orders(values: Any, unknown: set[str]) -> tuple[AdmissionOrder, ...]:
        if values is None:
            unknown.add("ORDERS_UNKNOWN")
            return ()
        orders = tuple(
            AdmissionOrder(
                broker_order_id=str(getattr(item, "order_id", "") or ""),
                instrument_uid=str(getattr(item, "instrument_uid", "") or ""),
                request_id=getattr(item, "order_request_id", None) or None,
                status=_order_status(getattr(item, "execution_report_status", None)),
                side=_side(getattr(item, "direction", None)),
                quantity_lots=getattr(item, "lots_requested", None),
            )
            for item in values
        )
        if any(
            not order.broker_order_id
            or not order.instrument_uid
            or order.side is None
            or not isinstance(order.quantity_lots, int)
            or isinstance(order.quantity_lots, bool)
            or order.quantity_lots <= 0
            for order in orders
        ):
            unknown.add("ORDER_IDENTITY_UNKNOWN")
        if any(order.status == "UNKNOWN" for order in orders):
            unknown.add("ORDER_STATUS_UNKNOWN")
        return orders

    @staticmethod
    def _stop_orders(values: Any, unknown: set[str]) -> tuple[AdmissionOrder, ...]:
        if values is None:
            unknown.add("STOP_ORDERS_UNKNOWN")
            return ()
        orders = tuple(
            AdmissionOrder(
                broker_order_id=str(getattr(item, "stop_order_id", "") or ""),
                instrument_uid=str(getattr(item, "instrument_uid", "") or ""),
                request_id=None,
                status=_enum_name(getattr(item, "status", None)),
                side=_side(getattr(item, "direction", None)),
                quantity_lots=getattr(item, "lots_requested", None),
            )
            for item in values
        )
        if any(
            not order.broker_order_id
            or not order.instrument_uid
            or order.side is None
            or not isinstance(order.quantity_lots, int)
            or isinstance(order.quantity_lots, bool)
            or order.quantity_lots <= 0
            for order in orders
        ):
            unknown.add("STOP_ORDER_IDENTITY_UNKNOWN")
        if any(order.status != "STOP_ORDER_STATUS_ACTIVE" for order in orders):
            unknown.add("STOP_ORDER_STATUS_UNKNOWN")
        return orders


def _quotation(value: Decimal) -> Quotation:
    units = int(value)
    nano_value = (value - units) * Decimal("1000000000")
    nano = int(nano_value)
    if nano_value != nano:
        raise ValueError("Limit price exceeds broker quotation precision.")
    return Quotation(units=units, nano=nano)


__all__ = ["TInvestAdmissionRiskAdapter"]
