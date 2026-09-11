"""User intents for broker market-data reads."""

from datetime import datetime, timedelta
from typing import Protocol

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.domain.brokers import BrokerRecordNotFoundError
from moex_sentinel.domain.errors import FieldError
from moex_sentinel.domain.market_data import (
    CandleInterval,
    HistoricCandle,
    InstrumentMarketSnapshot,
)
from moex_sentinel.services.environment import EnvironmentMismatchError
from moex_sentinel.usecases.errors import UseCaseError


class MarketDataServicePort(Protocol):
    async def search(self, broker_id: str, query: str, limit: int) -> tuple[InstrumentMarketSnapshot, ...]: ...

    async def instrument(self, broker_id: str, instrument_id: str) -> InstrumentMarketSnapshot: ...

    async def candles(
        self,
        broker_id: str,
        instrument_id: str,
        start: datetime,
        end: datetime,
        interval: CandleInterval,
    ) -> tuple[HistoricCandle, ...]: ...


class SearchMarketInstrumentsUsecase:
    def __init__(self, service: MarketDataServicePort) -> None:
        self._service = service

    async def execute(self, broker_id: str, query: str, limit: int = 20) -> tuple[InstrumentMarketSnapshot, ...]:
        normalized = query.strip()
        if len(normalized) < 2:
            raise UseCaseError(
                "INVALID_MARKET_QUERY",
                "Укажите минимум два символа для поиска.",
                (FieldError("query", "MIN_LENGTH", "Введите минимум два символа."),),
            )
        if not 1 <= limit <= 50:
            raise UseCaseError(
                "INVALID_MARKET_QUERY",
                "Лимит результатов должен быть от 1 до 50.",
                (FieldError("limit", "OUT_OF_RANGE", "Допустимое значение: от 1 до 50."),),
            )
        try:
            return await self._service.search(broker_id, normalized, limit)
        except Exception as error:
            raise _market_error(error) from error


class ViewMarketInstrumentUsecase:
    def __init__(self, service: MarketDataServicePort) -> None:
        self._service = service

    async def execute(self, broker_id: str, instrument_id: str) -> InstrumentMarketSnapshot:
        try:
            return await self._service.instrument(broker_id, instrument_id)
        except Exception as error:
            raise _market_error(error) from error


class ViewHistoricCandlesUsecase:
    def __init__(self, service: MarketDataServicePort) -> None:
        self._service = service

    async def execute(
        self,
        broker_id: str,
        instrument_id: str,
        start: datetime,
        end: datetime,
        interval: CandleInterval,
    ) -> tuple[HistoricCandle, ...]:
        if start.tzinfo is None or end.tzinfo is None or start >= end or end - start > timedelta(days=31):
            raise UseCaseError(
                "INVALID_CANDLE_RANGE",
                "Некорректный период свечей.",
                (FieldError("from", "INVALID_RANGE", "Проверьте начало и длину периода."),),
            )
        try:
            return await self._service.candles(broker_id, instrument_id, start, end, interval)
        except Exception as error:
            raise _market_error(error) from error


def _market_error(error: Exception) -> UseCaseError:
    if isinstance(error, TInvestAdapterError):
        return UseCaseError(error.code, str(error))
    if isinstance(error, BrokerRecordNotFoundError):
        return UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.")
    if isinstance(error, EnvironmentMismatchError):
        return UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.")
    if isinstance(error, ValueError):
        return UseCaseError("BROKER_CONFIGURATION", str(error))
    raise error
