"""T-Invest Sandbox adapter for synchronous read-only market data."""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import InvalidOperation
from typing import Any

from t_tech.invest import AsyncClient
from t_tech.invest.schemas import CandleInterval as SdkCandleInterval
from t_tech.invest.schemas import InstrumentIdType, InstrumentStatus

from moex_sentinel.adapters.tinvest.converters import enum_name, quotation_to_decimal
from moex_sentinel.adapters.tinvest.portfolio import SANDBOX_TARGET
from moex_sentinel.adapters.tinvest.request_errors import (
    SDK_REQUEST_ERRORS,
    invalid_response_error,
    map_request_error,
    request_status,
)
from moex_sentinel.domain.market_data import (
    CandleInterval,
    HistoricCandle,
    LastPrice,
    MarketInstrument,
)

ClientFactory = Callable[..., Any]
logger = logging.getLogger(f"uvicorn.error.{__name__}")


def _default_client_factory(token: str, *, target: str) -> AsyncClient:
    return AsyncClient(token, target=target)


SDK_INTERVALS = {
    CandleInterval.MIN_1: SdkCandleInterval.CANDLE_INTERVAL_1_MIN,
    CandleInterval.MIN_5: SdkCandleInterval.CANDLE_INTERVAL_5_MIN,
    CandleInterval.MIN_15: SdkCandleInterval.CANDLE_INTERVAL_15_MIN,
    CandleInterval.HOUR: SdkCandleInterval.CANDLE_INTERVAL_HOUR,
    CandleInterval.DAY: SdkCandleInterval.CANDLE_INTERVAL_DAY,
}


class TInvestMarketDataAdapter:
    def __init__(
        self,
        token: str,
        target: str,
        client_factory: ClientFactory = _default_client_factory,
    ) -> None:
        if target != SANDBOX_TARGET:
            raise ValueError("Only the configured Sandbox target is allowed.")
        self._token = token
        self._target = target
        self._client_factory = client_factory

    async def list_instruments(self) -> tuple[MarketInstrument, ...]:
        """Read every catalog group before converting the complete broker snapshot."""
        try:
            async with self._client_factory(self._token, target=self._target) as services:
                responses = []
                for group, domain_type, loader in (
                    ("shares", "SHARE", services.instruments.shares),
                    ("bonds", "BOND", services.instruments.bonds),
                    ("currencies", "CURRENCY", services.instruments.currencies),
                    ("futures", "FUTURE", services.instruments.futures),
                    ("etfs", "FUND", services.instruments.etfs),
                ):
                    response = await self._load_catalog_group(group, loader)
                    responses.append((domain_type, response))
        except SDK_REQUEST_ERRORS as error:
            raise map_request_error(error) from error
        try:
            return tuple(
                self._instrument(
                    item,
                    currency=(str(item.currency).upper() if item.currency else None),
                    instrument_type=domain_type,
                )
                for domain_type, response in responses
                for item in response.instruments
            )
        except (ValueError, InvalidOperation) as error:
            raise invalid_response_error() from error

    @staticmethod
    async def _load_catalog_group(
        group: str,
        loader: Callable[..., Awaitable[Any]],
    ) -> Any:
        try:
            response = await loader(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE)
        except Exception as error:
            grpc_status = request_status(error).name if isinstance(error, SDK_REQUEST_ERRORS) else ""
            logger.warning(
                "T-Invest catalog request failed: group=%s error_type=%s grpc_status=%s",
                group,
                type(error).__name__,
                grpc_status or "unknown",
                extra={
                    "instrument_group": group,
                    "error_type": type(error).__name__,
                    "grpc_status": grpc_status,
                },
            )
            raise
        logger.info(
            "T-Invest catalog request completed: group=%s item_count=%d",
            group,
            len(response.instruments),
            extra={
                "instrument_group": group,
                "item_count": len(response.instruments),
            },
        )
        return response

    async def search_instruments(self, query: str) -> tuple[MarketInstrument, ...]:
        """Return broker search matches without assigning a catalog currency."""
        try:
            async with self._client_factory(self._token, target=self._target) as services:
                response = await services.instruments.find_instrument(query=query)
        except SDK_REQUEST_ERRORS as error:
            raise map_request_error(error) from error
        try:
            return tuple(self._instrument(item, currency=None) for item in response.instruments)
        except (ValueError, InvalidOperation) as error:
            raise invalid_response_error() from error

    async def get_instrument(self, instrument_id: str) -> MarketInstrument:
        """Resolve one broker UID and preserve its quoted currency."""
        try:
            async with self._client_factory(self._token, target=self._target) as services:
                response = await services.instruments.get_instrument_by(
                    id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                    id=instrument_id,
                )
        except SDK_REQUEST_ERRORS as error:
            raise map_request_error(error) from error
        try:
            item = response.instrument
            currency = str(item.currency).upper() if item.currency else None
            return self._instrument(item, currency=currency)
        except (ValueError, InvalidOperation) as error:
            raise invalid_response_error() from error

    async def get_last_prices(self, instrument_ids: tuple[str, ...]) -> tuple[LastPrice, ...]:
        """Return prices with source timestamps; an empty request performs no I/O."""
        if not instrument_ids:
            return ()
        try:
            async with self._client_factory(self._token, target=self._target) as services:
                response = await services.market_data.get_last_prices(instrument_id=list(instrument_ids))
        except SDK_REQUEST_ERRORS as error:
            raise map_request_error(error) from error
        try:
            return tuple(
                LastPrice(
                    instrument_id=item.instrument_uid,
                    price=quotation_to_decimal(item.price),
                    captured_at=item.time,
                )
                for item in response.last_prices
            )
        except (ValueError, InvalidOperation) as error:
            raise invalid_response_error() from error

    async def get_candles(
        self,
        instrument_id: str,
        start: datetime,
        end: datetime,
        interval: CandleInterval,
    ) -> tuple[HistoricCandle, ...]:
        """Return source candles for the requested interval without filtering incomplete bars."""
        try:
            async with self._client_factory(self._token, target=self._target) as services:
                response = await services.market_data.get_candles(
                    instrument_id=instrument_id,
                    from_=start,
                    to=end,
                    interval=SDK_INTERVALS[interval],
                )
        except SDK_REQUEST_ERRORS as error:
            raise map_request_error(error) from error
        try:
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
        except (ValueError, InvalidOperation) as error:
            raise invalid_response_error() from error

    @staticmethod
    def _instrument(
        item: Any,
        *,
        currency: str | None,
        instrument_type: str | None = None,
    ) -> MarketInstrument:
        """Convert SDK instrument fields into the validated market contract."""
        resolved_type = instrument_type or enum_name(
            getattr(item, "instrument_kind", None) or getattr(item, "instrument_type", "")
        )
        return MarketInstrument(
            instrument_id=item.uid,
            figi=item.figi,
            ticker=item.ticker,
            name=item.name,
            class_code=item.class_code,
            instrument_type=resolved_type,
            currency=currency,
            lot=item.lot,
            min_price_increment=quotation_to_decimal(item.min_price_increment),
            api_trade_available=bool(item.api_trade_available_flag),
        )


__all__ = ["TInvestMarketDataAdapter"]
