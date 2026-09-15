import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from grpc import StatusCode
from t_tech.invest.exceptions import AioRequestError

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.adapters.tinvest.market_data import TInvestMarketDataAdapter
from moex_sentinel.adapters.tinvest.portfolio import SANDBOX_TARGET
from moex_sentinel.domain.market_data import CandleInterval


def quotation(units: int, nano: int = 0) -> SimpleNamespace:
    return SimpleNamespace(units=units, nano=nano)


class FakeInstrumentsService:
    async def shares(self, *, instrument_status):
        assert instrument_status.name == "INSTRUMENT_STATUS_BASE"
        return SimpleNamespace(instruments=[self._listed("uid-share", "SBER", "share")])

    async def bonds(self, *, instrument_status):
        return SimpleNamespace(instruments=[self._listed("uid-bond", "SU", "bond")])

    async def currencies(self, *, instrument_status):
        return SimpleNamespace(instruments=[self._listed("uid-currency", "CNYRUB", "currency")])

    async def futures(self, *, instrument_status):
        return SimpleNamespace(instruments=[self._listed("uid-future", "Si", "future")])

    async def etfs(self, *, instrument_status):
        return SimpleNamespace(instruments=[self._listed("uid-etf", "TMOS", "etf")])

    @staticmethod
    def _listed(uid: str, ticker: str, kind: str):
        return SimpleNamespace(
            uid=uid,
            figi=f"figi-{uid}",
            ticker=ticker,
            name=ticker,
            class_code="TQBR",
            instrument_kind="INSTRUMENT_TYPE_UNSPECIFIED",
            instrument_type=kind,
            currency="rub",
            lot=1,
            min_price_increment=quotation(0, 10_000_000),
            api_trade_available_flag=True,
        )

    async def find_instrument(self, *, query: str):
        assert query == "SBER"
        return SimpleNamespace(
            instruments=[
                SimpleNamespace(
                    uid="uid-1",
                    figi="figi-1",
                    ticker="SBER",
                    name="Sber",
                    class_code="TQBR",
                    instrument_type="share",
                    instrument_kind="INSTRUMENT_TYPE_SHARE",
                    lot=10,
                    min_price_increment=quotation(0, 10_000_000),
                    api_trade_available_flag=True,
                )
            ]
        )

    async def get_instrument_by(self, *, id_type, id: str):
        assert id == "uid-1"
        assert id_type.name == "INSTRUMENT_ID_TYPE_UID"
        return SimpleNamespace(
            instrument=SimpleNamespace(
                uid="uid-1",
                figi="figi-1",
                ticker="SBER",
                name="Sber",
                class_code="TQBR",
                instrument_type="share",
                instrument_kind="INSTRUMENT_TYPE_SHARE",
                currency="rub",
                lot=10,
                min_price_increment=quotation(0, 10_000_000),
                api_trade_available_flag=True,
            )
        )


class FakeMarketDataService:
    async def get_last_prices(self, *, instrument_id):
        assert instrument_id == ["uid-1"]
        return SimpleNamespace(
            last_prices=[
                SimpleNamespace(
                    instrument_uid="uid-1",
                    price=quotation(312, 450_000_000),
                    time=datetime(2026, 8, 5, 10, tzinfo=UTC),
                )
            ]
        )

    async def get_candles(self, **kwargs):
        assert kwargs["instrument_id"] == "uid-1"
        assert kwargs["interval"].name == "CANDLE_INTERVAL_5_MIN"
        return SimpleNamespace(
            candles=[
                SimpleNamespace(
                    open=quotation(310),
                    high=quotation(313),
                    low=quotation(309, 500_000_000),
                    close=quotation(312, 450_000_000),
                    volume=1250,
                    time=datetime(2026, 8, 5, 9, 55, tzinfo=UTC),
                    is_complete=True,
                )
            ]
        )


class FakeContext:
    async def __aenter__(self):
        return SimpleNamespace(
            instruments=FakeInstrumentsService(),
            market_data=FakeMarketDataService(),
        )

    async def __aexit__(self, *_args):
        return None


def adapter() -> TInvestMarketDataAdapter:
    return TInvestMarketDataAdapter(
        "synthetic-token",
        SANDBOX_TARGET,
        client_factory=lambda *_args, **_kwargs: FakeContext(),
    )


def test_adapter_maps_search_and_instrument_card_without_sdk_types() -> None:
    search_result = asyncio.run(adapter().search_instruments("SBER"))
    instrument = asyncio.run(adapter().get_instrument("uid-1"))

    assert search_result[0].instrument_id == "uid-1"
    assert search_result[0].ticker == "SBER"
    assert search_result[0].currency is None
    assert search_result[0].lot == 10
    assert instrument.currency == "RUB"


def test_adapter_maps_last_prices_and_historic_candles() -> None:
    start = datetime(2026, 8, 5, 9, tzinfo=UTC)
    end = datetime(2026, 8, 5, 10, tzinfo=UTC)

    prices = asyncio.run(adapter().get_last_prices(("uid-1",)))
    candles = asyncio.run(adapter().get_candles("uid-1", start, end, CandleInterval.MIN_5))

    assert prices[0].price == Decimal("312.45")
    assert prices[0].captured_at == end
    assert candles[0].open == Decimal("310")
    assert candles[0].low == Decimal("309.5")
    assert candles[0].volume == 1250
    assert candles[0].is_complete is True


def test_adapter_loads_complete_supported_instrument_catalog() -> None:
    result = asyncio.run(adapter().list_instruments())

    assert [item.instrument_id for item in result] == [
        "uid-share",
        "uid-bond",
        "uid-currency",
        "uid-future",
        "uid-etf",
    ]
    assert all(item.currency == "RUB" for item in result)
    assert [item.instrument_type for item in result] == [
        "SHARE",
        "BOND",
        "CURRENCY",
        "FUTURE",
        "FUND",
    ]


def test_catalog_logging_identifies_failed_sdk_group_without_exception_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingInstrumentsService(FakeInstrumentsService):
        async def bonds(self, *, instrument_status):
            raise AioRequestError(StatusCode.UNAVAILABLE, "transport detail must stay hidden", metadata=None)

    class FailingContext(FakeContext):
        async def __aenter__(self):
            return SimpleNamespace(
                instruments=FailingInstrumentsService(),
                market_data=FakeMarketDataService(),
            )

    failing = TInvestMarketDataAdapter(
        "synthetic-token",
        SANDBOX_TARGET,
        client_factory=lambda *_args, **_kwargs: FailingContext(),
    )

    with caplog.at_level(logging.INFO), pytest.raises(TInvestAdapterError):
        asyncio.run(failing.list_instruments())

    failure = next(record for record in caplog.records if record.levelno == logging.WARNING)
    assert failure.instrument_group == "bonds"
    assert failure.error_type == "AioRequestError"
    assert failure.grpc_status == "UNAVAILABLE"
    assert "transport detail" not in failure.getMessage()
