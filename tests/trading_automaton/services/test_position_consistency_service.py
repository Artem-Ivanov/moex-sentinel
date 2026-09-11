from datetime import UTC, datetime
from decimal import Decimal

from trading_automaton.services.lot_ledger import LotLedgerMismatchError
from trading_automaton.services.position_consistency import PositionConsistencyService

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


class Ledger:
    def __init__(self, result=(), error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = []

    def reconcile(self, **values):
        self.calls.append(values)
        if self.error is not None:
            raise self.error
        return self.result


class Holds:
    def __init__(self) -> None:
        self.calls = []

    def hold_active(self, reason, automation_id=None):
        self.calls.append((reason, automation_id))


def test_allows_position_when_broker_and_worker_ledger_reconcile() -> None:
    ledger = Ledger(result=("lot",))
    holds = Holds()

    result = PositionConsistencyService(ledger, holds, now=lambda: NOW).reconcile(
        automation_id="automation",
        broker_lots=2,
        average_price=Decimal("100"),
    )

    assert result.consistent is True
    assert result.lots == ("lot",)
    assert holds.calls == []


def test_holds_only_the_ambiguous_position_when_ledger_differs() -> None:
    ledger = Ledger(error=LotLedgerMismatchError("differ"))
    holds = Holds()

    result = PositionConsistencyService(ledger, holds, now=lambda: NOW).reconcile(
        automation_id="automation",
        broker_lots=0,
        average_price=Decimal(),
    )

    assert result.consistent is False
    assert result.reason_code == "POSITION_RECONCILIATION_REQUIRED"
    assert holds.calls == [("POSITION_RECONCILIATION_REQUIRED", "automation")]
