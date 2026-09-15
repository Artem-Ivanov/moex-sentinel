from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.domain.instrument_catalog import (
    CatalogCategory,
    CatalogInstrument,
    CatalogInstrumentPrice,
    CatalogListView,
    CatalogReconciliationResult,
    CatalogSyncState,
    InstrumentDetailsView,
)
from moex_sentinel.domain.market_data import LastPrice
from moex_sentinel.services.instrument_catalog import InstrumentCatalogService
from moex_sentinel.usecases.instruments import ViewBrokerInstrumentsUsecase

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)


def item(*, selected: bool = True) -> CatalogInstrument:
    return CatalogInstrument(
        id="row-1",
        broker_id="broker-1",
        instrument_id="uid-1",
        figi="figi-1",
        ticker="SBER",
        name="Sber",
        class_code="TQBR",
        instrument_type="INSTRUMENT_TYPE_SHARE",
        category="SHARE",
        currency="RUB",
        lot=10,
        min_price_increment=Decimal("0.01"),
        api_trade_available=True,
        is_active=True,
        is_selected=selected,
        first_seen_at=NOW,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


class Synchronize:
    async def execute(self, broker_id: str):
        return CatalogReconciliationResult(broker_id, 2, 1, 1, NOW)


class ListCatalog:
    async def execute(
        self,
        broker_id,
        category,
        include_inactive,
        selected_only,
        lot_price_from,
        lot_price_to,
        currency,
    ):
        assert (category, include_inactive, selected_only) == ("SHARE", False, True)
        assert (lot_price_from, lot_price_to) == (Decimal("3000"), Decimal("4000"))
        assert currency == "RUB"
        return CatalogListView(
            broker_id,
            (CatalogInstrumentPrice(item(), Decimal("312.45"), Decimal("3124.50"), NOW),),
            (CatalogCategory("SHARE", "Акции", 1),),
            CatalogSyncState(broker_id, "SUCCESS", NOW, NOW, None),
            ("RUB", "USD"),
        )


class Details:
    async def execute(self, broker_id, instrument_id):
        return InstrumentDetailsView(
            "Sandbox",
            item(),
            LastPrice(instrument_id=instrument_id, price=Decimal("312.45"), captured_at=NOW),
            Decimal("3124.50"),
            CatalogSyncState(broker_id, "SUCCESS", NOW, NOW, None),
        )


class Selection:
    def execute(self, broker_id, instrument_id, selected):
        return item(selected=selected)


@pytest.fixture
def catalog_client(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "moex_sentinel.api.app.build_application_usecases",
        lambda _factory: SimpleNamespace(
            synchronize_broker_instruments=Synchronize(),
            view_broker_instruments=ListCatalog(),
            view_instrument_details=Details(),
            set_instrument_selection=Selection(),
        ),
    )
    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'catalog.db'}")) as client:
        yield client


def test_catalog_synchronization_response(catalog_client):
    synchronized = catalog_client.post("/api/brokers/broker-1/instruments/synchronize")

    assert synchronized.json() == {
        "broker_id": "broker-1",
        "added": 2,
        "updated": 1,
        "deactivated": 1,
        "synchronized_at": "2026-08-05T12:00:00Z",
    }


def test_catalog_listing_filters_and_prices(catalog_client):
    listing = catalog_client.get(
        "/api/brokers/broker-1/instruments",
        params={"category": "SHARE", "selected_only": "true", "lot_price_from": "3000", "lot_price_to": "4000"},
    )

    assert listing.json()["items"][0]["is_selected"] is True
    assert listing.json()["items"][0]["unit_price"] == "312.45"
    assert listing.json()["items"][0]["lot_price"] == "3124.50"
    assert listing.json()["items"][0]["price_captured_at"] == "2026-08-05T12:00:00Z"
    assert listing.json()["categories"] == [{"code": "SHARE", "label": "Акции", "count": 1}]


def test_catalog_details_response(catalog_client):
    details = catalog_client.get("/api/brokers/broker-1/instruments/uid-1")

    assert details.json()["last_price"]["price"] == "312.45"
    assert details.json()["lot_price"] == "3124.50"


def test_catalog_selection_response(catalog_client):
    selected = catalog_client.put("/api/brokers/broker-1/instruments/uid-1/selection", json={"selected": False})

    assert selected.json()["is_selected"] is False


def test_invalid_category_precedes_range_validation_and_external_reads(monkeypatch, tmp_path):
    brokers, catalog, adapter_factory = Mock(), Mock(), Mock()
    usecase = ViewBrokerInstrumentsUsecase(InstrumentCatalogService(brokers, catalog, adapter_factory))
    monkeypatch.setattr(
        "moex_sentinel.api.app.build_application_usecases",
        lambda _factory: SimpleNamespace(view_broker_instruments=usecase),
    )
    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'invalid-category.db'}")) as client:
        response = client.get(
            "/api/brokers/broker-1/instruments",
            params={
                "category": "UNKNOWN",
                "lot_price_from": "500",
                "lot_price_to": "100",
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "INVALID_INSTRUMENT_CATEGORY",
            "message": "Неизвестная категория инструментов.",
            "fields": [{"path": "category", "code": "UNSUPPORTED", "message": "Выберите доступную категорию."}],
        }
    }
    assert brokers.mock_calls == catalog.mock_calls == adapter_factory.mock_calls == []
