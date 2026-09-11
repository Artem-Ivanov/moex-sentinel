"""Guard trading decisions with broker-to-ledger position reconciliation."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from trading_automaton.domain.dtos import PositionConsistencyResult
from trading_automaton.services.lot_ledger import LotLedgerMismatchError
from trading_automaton.storage.repository import TradeLotRecord


class PositionLedgerPort(Protocol):
    def reconcile(
        self,
        *,
        automation_id: str,
        broker_lots: int,
        average_price: Decimal,
        occurred_at: datetime,
    ) -> list[TradeLotRecord]: ...


class PositionHoldPort(Protocol):
    def hold_active(self, reason: str, automation_id: str | None = None) -> None: ...
class PositionConsistencyService:
    def __init__(
        self,
        ledger: PositionLedgerPort,
        holds: PositionHoldPort,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._ledger = ledger
        self._holds = holds
        self._now = now

    def reconcile(
        self,
        *,
        automation_id: str,
        broker_lots: int,
        average_price: Decimal,
    ) -> PositionConsistencyResult:
        try:
            lots = self._ledger.reconcile(
                automation_id=automation_id,
                broker_lots=broker_lots,
                average_price=average_price,
                occurred_at=self._now(),
            )
        except (ArithmeticError, LotLedgerMismatchError, ValueError):
            reason = "POSITION_RECONCILIATION_REQUIRED"
            self._holds.hold_active(reason, automation_id)
            return PositionConsistencyResult(False, (), reason)
        return PositionConsistencyResult(True, tuple(lots))
