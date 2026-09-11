"""Account-scoped cash snapshots and atomic reservations for BUY intents."""

import asyncio
from decimal import Decimal
from typing import Protocol

from trading_automaton.domain.dtos import ActiveCashReservation, CommittedCashReservation, _Reservation

__all__ = [
    "AccountCashReservationService",
    "ActiveCashReservation",
    "ActiveCashReservationPort",
    "CommittedCashReservation",
]

_ACTIVE_STATES = {
    "CREATED",
    "DISPATCH_PENDING",
    "SUBMITTING",
    "SUBMITTED",
    "ACCEPTED",
    "PARTIALLY_FILLED",
    "UNCERTAIN",
}
_TERMINAL_STATES = {"FILLED", "REJECTED", "CANCELLED", "EXPIRED"}


class ActiveCashReservationPort(Protocol):
    intent_id: str
    account_id: str
    currency: str
    amount: Decimal
    side: str
    state: str


class AccountCashReservationService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._snapshots: dict[tuple[str, str], Decimal] = {}
        self._reservations: dict[str, _Reservation] = {}

    async def replace_snapshot(self, account_id: str, currency: str, free_cash: Decimal) -> None:
        async with self._lock:
            self._snapshots[self._key(account_id, currency)] = max(Decimal(), free_cash)

    async def available(self, account_id: str, currency: str) -> Decimal:
        async with self._lock:
            return self._available_unlocked(account_id, currency)

    async def reserved(self, account_id: str, currency: str) -> Decimal:
        async with self._lock:
            return self._reserved_unlocked(account_id, currency)

    async def try_reserve(
        self,
        account_id: str,
        currency: str,
        intent_id: str,
        amount: Decimal,
    ) -> bool:
        if amount < 0:
            raise ValueError("Cash reservation amount must be non-negative.")
        async with self._lock:
            existing = self._reservations.get(intent_id)
            reservation = _Reservation(account_id, currency.upper(), amount)
            if existing is not None:
                return existing == reservation
            if self._available_unlocked(account_id, currency) < amount:
                return False
            self._reservations[intent_id] = reservation
            return True

    async def reserve_committed_batch(self, values: tuple[CommittedCashReservation, ...]) -> None:
        """Atomically publish reservations after their durable intents commit."""
        async with self._lock:
            additions: dict[str, _Reservation] = {}
            for value in values:
                if value.amount < 0:
                    raise ValueError("Cash reservation values must be non-negative.")
                reservation = _Reservation(value.account_id, value.currency.upper(), value.amount)
                existing = additions.get(value.intent_id, self._reservations.get(value.intent_id))
                if existing is not None and existing != reservation:
                    raise ValueError("Committed cash reservation conflicts with existing reservation.")
                additions[value.intent_id] = reservation
            self._reservations.update(additions)

    async def complete(self, intent_id: str, state: str) -> None:
        if state not in _TERMINAL_STATES:
            return
        async with self._lock:
            self._reservations.pop(intent_id, None)

    async def release(self, intent_id: str) -> None:
        async with self._lock:
            self._reservations.pop(intent_id, None)

    async def restore(self, values: tuple[ActiveCashReservationPort, ...]) -> None:
        async with self._lock:
            for value in values:
                if value.side == "BUY" and value.state in _ACTIVE_STATES:
                    self._reservations[value.intent_id] = _Reservation(
                        value.account_id,
                        value.currency.upper(),
                        value.amount,
                    )

    def _available_unlocked(self, account_id: str, currency: str) -> Decimal:
        free_cash = self._snapshots.get(self._key(account_id, currency), Decimal())
        return max(Decimal(), free_cash - self._reserved_unlocked(account_id, currency))

    def _reserved_unlocked(self, account_id: str, currency: str) -> Decimal:
        expected_currency = currency.upper()
        return sum(
            (
                item.amount
                for item in self._reservations.values()
                if item.account_id == account_id and item.currency == expected_currency
            ),
            start=Decimal(),
        )

    @staticmethod
    def _key(account_id: str, currency: str) -> tuple[str, str]:
        return account_id, currency.upper()
