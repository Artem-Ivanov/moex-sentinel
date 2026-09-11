from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from trading_automaton.services.lot_ledger import LotLedgerMismatchError, LotLedgerService
from trading_automaton.storage.models import Base, LotAllocationModel
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


def setup_ledger():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repository = LocalAutomationRepository(factory)
    return LotLedgerService(repository), repository, factory


def test_buy_is_idempotent_and_sell_allocates_newest_lot_first() -> None:
    ledger, repository, factory = setup_ledger()
    ledger.record_buy_execution(
        automation_id="a",
        intent_id="buy-1",
        quantity_lots=2,
        price=Decimal("100"),
        commission=Decimal("2"),
        executed_at=NOW,
    )
    ledger.record_buy_execution(
        automation_id="a",
        intent_id="buy-2",
        quantity_lots=2,
        price=Decimal("90"),
        commission=Decimal("2"),
        executed_at=NOW + timedelta(minutes=1),
    )
    ledger.record_buy_execution(
        automation_id="a",
        intent_id="buy-2",
        quantity_lots=2,
        price=Decimal("90"),
        commission=Decimal("2"),
        executed_at=NOW + timedelta(minutes=1),
    )

    ledger.allocate_sell_execution(
        automation_id="a",
        intent_id="sell-1",
        quantity_lots=3,
        price=Decimal("101"),
        commission=Decimal("3"),
        executed_at=NOW + timedelta(minutes=2),
        lot_size=1,
    )

    open_lots = repository.list_open_lots("a")
    assert [(item.entry_price, item.remaining_lots) for item in open_lots] == [(Decimal("100.000000000"), 1)]
    with factory() as session:
        allocations = session.query(LotAllocationModel).all()
    assert [item.quantity_lots for item in allocations] == [2, 1]


def test_reconcile_creates_one_synthetic_lot_then_detects_mismatch() -> None:
    ledger, _, _ = setup_ledger()
    lots = ledger.reconcile(automation_id="a", broker_lots=4, average_price=Decimal("75"), occurred_at=NOW)
    assert [(item.source, item.remaining_lots) for item in lots] == [("RECONCILED", 4)]

    with pytest.raises(LotLedgerMismatchError, match="differ"):
        ledger.reconcile(
            automation_id="a",
            broker_lots=3,
            average_price=Decimal("75"),
            occurred_at=NOW,
        )


@pytest.mark.parametrize(
    ("amount", "lots", "lot_size", "broker_price", "limit_price", "expected"),
    [
        ("941.4", 1, 10, "941.4", "94.14", "94.14"),
        ("919.9", 1, 100, "919.9", "9.199", "9.199"),
        ("200.5", 2, 1, "100.25", "100", "100.25"),
        ("0", 1, 10, "100.25", "100", "100.25"),
    ],
)
def test_execution_price_is_normalized_to_one_instrument_unit(
    amount: str,
    lots: int,
    lot_size: int,
    broker_price: str,
    limit_price: str,
    expected: str,
) -> None:
    ledger, _, _ = setup_ledger()

    price = ledger.normalize_execution_price(
        executed_amount=Decimal(amount),
        executed_lots=lots,
        lot_size=lot_size,
        broker_price=Decimal(broker_price),
        limit_price=Decimal(limit_price),
    )

    assert price == Decimal(expected)


@pytest.mark.parametrize(
    ("amount", "lots", "lot_size", "broker_price", "limit_price", "reason"),
    [
        ("0", 1, 10, "0", "100", "PRICE_UNAVAILABLE"),
        ("500", 1, 1, "500", "100", "PRICE_OUT_OF_RANGE"),
    ],
)
def test_execution_price_rejects_missing_or_implausible_values(
    amount: str,
    lots: int,
    lot_size: int,
    broker_price: str,
    limit_price: str,
    reason: str,
) -> None:
    ledger, _, _ = setup_ledger()

    with pytest.raises(ValueError, match=reason):
        ledger.normalize_execution_price(
            executed_amount=Decimal(amount),
            executed_lots=lots,
            lot_size=lot_size,
            broker_price=Decimal(broker_price),
            limit_price=Decimal(limit_price),
        )
