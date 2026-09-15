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
    InstrumentCategoryError,
    InstrumentLotPriceRangeError,
)
from moex_sentinel.services.position_adoption import ConfiguredPositionAdoptionPort
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


class SynchronizeBrokerInstrumentsUsecase:
    """Synchronize the catalog and optional adoption, preserving declared application errors."""

    def __init__(
        self,
        service: InstrumentCatalogServicePort,
        position_adoption: ConfiguredPositionAdoptionPort | None = None,
    ) -> None:
        self._service = service
        self._position_adoption = position_adoption

    async def execute(self, broker_id: str) -> CatalogReconciliationResult:
        """Return catalog reconciliation after optional adoption; translate known service failures."""
        try:
            result = await self._service.synchronize(broker_id)
            if self._position_adoption is not None:
                await self._position_adoption.adopt(broker_id)
            return result
        except CatalogInstrumentNotFoundError as error:
            raise UseCaseError("INSTRUMENT_NOT_FOUND", "Инструмент не найден.") from error
        except BrokerRecordNotFoundError as error:
            raise UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.") from error
        except TInvestAdapterError as error:
            raise UseCaseError(error.code, str(error)) from error
        except EnvironmentMismatchError as error:
            raise UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.") from error
        except InstrumentLotPriceRangeError as error:
            raise UseCaseError(
                "INVALID_LOT_PRICE_RANGE",
                str(error),
                (
                    FieldError("lot_price_from", "INVALID_RANGE", str(error)),
                    FieldError("lot_price_to", "INVALID_RANGE", str(error)),
                ),
            ) from error
        except ValueError as error:
            raise UseCaseError("BROKER_CONFIGURATION", str(error)) from error


class ViewBrokerInstrumentsUsecase:
    """Read catalog selections with stable category, range and broker error payloads."""

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
        """Return filtered catalog instruments; report invalid filters and known broker failures."""
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
        except InstrumentCategoryError as error:
            raise UseCaseError(
                "INVALID_INSTRUMENT_CATEGORY",
                "Неизвестная категория инструментов.",
                (FieldError("category", "UNSUPPORTED", "Выберите доступную категорию."),),
            ) from error
        except CatalogInstrumentNotFoundError as error:
            raise UseCaseError("INSTRUMENT_NOT_FOUND", "Инструмент не найден.") from error
        except BrokerRecordNotFoundError as error:
            raise UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.") from error
        except TInvestAdapterError as error:
            raise UseCaseError(error.code, str(error)) from error
        except EnvironmentMismatchError as error:
            raise UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.") from error
        except InstrumentLotPriceRangeError as error:
            raise UseCaseError(
                "INVALID_LOT_PRICE_RANGE",
                str(error),
                (
                    FieldError("lot_price_from", "INVALID_RANGE", str(error)),
                    FieldError("lot_price_to", "INVALID_RANGE", str(error)),
                ),
            ) from error
        except ValueError as error:
            raise UseCaseError("BROKER_CONFIGURATION", str(error)) from error


class ViewInstrumentDetailsUsecase:
    """Read catalog details and translate declared lookup or broker failures."""

    def __init__(self, service: InstrumentCatalogServicePort) -> None:
        self._service = service

    async def execute(self, broker_id: str, instrument_id: str) -> InstrumentDetailsView:
        """Return catalog instrument details; translate known lookup and broker failures."""
        try:
            return await self._service.details(broker_id, instrument_id)
        except CatalogInstrumentNotFoundError as error:
            raise UseCaseError("INSTRUMENT_NOT_FOUND", "Инструмент не найден.") from error
        except BrokerRecordNotFoundError as error:
            raise UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.") from error
        except TInvestAdapterError as error:
            raise UseCaseError(error.code, str(error)) from error
        except EnvironmentMismatchError as error:
            raise UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.") from error
        except InstrumentLotPriceRangeError as error:
            raise UseCaseError(
                "INVALID_LOT_PRICE_RANGE",
                str(error),
                (
                    FieldError("lot_price_from", "INVALID_RANGE", str(error)),
                    FieldError("lot_price_to", "INVALID_RANGE", str(error)),
                ),
            ) from error
        except ValueError as error:
            raise UseCaseError("BROKER_CONFIGURATION", str(error)) from error


class SetInstrumentSelectionUsecase:
    """Update catalog selection and translate declared service failures."""

    def __init__(self, service: InstrumentCatalogServicePort) -> None:
        self._service = service

    def execute(self, broker_id: str, instrument_id: str, selected: bool) -> CatalogInstrument:
        """Return the updated catalog instrument; translate known lookup and broker failures."""
        try:
            return self._service.set_selected(broker_id, instrument_id, selected)
        except CatalogInstrumentNotFoundError as error:
            raise UseCaseError("INSTRUMENT_NOT_FOUND", "Инструмент не найден.") from error
        except BrokerRecordNotFoundError as error:
            raise UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.") from error
        except TInvestAdapterError as error:
            raise UseCaseError(error.code, str(error)) from error
        except EnvironmentMismatchError as error:
            raise UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.") from error
        except InstrumentLotPriceRangeError as error:
            raise UseCaseError(
                "INVALID_LOT_PRICE_RANGE",
                str(error),
                (
                    FieldError("lot_price_from", "INVALID_RANGE", str(error)),
                    FieldError("lot_price_to", "INVALID_RANGE", str(error)),
                ),
            ) from error
        except ValueError as error:
            raise UseCaseError("BROKER_CONFIGURATION", str(error)) from error
