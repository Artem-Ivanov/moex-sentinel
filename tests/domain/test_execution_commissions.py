from datetime import UTC, datetime
from decimal import Decimal

import pytest

from moex_sentinel.domain.execution_commissions import normalize_broker_fee
from moex_sentinel.domain.portfolio import ExternalOperation, Money


def operation(**overrides: object) -> ExternalOperation:
    values: dict[str, object] = {
        "operation_id": "fee-operation-1",
        "parent_operation_id": "exchange-order-1",
        "account_id": "account-1",
        "operation_type": "BROKER_FEE",
        "state": "EXECUTED",
        "occurred_at": datetime(2026, 10, 27, 7, 0, 0, 123456, tzinfo=UTC),
        "payment": Money(Decimal("-1.72"), "rub"),
        "price": None,
        "quantity": Decimal("0"),
        "commission": Money(Decimal("-0.01"), "RUB"),
        "instrument_id": "instrument-1",
    }
    values.update(overrides)
    return ExternalOperation(**values)


def test_normalizes_broker_fee_from_negative_payment_and_parent_identity() -> None:
    fee = normalize_broker_fee("broker-1", operation())

    assert fee is not None
    assert fee.parent_operation_id == "exchange-order-1"
    assert fee.operation_id == "fee-operation-1"
    assert fee.amount == Decimal("1.72")
    assert fee.currency == "RUB"
    assert fee.occurred_at == datetime(2026, 10, 27, 7, 0, 0, 123000, tzinfo=UTC)


def test_normalizes_actual_t_invest_enum_names() -> None:
    fee = normalize_broker_fee(
        "broker-1",
        operation(
            operation_type="OPERATION_TYPE_BROKER_FEE",
            state="OPERATION_STATE_EXECUTED",
        ),
    )

    assert fee is not None
    assert fee.amount == Decimal("1.72")


def test_ignores_non_broker_fee_nested_commission() -> None:
    assert normalize_broker_fee("broker-1", operation(operation_type="BUY")) is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"payment": Money(Decimal("0"), "RUB")}, "negative amount"),
        ({"payment": Money(Decimal("1.72"), "RUB")}, "negative amount"),
        ({"payment": None}, "incomplete"),
        ({"parent_operation_id": None}, "incomplete"),
        ({"state": "PENDING"}, "incomplete"),
        ({"payment": Money(Decimal("-1.0000000001"), "RUB")}, "9 decimal places"),
    ],
)
def test_rejects_unconfirmed_or_unrepresentable_fee(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_broker_fee("broker-1", operation(**changes))
