from decimal import Decimal

import pytest

from sentinel_contracts.trading import PositionSnapshot


def test_position_snapshot_rejects_malformed_decimal_metadata() -> None:
    metadata: dict[str, int | str] = {
        "quantity_lots": 1,
        "average_price": "not-a-number",
        "invested_amount": "100",
        "realized_pnl": "0",
        "unrealized_pnl": "0",
        "net_pnl": "0",
        "actual_commissions": "0",
    }

    with pytest.raises(ValueError, match="Position snapshot is invalid"):
        PositionSnapshot.from_metadata(metadata)


def test_position_snapshot_preserves_exact_decimal_values() -> None:
    snapshot = PositionSnapshot(2, *(Decimal("1.25") for _ in range(6)))

    assert PositionSnapshot.from_metadata(snapshot.to_metadata()) == snapshot
