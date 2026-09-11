from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.domain.market_data import (
    HistoricCandle,
    InstrumentMarketSnapshot,
    LastPrice,
    MarketInstrument,
)
from moex_sentinel.usecases.errors import FieldError, UseCaseError


class SearchUsecase:
    async def execute(self, broker_id: str, query: str, limit: int):
        assert (broker_id, query, limit) == ("broker-1", "SBER", 10)
        instrument = MarketInstrument(
            instrument_id="uid-1",
            figi="figi-1",
            ticker="SBER",
            name="Sber",
            class_code="TQBR",
            instrument_type="SHARE",
            currency="RUB",
            lot=10,
            min_price_increment=Decimal("0.01"),
            api_trade_available=True,
        )
        price = LastPrice(
            instrument_id="uid-1", price=Decimal("312.45"), captured_at=datetime(2026, 8, 5, 10, tzinfo=UTC)
        )
        return (InstrumentMarketSnapshot(instrument, price),)


class InstrumentUsecase:
    async def execute(self, broker_id: str, instrument_id: str):
        assert (broker_id, instrument_id) == ("broker-1", "uid-1")
        instrument = MarketInstrument(
            instrument_id="uid-1",
            figi="figi-1",
            ticker="SBER",
            name="Sber",
            class_code="TQBR",
            instrument_type="SHARE",
            currency="RUB",
            lot=10,
            min_price_increment=Decimal("0.01"),
            api_trade_available=True,
        )
        return InstrumentMarketSnapshot(instrument, None)


class CandlesUsecase:
    async def execute(self, broker_id, instrument_id, start, end, interval):
        assert broker_id == "broker-1"
        assert instrument_id == "uid-1"
        assert interval.value == "5_MIN"
        return (
            HistoricCandle(
                instrument_id="uid-1",
                open=Decimal("310"),
                high=Decimal("313"),
                low=Decimal("309.5"),
                close=Decimal("312.45"),
                volume=1250,
                started_at=datetime(2026, 8, 5, 9, 55, tzinfo=UTC),
                is_complete=True,
            ),
        )


def test_market_data_http_contracts(monkeypatch, tmp_path) -> None:
    usecases = SimpleNamespace(
        search_market_instruments=SearchUsecase(),
        view_market_instrument=InstrumentUsecase(),
        view_historic_candles=CandlesUsecase(),
    )
    monkeypatch.setattr("moex_sentinel.api.app.build_application_usecases", lambda _factory: usecases)

    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'market.db'}")) as client:
        search = client.get("/api/brokers/broker-1/market/instruments", params={"query": "SBER", "limit": 10})
        card = client.get("/api/brokers/broker-1/market/instruments/uid-1")
        candles = client.get(
            "/api/brokers/broker-1/market/instruments/uid-1/candles",
            params={
                "from": "2026-08-05T09:00:00Z",
                "to": "2026-08-05T10:00:00Z",
                "interval": "5_MIN",
            },
        )

    assert search.status_code == 200
    assert search.json() == {
        "items": [
            {
                "instrument_id": "uid-1",
                "figi": "figi-1",
                "ticker": "SBER",
                "name": "Sber",
                "class_code": "TQBR",
                "instrument_type": "SHARE",
                "currency": "RUB",
                "lot": 10,
                "api_trade_available": True,
                "last_price": {"price": "312.45", "captured_at": "2026-08-05T10:00:00Z"},
            }
        ]
    }
    assert card.status_code == 200
    assert card.json()["last_price"] is None
    assert candles.status_code == 200
    assert candles.json()["items"][0]["close"] == "312.45"


def test_market_search_validation_is_returned_as_field_error(monkeypatch, tmp_path) -> None:
    class InvalidSearchUsecase:
        async def execute(self, broker_id: str, query: str, limit: int):
            raise UseCaseError(
                "INVALID_MARKET_QUERY",
                "Проверьте запрос.",
                (FieldError("query", "MIN_LENGTH", "Введите минимум два символа."),),
            )

    usecases = SimpleNamespace(search_market_instruments=InvalidSearchUsecase())
    monkeypatch.setattr("moex_sentinel.api.app.build_application_usecases", lambda _factory: usecases)

    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'invalid-market.db'}")) as client:
        response = client.get("/api/brokers/broker-1/market/instruments", params={"query": "S", "limit": 20})

    assert response.status_code == 422
    assert response.json()["detail"]["fields"] == [
        {"path": "query", "code": "MIN_LENGTH", "message": "Введите минимум два символа."}
    ]
