import asyncio
from decimal import Decimal

import pytest

from moex_sentinel.domain.instrument_catalog import CatalogInstrumentNotFoundError
from moex_sentinel.services.instrument_catalog import InstrumentLotPriceRangeError
from moex_sentinel.usecases.errors import UseCaseError
from moex_sentinel.usecases.instruments import (
    SynchronizeBrokerInstrumentsUsecase,
    ViewBrokerInstrumentsUsecase,
    ViewInstrumentDetailsUsecase,
)


def test_successful_catalog_sync_invokes_position_adoption_after_commit() -> None:
    calls: list[str] = []

    class Catalog:
        async def synchronize(self, broker_id: str):
            calls.append("catalog")
            return "synchronized"

    class Adoption:
        async def adopt(self, broker_id: str):
            calls.append("adoption")

    result = asyncio.run(SynchronizeBrokerInstrumentsUsecase(Catalog(), Adoption()).execute("broker-1"))

    assert result == "synchronized"
    assert calls == ["catalog", "adoption"]


def test_failed_catalog_sync_never_invokes_position_adoption() -> None:
    called = False

    class Catalog:
        async def synchronize(self, broker_id: str):
            raise ValueError("catalog failed")

    class Adoption:
        async def adopt(self, broker_id: str):
            nonlocal called
            called = True

    with pytest.raises(UseCaseError):
        asyncio.run(SynchronizeBrokerInstrumentsUsecase(Catalog(), Adoption()).execute("broker-1"))

    assert called is False


class MissingService:
    async def details(self, broker_id: str, instrument_id: str):
        raise CatalogInstrumentNotFoundError(instrument_id)


def test_instrument_details_maps_missing_catalog_record_to_readable_error() -> None:
    with pytest.raises(UseCaseError) as caught:
        asyncio.run(ViewInstrumentDetailsUsecase(MissingService()).execute("broker-1", "missing"))

    assert caught.value.code == "INSTRUMENT_NOT_FOUND"
    assert "missing" not in caught.value.message


def test_instrument_list_passes_lot_price_range_to_service() -> None:
    class ListingService:
        def __init__(self) -> None:
            self.received = None

        async def list(self, *args):
            self.received = args
            return "catalog"

    listing = ListingService()
    result = asyncio.run(
        ViewBrokerInstrumentsUsecase(listing).execute(
            "broker-1",
            "SHARE",
            False,
            True,
            Decimal("100"),
            Decimal("500"),
        )
    )

    assert result == "catalog"
    assert listing.received == (
        "broker-1",
        "SHARE",
        False,
        True,
        Decimal("100"),
        Decimal("500"),
        "RUB",
    )


def test_instrument_list_maps_invalid_price_range_to_field_errors() -> None:
    class InvalidRangeService:
        async def list(self, *args):
            raise InstrumentLotPriceRangeError("Некорректный диапазон цены лота.")

    with pytest.raises(UseCaseError) as caught:
        asyncio.run(
            ViewBrokerInstrumentsUsecase(InvalidRangeService()).execute(
                "broker-1", None, False, False, Decimal("500"), Decimal("100")
            )
        )

    assert caught.value.code == "INVALID_LOT_PRICE_RANGE"
    assert [error.path for error in caught.value.fields] == [
        "lot_price_from",
        "lot_price_to",
    ]
