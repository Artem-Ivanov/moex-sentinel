"""User intents for the local broker instrument catalog."""

from decimal import Decimal
from typing import Protocol

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.domain.brokers import BrokerRecordNotFoundError
from moex_sentinel.domain.errors import FieldError
from moex_sentinel.domain.instrument_catalog import (
    CatalogInstrument,
    CatalogInstrumentNotFoundError,
    CatalogListView,
    CatalogReconciliationResult,
    InstrumentDetailsView,
)
from moex_sentinel.services.environment import EnvironmentMismatchError
from moex_sentinel.services.instrument_catalog import (
    CATEGORY_LABELS,
    InstrumentLotPriceRangeError,
)
from moex_sentinel.usecases.errors import UseCaseError


class InstrumentCatalogServicePort(Protocol):
    async def synchronize(self, broker_id: str) -> CatalogReconciliationResult: ...

    async def list(
        self,
        broker_id: str,
        category: str | None,
        include_inactive: bool,
        selected_only: bool,
        lot_price_from: Decimal | None,
        lot_price_to: Decimal | None,
        currency: str,
    ) -> CatalogListView: ...

    async def details(self, broker_id: str, instrument_id: str) -> InstrumentDetailsView: ...

    def set_selected(self, broker_id: str, instrument_id: str, selected: bool) -> CatalogInstrument: ...


class PositionAdoptionUsecasePort(Protocol):
    async def execute(self, broker_id: str) -> object: ...


class SynchronizeBrokerInstrumentsUsecase:
    def __init__(
        self,
        service: InstrumentCatalogServicePort,
        position_adoption: PositionAdoptionUsecasePort | None = None,
    ) -> None:
        self._service = service
        self._position_adoption = position_adoption

    async def execute(self, broker_id: str) -> CatalogReconciliationResult:
        try:
            result = await self._service.synchronize(broker_id)
            if self._position_adoption is not None:
                await self._position_adoption.execute(broker_id)
            return result
        except Exception as error:
            raise _catalog_error(error) from error


class ViewBrokerInstrumentsUsecase:
    def __init__(self, service: InstrumentCatalogServicePort) -> None:
        self._service = service

    async def execute(
        self,
        broker_id: str,
        category: str | None,
        include_inactive: bool,
        selected_only: bool,
        lot_price_from: Decimal | None,
        lot_price_to: Decimal | None,
        currency: str = "RUB",
    ) -> CatalogListView:
        if category is not None and category not in CATEGORY_LABELS:
            raise UseCaseError(
                "INVALID_INSTRUMENT_CATEGORY",
                "Неизвестная категория инструментов.",
                (FieldError("category", "UNSUPPORTED", "Выберите доступную категорию."),),
            )
        try:
            return await self._service.list(
                broker_id,
                category,
                include_inactive,
                selected_only,
                lot_price_from,
                lot_price_to,
                currency,
            )
        except Exception as error:
            raise _catalog_error(error) from error


class ViewInstrumentDetailsUsecase:
    def __init__(self, service: InstrumentCatalogServicePort) -> None:
        self._service = service

    async def execute(self, broker_id: str, instrument_id: str) -> InstrumentDetailsView:
        try:
            return await self._service.details(broker_id, instrument_id)
        except Exception as error:
            raise _catalog_error(error) from error


class SetInstrumentSelectionUsecase:
    def __init__(self, service: InstrumentCatalogServicePort) -> None:
        self._service = service

    def execute(self, broker_id: str, instrument_id: str, selected: bool) -> CatalogInstrument:
        try:
            return self._service.set_selected(broker_id, instrument_id, selected)
        except Exception as error:
            raise _catalog_error(error) from error


def _catalog_error(error: Exception) -> UseCaseError:
    if isinstance(error, CatalogInstrumentNotFoundError):
        return UseCaseError("INSTRUMENT_NOT_FOUND", "Инструмент не найден.")
    if isinstance(error, BrokerRecordNotFoundError):
        return UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.")
    if isinstance(error, TInvestAdapterError):
        return UseCaseError(error.code, str(error))
    if isinstance(error, EnvironmentMismatchError):
        return UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.")
    if isinstance(error, InstrumentLotPriceRangeError):
        return UseCaseError(
            "INVALID_LOT_PRICE_RANGE",
            str(error),
            (
                FieldError("lot_price_from", "INVALID_RANGE", str(error)),
                FieldError("lot_price_to", "INVALID_RANGE", str(error)),
            ),
        )
    if isinstance(error, ValueError):
        return UseCaseError("BROKER_CONFIGURATION", str(error))
    raise error
