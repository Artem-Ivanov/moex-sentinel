"""Validation and idempotency orchestration for broker-position adoption."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from moex_sentinel.domain.instrument_catalog import UserBrokerCatalogInstrument
from moex_sentinel.domain.portfolio import ActiveBrokerOrder, ExternalPosition, Money
from moex_sentinel.domain.position_adoption import PositionAdoptionReason, PositionAdoptionWriteResult
from moex_sentinel.services.position_adoption import PositionAdoptionService

NOW = datetime(2026, 8, 14, 10, 0, 0, 123000, tzinfo=UTC)


def instrument() -> UserBrokerCatalogInstrument:
    return UserBrokerCatalogInstrument(
        id="00000000-0000-4000-8000-000000000102",
        user_broker_id="00000000-0000-4000-8000-000000000101",
        external_instrument_id="external-1",
        external_identifiers={},
        ticker="TEST",
        name="Synthetic instrument",
        instrument_type="SHARE",
        class_code="TQBR",
        currency="RUB",
        lot_size=10,
        min_price_increment=Decimal("0.01"),
        api_trade_available=True,
        is_active=True,
        is_selected=True,
        first_seen_at=NOW,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


class Broker:
    def __init__(self, positions: tuple[ExternalPosition, ...], orders: tuple[ActiveBrokerOrder, ...] = ()) -> None:
        self.positions = positions
        self.orders = orders

    async def get_positions(self, account_id: str) -> tuple[ExternalPosition, ...]:
        assert account_id == "account-1"
        return self.positions

    async def list_active_orders(self, account_id: str, instrument_id: str) -> tuple[ActiveBrokerOrder, ...]:
        assert account_id == "account-1"
        return tuple(item for item in self.orders if item.instrument_id == instrument_id)


class Repository:
    def __init__(self, mapped: UserBrokerCatalogInstrument | None = None) -> None:
        self.mapped = mapped
        self.candidates = []
        self.outcome = PositionAdoptionWriteResult.ADOPTED

    def find_instrument(self, user_broker_id: str, external_instrument_id: str):
        assert user_broker_id == "00000000-0000-4000-8000-000000000101"
        return self.mapped if self.mapped and self.mapped.external_instrument_id == external_instrument_id else None

    def adopt(self, candidate):
        self.candidates.append(candidate)
        return self.outcome


def position(quantity: str = "2", price: str = "100", currency: str = "RUB") -> ExternalPosition:
    return ExternalPosition(
        account_id="account-1",
        instrument_id="external-1",
        ticker="TEST",
        quantity_lots=Decimal(quantity),
        average_price=Money(Decimal(price), currency),
        current_price=None,
        expected_yield=None,
    )


def adopt(broker: Broker, repository: Repository):
    return asyncio.run(
        PositionAdoptionService(repository, clock=lambda: NOW).adopt(
            "00000000-0000-4000-8000-000000000101",
            "account-1",
            broker,
        )
    )


def test_valid_position_is_adopted_with_exact_lots_price_and_invested_amount() -> None:
    repository = Repository(instrument())

    result = adopt(Broker((position(),)), repository)

    assert result.adopted == 1
    assert result.existing == result.held == result.skipped == 0
    candidate = repository.candidates[0]
    assert candidate.quantity_lots == 2
    assert candidate.average_price == Decimal("100")
    assert candidate.invested_amount == Decimal("2000")
    assert candidate.currency == "RUB"
    assert candidate.observed_at == NOW


def test_zero_position_is_skipped_without_write() -> None:
    repository = Repository(instrument())

    result = adopt(Broker((position("0"),)), repository)

    assert result.skipped == 1
    assert repository.candidates == []


def test_invalid_quantity_price_currency_and_missing_mapping_are_held() -> None:
    cases = (
        (Repository(instrument()), position("-1"), PositionAdoptionReason.BOOTSTRAP_INVALID_QUANTITY),
        (Repository(instrument()), position("1.5"), PositionAdoptionReason.BOOTSTRAP_INVALID_QUANTITY),
        (Repository(instrument()), position("1", "0"), PositionAdoptionReason.BOOTSTRAP_PRICE_UNAVAILABLE),
        (Repository(instrument()), position("1", "100", "USD"), PositionAdoptionReason.BOOTSTRAP_CURRENCY_MISMATCH),
        (Repository(None), position(), PositionAdoptionReason.BOOTSTRAP_INSTRUMENT_NOT_FOUND),
    )

    for repository, value, reason in cases:
        result = adopt(Broker((value,)), repository)
        assert result.held == 1
        assert result.diagnostics[0].reason is reason
        assert repository.candidates == []


def test_active_or_unknown_order_holds_position() -> None:
    order = ActiveBrokerOrder("account-1", "external-1", "order-1", "UNKNOWN")
    repository = Repository(instrument())

    result = adopt(Broker((position(),), (order,)), repository)

    assert result.held == 1
    assert result.diagnostics[0].reason is PositionAdoptionReason.BOOTSTRAP_ACTIVE_ORDER
    assert repository.candidates == []


def test_repository_existing_result_is_counted_separately() -> None:
    repository = Repository(instrument())
    repository.outcome = PositionAdoptionWriteResult.EXISTING

    result = adopt(Broker((position(),)), repository)

    assert result.existing == 1
    assert result.adopted == 0
