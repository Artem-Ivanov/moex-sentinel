"""Business logic for broker-scoped instrument catalog synchronization."""

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from moex_sentinel.domain.instrument_catalog import (
    CatalogCategory,
    CatalogInstrument,
    CatalogInstrumentPrice,
    CatalogListView,
    CatalogReconciliationResult,
    CatalogSyncState,
    InstrumentDetailsView,
    UserBrokerCatalogInstrument,
    UserBrokerCatalogInstrumentDraft,
    UserBrokerCatalogReconciliationResult,
    UserBrokerCatalogSyncState,
)
from moex_sentinel.domain.market_data import CandleInterval, HistoricCandle, LastPrice
from moex_sentinel.domain.user_brokers import UserBroker
from moex_sentinel.services.environment import EnvironmentMismatchError, EnvironmentStatePort
from moex_sentinel.services.market_data_ports import MarketDataPort

CATEGORY_LABELS = {
    "SHARE": "Акции",
    "BOND": "Облигации",
    "CURRENCY": "Валюты",
    "FUTURE": "Фьючерсы",
    "FUND": "ETF/фонды",
    "OTHER": "Прочие",
}


class InstrumentLotPriceRangeError(ValueError):
    pass


class InstrumentCategoryError(ValueError):
    """The requested category is not part of the supported catalog taxonomy."""


class InstrumentCatalogRepositoryPort(Protocol):
    def reconcile(
        self,
        user_broker_id: str,
        snapshot: tuple[UserBrokerCatalogInstrumentDraft, ...],
        synchronized_at: datetime,
    ) -> UserBrokerCatalogReconciliationResult: ...

    def list(
        self,
        user_broker_id: str,
        *,
        include_inactive: bool,
    ) -> tuple[UserBrokerCatalogInstrument, ...]: ...

    def get(self, user_broker_id: str, instrument_id: str) -> UserBrokerCatalogInstrument: ...

    def set_selected(self, user_broker_id: str, instrument_id: str, selected: bool) -> UserBrokerCatalogInstrument: ...

    def sync_state(self, user_broker_id: str) -> UserBrokerCatalogSyncState: ...

    def mark_failed(self, broker_id: str, attempted_at: datetime, safe_error: str) -> None: ...


class UserBrokerRepositoryPort(Protocol):
    def get(self, user_broker_id: str) -> UserBroker: ...


class InstrumentCatalogService:
    def __init__(
        self,
        brokers: UserBrokerRepositoryPort,
        catalog: InstrumentCatalogRepositoryPort,
        adapter_factory: Callable[[UserBroker], MarketDataPort],
        environment: EnvironmentStatePort | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._brokers = brokers
        self._catalog = catalog
        self._adapter_factory = adapter_factory
        self._environment = environment
        self._clock = clock

    async def synchronize(self, broker_id: str) -> CatalogReconciliationResult:
        adapter = self._adapter(broker_id)
        try:
            external = await adapter.list_instruments()
        except Exception:
            self._catalog.mark_failed(
                broker_id,
                self._clock(),
                "Не удалось синхронизировать справочник.",  # noqa: RUF001
            )
            raise
        unique = {
            item.instrument_id: UserBrokerCatalogInstrumentDraft(
                external_instrument_id=item.instrument_id,
                external_identifiers={"figi": item.figi},
                ticker=item.ticker,
                name=item.name,
                class_code=item.class_code,
                instrument_type=item.instrument_type,
                currency=item.currency or "RUB",
                lot_size=item.lot,
                min_price_increment=item.min_price_increment,
                api_trade_available=item.api_trade_available,
            )
            for item in external
            if item.instrument_id.strip() and item.ticker.strip() and item.lot > 0 and item.min_price_increment > 0
        }
        result = self._catalog.reconcile(broker_id, tuple(unique.values()), self._clock())
        return CatalogReconciliationResult(
            broker_id=result.user_broker_id,
            added=result.added,
            updated=result.updated,
            deactivated=result.deactivated,
            synchronized_at=result.synchronized_at,
        )

    async def list(
        self,
        broker_id: str,
        category: str | None,
        include_inactive: bool,
        selected_only: bool,
        lot_price_from: Decimal | None,
        lot_price_to: Decimal | None,
        currency: str = "RUB",
    ) -> CatalogListView:
        """List priced catalog entries after validating category and range before any I/O."""
        if category is not None and category not in CATEGORY_LABELS:
            raise InstrumentCategoryError("Неизвестная категория инструментов.")
        if lot_price_from is not None and lot_price_from < 0:
            raise InstrumentLotPriceRangeError("Минимальная цена лота не может быть отрицательной.")
        if lot_price_to is not None and lot_price_to < 0:
            raise InstrumentLotPriceRangeError("Максимальная цена лота не может быть отрицательной.")
        if lot_price_from is not None and lot_price_to is not None and lot_price_from > lot_price_to:
            raise InstrumentLotPriceRangeError("Минимальная цена лота не может превышать максимальную.")
        broker = self._broker(broker_id)
        all_records = self._catalog.list(broker_id, include_inactive=include_inactive)
        all_items = tuple(self._instrument(item) for item in all_records)
        currencies = tuple(sorted({item.currency for item in all_items if item.currency}))
        normalized_currency = currency.strip().upper()
        currency_items = tuple(item for item in all_items if (item.currency or "").upper() == normalized_currency)
        counts = Counter(item.category for item in currency_items)
        categories = tuple(
            CatalogCategory(code, label, counts[code]) for code, label in CATEGORY_LABELS.items() if counts[code] > 0
        )
        instruments = tuple(
            item
            for item in all_items
            if (category is None or item.category == category) and (not selected_only or item.is_selected)
        )
        instruments = tuple(item for item in instruments if (item.currency or "").upper() == normalized_currency)
        adapter = self._adapter_factory(broker)
        prices: dict[str, LastPrice] = {}
        instrument_ids = tuple(item.instrument_id for item in instruments)
        for offset in range(0, len(instrument_ids), 300):
            batch = await adapter.get_last_prices(instrument_ids[offset : offset + 300])
            prices.update((price.instrument_id, price) for price in batch)
        items = []
        for instrument in instruments:
            price = prices.get(instrument.instrument_id)
            unit_price = None if price is None else price.price
            lot_price = None if unit_price is None else unit_price * instrument.lot
            if lot_price_from is not None and (lot_price is None or lot_price < lot_price_from):
                continue
            if lot_price_to is not None and (lot_price is None or lot_price > lot_price_to):
                continue
            items.append(
                CatalogInstrumentPrice(
                    instrument,
                    unit_price,
                    lot_price,
                    None if price is None else price.captured_at,
                )
            )
        return CatalogListView(
            broker_id=broker_id,
            items=tuple(items),
            categories=categories,
            sync_state=self._sync_state(self._catalog.sync_state(broker_id)),
            currencies=currencies,
        )

    async def details(self, broker_id: str, instrument_id: str) -> InstrumentDetailsView:
        broker = self._broker(broker_id)
        record = self._catalog.get(broker_id, instrument_id)
        instrument = self._instrument(record)
        prices = await self._adapter_factory(broker).get_last_prices((record.external_instrument_id,))
        return InstrumentDetailsView(
            broker_name=broker.display_name,
            instrument=instrument,
            last_price=prices[0] if prices else None,
            lot_price=None if not prices else prices[0].price * instrument.lot,
            sync_state=self._sync_state(self._catalog.sync_state(broker_id)),
        )

    async def candles(
        self,
        broker_id: str,
        instrument_id: str,
        start: datetime,
        end: datetime,
        interval: CandleInterval,
    ) -> tuple[HistoricCandle, ...]:
        """Read completed candles for an internal catalog ID in the broker scope."""
        broker = self._broker(broker_id)
        record = self._catalog.get(broker_id, instrument_id)
        candles = await self._adapter_factory(broker).get_candles(record.external_instrument_id, start, end, interval)
        return tuple(candle for candle in candles if candle.is_complete)

    def set_selected(self, broker_id: str, instrument_id: str, selected: bool) -> CatalogInstrument:
        self._broker(broker_id)
        return self._instrument(self._catalog.set_selected(broker_id, instrument_id, selected))

    def _adapter(self, broker_id: str) -> MarketDataPort:
        return self._adapter_factory(self._broker(broker_id))

    def _broker(self, broker_id: str) -> UserBroker:
        broker = self._brokers.get(broker_id)
        if not broker.enabled:
            raise ValueError("Подключение брокера отключено.")
        active_test = self._environment is None or self._environment.view().active_environment == "TEST"
        if broker.is_test is not active_test:
            raise EnvironmentMismatchError("Broker belongs to inactive environment.")
        return broker

    @classmethod
    def _instrument(cls, value: UserBrokerCatalogInstrument) -> CatalogInstrument:
        return CatalogInstrument(
            id=value.id,
            broker_id=value.user_broker_id,
            instrument_id=value.external_instrument_id,
            figi=str(value.external_identifiers.get("figi", "")),
            ticker=value.ticker,
            name=value.name,
            class_code=value.class_code,
            instrument_type=value.instrument_type,
            category=cls._category(value.instrument_type),
            currency=value.currency,
            lot=value.lot_size,
            min_price_increment=value.min_price_increment,
            api_trade_available=value.api_trade_available,
            is_active=value.is_active,
            is_selected=value.is_selected,
            first_seen_at=value.first_seen_at,
            last_seen_at=value.last_seen_at,
            created_at=value.created_at,
            updated_at=value.updated_at,
        )

    @staticmethod
    def _sync_state(value: UserBrokerCatalogSyncState) -> CatalogSyncState:
        return CatalogSyncState(
            broker_id=value.user_broker_id,
            status=value.status,
            last_attempt_at=value.last_attempt_at,
            last_success_at=value.last_success_at,
            safe_error=value.safe_error,
        )

    @staticmethod
    def _category(instrument_type: str) -> str:
        normalized = instrument_type.upper()
        if "SHARE" in normalized:
            return "SHARE"
        if "BOND" in normalized:
            return "BOND"
        if "CURRENCY" in normalized:
            return "CURRENCY"
        if "FUTURE" in normalized:
            return "FUTURE"
        if "ETF" in normalized or "FUND" in normalized:
            return "FUND"
        return "OTHER"
