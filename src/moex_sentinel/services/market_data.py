"""Broker-scoped business operations over read-only market data."""

from collections.abc import Callable
from datetime import datetime

from moex_sentinel.domain.market_data import (
    CandleInterval,
    HistoricCandle,
    InstrumentMarketSnapshot,
)
from moex_sentinel.domain.user_brokers import UserBroker
from moex_sentinel.services.environment import EnvironmentMismatchError, EnvironmentStatePort
from moex_sentinel.services.market_data_ports import MarketDataPort

MarketDataAdapterFactory = Callable[[UserBroker], MarketDataPort]


class UserBrokerRepositoryPort:
    def get(self, user_broker_id: str) -> UserBroker: ...


class BrokerMarketDataService:
    def __init__(
        self,
        brokers: UserBrokerRepositoryPort,
        adapter_factory: MarketDataAdapterFactory,
        environment: EnvironmentStatePort | None = None,
    ) -> None:
        self._brokers = brokers
        self._adapter_factory = adapter_factory
        self._environment = environment

    async def search(self, broker_id: str, query: str, limit: int) -> tuple[InstrumentMarketSnapshot, ...]:
        adapter = self._adapter(broker_id)
        instruments = tuple(item for item in await adapter.search_instruments(query) if item.api_trade_available)[
            :limit
        ]
        prices = {
            item.instrument_id: item
            for item in await adapter.get_last_prices(tuple(instrument.instrument_id for instrument in instruments))
        }
        return tuple(
            InstrumentMarketSnapshot(instrument, prices.get(instrument.instrument_id)) for instrument in instruments
        )

    async def instrument(self, broker_id: str, instrument_id: str) -> InstrumentMarketSnapshot:
        adapter = self._adapter(broker_id)
        instrument = await adapter.get_instrument(instrument_id)
        prices = await adapter.get_last_prices((instrument_id,))
        return InstrumentMarketSnapshot(instrument, prices[0] if prices else None)

    async def candles(
        self,
        broker_id: str,
        instrument_id: str,
        start: datetime,
        end: datetime,
        interval: CandleInterval,
    ) -> tuple[HistoricCandle, ...]:
        candles = await self._adapter(broker_id).get_candles(instrument_id, start, end, interval)
        return tuple(item for item in candles if item.is_complete)

    def _adapter(self, broker_id: str) -> MarketDataPort:
        broker = self._brokers.get(broker_id)
        if not broker.enabled:
            raise ValueError("Подключение брокера отключено.")
        active_test = self._environment is None or self._environment.view().active_environment == "TEST"
        if broker.is_test is not active_test:
            raise EnvironmentMismatchError("Broker belongs to inactive environment.")
        return self._adapter_factory(broker)
