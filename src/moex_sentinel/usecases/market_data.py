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
    """Search broker instruments with stable query and connection error payloads."""

    def __init__(self, service: MarketDataServicePort) -> None:
        self._service = service

    async def execute(self, broker_id: str, query: str, limit: int = 20) -> tuple[InstrumentMarketSnapshot, ...]:
        """Return matching broker instruments; report an invalid query or known broker failure."""
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
        except TInvestAdapterError as error:
            raise UseCaseError(error.code, str(error)) from error
        except BrokerRecordNotFoundError as error:
            raise UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.") from error
        except EnvironmentMismatchError as error:
            raise UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.") from error
        except ValueError as error:
            raise UseCaseError("BROKER_CONFIGURATION", str(error)) from error


class ViewMarketInstrumentUsecase:
    """Read a broker instrument by external ID and translate declared failures."""

    def __init__(self, service: MarketDataServicePort) -> None:
        self._service = service

    async def execute(self, broker_id: str, instrument_id: str) -> InstrumentMarketSnapshot:
        """Return a snapshot by external instrument ID; translate known broker failures."""
        try:
            return await self._service.instrument(broker_id, instrument_id)
        except TInvestAdapterError as error:
            raise UseCaseError(error.code, str(error)) from error
        except BrokerRecordNotFoundError as error:
            raise UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.") from error
        except EnvironmentMismatchError as error:
            raise UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.") from error
        except ValueError as error:
            raise UseCaseError("BROKER_CONFIGURATION", str(error)) from error


class ViewHistoricCandlesUsecase:
    """Read an external instrument candle interval with stable range and broker errors."""

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
        """Return candles by external instrument ID; reject an invalid range or known broker failure."""
        if start.tzinfo is None or end.tzinfo is None or start >= end or end - start > timedelta(days=31):
            raise UseCaseError(
                "INVALID_CANDLE_RANGE",
                "Некорректный период свечей.",
                (FieldError("from", "INVALID_RANGE", "Проверьте начало и длину периода."),),
            )
        try:
            return await self._service.candles(broker_id, instrument_id, start, end, interval)
        except TInvestAdapterError as error:
            raise UseCaseError(error.code, str(error)) from error
        except BrokerRecordNotFoundError as error:
            raise UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.") from error
        except EnvironmentMismatchError as error:
            raise UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.") from error
        except ValueError as error:
            raise UseCaseError("BROKER_CONFIGURATION", str(error)) from error
