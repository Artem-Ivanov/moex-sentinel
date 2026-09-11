import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from moex_sentinel.domain.market_data import CandleInterval
from moex_sentinel.usecases.errors import UseCaseError
from moex_sentinel.usecases.market_data import (
    SearchMarketInstrumentsUsecase,
    ViewHistoricCandlesUsecase,
)


class Service:
    async def search(self, broker_id: str, query: str, limit: int):
        return broker_id, query, limit

    async def instrument(self, broker_id: str, instrument_id: str):
        return broker_id, instrument_id

    async def candles(self, broker_id, instrument_id, start, end, interval):
        return broker_id, instrument_id, start, end, interval


def test_search_usecase_normalizes_query_before_service() -> None:
    result = asyncio.run(SearchMarketInstrumentsUsecase(Service()).execute("broker-1", "  sber  ", 20))

    assert result == ("broker-1", "sber", 20)


@pytest.mark.parametrize(("query", "limit"), [(" ", 10), ("S", 10), ("SBER", 0), ("SBER", 51)])
def test_search_usecase_rejects_invalid_input_with_field_error(query: str, limit: int) -> None:
    with pytest.raises(UseCaseError) as caught:
        asyncio.run(SearchMarketInstrumentsUsecase(Service()).execute("broker-1", query, limit))

    assert caught.value.code == "INVALID_MARKET_QUERY"
    assert caught.value.fields[0].path in {"query", "limit"}


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 8, 5, 10, tzinfo=UTC), datetime(2026, 8, 5, 9, tzinfo=UTC)),
        (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 5, tzinfo=UTC)),
        (datetime(2026, 8, 5), datetime(2026, 8, 5) + timedelta(hours=1)),
    ],
)
def test_candles_usecase_rejects_invalid_range(start: datetime, end: datetime) -> None:
    usecase = ViewHistoricCandlesUsecase(Service())

    with pytest.raises(UseCaseError) as caught:
        asyncio.run(usecase.execute("broker-1", "uid-1", start, end, CandleInterval.MIN_5))

    assert caught.value.code == "INVALID_CANDLE_RANGE"
    assert caught.value.fields[0].path == "from"
