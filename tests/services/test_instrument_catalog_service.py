import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.domain.brokers import Broker, BrokerField
from moex_sentinel.domain.environment import EnvironmentState
from moex_sentinel.domain.instrument_catalog import (
    UserBrokerCatalogInstrument,
    UserBrokerCatalogReconciliationResult,
    UserBrokerCatalogSyncState,
)
from moex_sentinel.domain.market_data import LastPrice, MarketInstrument
from moex_sentinel.services.instrument_catalog import (
    InstrumentCatalogService,
    InstrumentLotPriceRangeError,
)

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)


def broker() -> Broker:
    return Broker(
        id="broker-1",
        display_name="Sandbox",
        provider_code="TINVEST",
        environment_code="SANDBOX",
        adapter_code="TINVEST_SANDBOX",
        enabled=True,
        fields=(BrokerField(name="token", value="synthetic-token"),),
        created_at=NOW,
        updated_at=NOW,
    )


class Brokers:
    def get(self, broker_id: str) -> Broker:
        return broker()


class Environment:
    def view(self) -> EnvironmentState:
        return EnvironmentState("TEST", True, True)


class Adapter:
    async def list_instruments(self):
        return (
            MarketInstrument(
                instrument_id="share",
                figi="f1",
                ticker="AAA",
                name="Share",
                class_code="TQBR",
                instrument_type="INSTRUMENT_TYPE_SHARE",
                currency="RUB",
                lot=10,
                min_price_increment=Decimal("0.01"),
                api_trade_available=True,
            ),
            MarketInstrument(
                instrument_id="bond",
                figi="f2",
                ticker="BBB",
                name="Bond",
                class_code="TQOB",
                instrument_type="INSTRUMENT_TYPE_BOND",
                currency="RUB",
                lot=1,
                min_price_increment=Decimal("0.01"),
                api_trade_available=True,
            ),
            MarketInstrument(
                instrument_id="duplicate",
                figi="f3",
                ticker="",
                name="Invalid",
                class_code="",
                instrument_type="INSTRUMENT_TYPE_SHARE",
                currency=None,
                lot=1,
                min_price_increment=Decimal("0.01"),
                api_trade_available=True,
            ),
            MarketInstrument(
                instrument_id="zero-price-increment",
                figi="f4",
                ticker="ZERO",
                name="Invalid price increment",
                class_code="TQBR",
                instrument_type="INSTRUMENT_TYPE_SHARE",
                currency="RUB",
                lot=1,
                min_price_increment=Decimal("0"),
                api_trade_available=True,
            ),
        )

    async def get_last_prices(self, instrument_ids):
        prices = {"share": Decimal("12.5"), "bond": Decimal("20")}
        return tuple(
            LastPrice(instrument_id=instrument_id, price=prices[instrument_id], captured_at=NOW)
            for instrument_id in instrument_ids
            if instrument_id in prices
        )


class Catalog:
    def __init__(self) -> None:
        self.snapshot = ()
        self.selected = False
        self.failure = None

    def reconcile(self, broker_id, snapshot, synchronized_at):
        self.snapshot = snapshot
        return UserBrokerCatalogReconciliationResult(
            user_broker_id=broker_id,
            added=len(snapshot),
            updated=0,
            deactivated=0,
            synchronized_at=synchronized_at,
        )

    def list(self, broker_id, *, include_inactive):
        items = (
            self._record("share", "SHARE", selected=True),
            self._record("bond", "BOND", selected=False),
        )
        return items

    def get(self, broker_id, instrument_id):
        return self._record(instrument_id, "SHARE", selected=self.selected)

    def set_selected(self, broker_id, instrument_id, selected):
        self.selected = selected
        return self.get(broker_id, instrument_id)

    def sync_state(self, broker_id):
        return UserBrokerCatalogSyncState(broker_id, "SUCCESS", NOW, NOW, None, False)

    def mark_failed(self, broker_id, attempted_at, safe_error):
        self.failure = (broker_id, attempted_at, safe_error)

    @staticmethod
    def _record(uid: str, category: str, *, selected: bool) -> UserBrokerCatalogInstrument:
        return UserBrokerCatalogInstrument(
            id=f"row-{uid}",
            user_broker_id="broker-1",
            external_instrument_id=uid,
            external_identifiers={"figi": f"figi-{uid}"},
            ticker=uid.upper(),
            name=uid,
            class_code="TQBR",
            instrument_type=f"INSTRUMENT_TYPE_{category}",
            currency="RUB",
            lot_size=10 if uid == "share" else 1,
            min_price_increment=Decimal("0.01"),
            api_trade_available=True,
            is_active=True,
            is_selected=selected,
            first_seen_at=NOW,
            last_seen_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )


def service(catalog: Catalog | None = None) -> InstrumentCatalogService:
    return InstrumentCatalogService(
        Brokers(), catalog or Catalog(), lambda _broker: Adapter(), Environment(), clock=lambda: NOW
    )


def test_synchronize_normalizes_categories_and_rejects_invalid_rows() -> None:
    catalog = Catalog()
    result = asyncio.run(service(catalog).synchronize("broker-1"))

    assert result.added == 2
    assert [item.instrument_type for item in catalog.snapshot] == [
        "INSTRUMENT_TYPE_SHARE",
        "INSTRUMENT_TYPE_BOND",
    ]


def test_list_uses_backend_category_and_selected_filters_with_counts() -> None:
    result = asyncio.run(
        service().list(
            "broker-1",
            category="SHARE",
            include_inactive=False,
            selected_only=True,
            lot_price_from=None,
            lot_price_to=None,
        )
    )

    assert [item.instrument.instrument_id for item in result.items] == ["share"]
    assert result.items[0].unit_price == Decimal("12.5")
    assert result.items[0].lot_price == Decimal("125")
    assert result.items[0].price_captured_at == NOW
    assert [(item.code, item.count) for item in result.categories] == [("SHARE", 1), ("BOND", 1)]


def test_list_filters_by_inclusive_lot_price_range_and_excludes_missing_prices() -> None:
    result = asyncio.run(
        service().list(
            "broker-1",
            category=None,
            include_inactive=False,
            selected_only=False,
            lot_price_from=Decimal("20"),
            lot_price_to=Decimal("125"),
        )
    )

    assert [item.instrument.instrument_id for item in result.items] == ["share", "bond"]

    class MissingBondPriceAdapter(Adapter):
        async def get_last_prices(self, instrument_ids):
            return (LastPrice(instrument_id="share", price=Decimal("12.5"), captured_at=NOW),)

    filtered = InstrumentCatalogService(
        Brokers(),
        Catalog(),
        lambda _broker: MissingBondPriceAdapter(),
        Environment(),
        clock=lambda: NOW,
    )
    missing = asyncio.run(
        filtered.list(
            "broker-1",
            category=None,
            include_inactive=False,
            selected_only=False,
            lot_price_from=Decimal("0"),
            lot_price_to=None,
        )
    )

    assert [item.instrument.instrument_id for item in missing.items] == ["share"]


def test_list_rejects_reversed_lot_price_range() -> None:
    with pytest.raises(InstrumentLotPriceRangeError):
        asyncio.run(
            service().list(
                "broker-1",
                category=None,
                include_inactive=False,
                selected_only=False,
                lot_price_from=Decimal("500"),
                lot_price_to=Decimal("100"),
            )
        )


def test_details_joins_current_price_and_selection_is_persisted() -> None:
    catalog = Catalog()
    selected = service(catalog).set_selected("broker-1", "share", True)
    details = asyncio.run(service(catalog).details("broker-1", "share"))

    assert selected.is_selected is True
    assert details.broker_name == "Sandbox"
    assert details.last_price is not None
    assert details.last_price.price == Decimal("12.5")
    assert details.lot_price == Decimal("125")


def test_synchronize_persists_safe_failure_state_without_transport_details() -> None:
    class FailingAdapter(Adapter):
        async def list_instruments(self):
            raise TInvestAdapterError("BROKER_UNAVAILABLE", "Площадка недоступна.", retryable=True)

    catalog = Catalog()
    failing = InstrumentCatalogService(
        Brokers(), catalog, lambda _broker: FailingAdapter(), Environment(), clock=lambda: NOW
    )

    with pytest.raises(TInvestAdapterError):
        asyncio.run(failing.synchronize("broker-1"))

    assert catalog.failure == (
        "broker-1",
        NOW,
        "Не удалось синхронизировать справочник.",  # noqa: RUF001
    )
