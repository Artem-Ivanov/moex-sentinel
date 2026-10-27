import asyncio
from contextlib import suppress
from decimal import Decimal

from trading_automaton.services.account_cash_reservation import (
    AccountCashReservationService,
    ActiveCashReservation,
    CommittedCashReservation,
)


def test_reserves_atomically_against_latest_account_cash_snapshot() -> None:
    async def scenario():
        service = AccountCashReservationService()
        await service.replace_snapshot("account", "RUB", Decimal("150"))
        first, second = await asyncio.gather(
            service.try_reserve("account", "RUB", "intent-1", Decimal("100")),
            service.try_reserve("account", "RUB", "intent-2", Decimal("100")),
        )
        return service, first, second

    service, first, second = asyncio.run(scenario())

    assert sorted((first, second)) == [False, True]
    assert asyncio.run(service.reserved("account", "RUB")) == Decimal("100")
    assert asyncio.run(service.available("account", "RUB")) == Decimal("50")


def test_reservation_is_idempotent_and_released_only_for_terminal_intent() -> None:
    async def scenario():
        service = AccountCashReservationService()
        await service.replace_snapshot("account", "RUB", Decimal("100"))
        assert await service.try_reserve("account", "RUB", "intent", Decimal("75"))
        assert await service.try_reserve("account", "RUB", "intent", Decimal("75"))
        await service.complete("intent", "UNCERTAIN")
        uncertain = await service.reserved("account", "RUB")
        await service.complete("intent", "FILLED")
        terminal = await service.reserved("account", "RUB")
        return uncertain, terminal

    uncertain, terminal = asyncio.run(scenario())

    assert uncertain == Decimal("75")
    assert terminal == Decimal()


def test_restores_active_buy_intents_and_uses_all_remaining_free_cash() -> None:
    async def scenario():
        service = AccountCashReservationService()
        await service.replace_snapshot("account", "RUB", Decimal("200"))
        await service.restore(
            (
                ActiveCashReservation("intent-active", "broker", "account", "RUB", Decimal("50"), "BUY", "ACCEPTED"),
                ActiveCashReservation(
                    "intent-uncertain", "broker", "account", "RUB", Decimal("25"), "BUY", "UNCERTAIN"
                ),
                ActiveCashReservation("intent-terminal", "broker", "account", "RUB", Decimal("80"), "BUY", "FILLED"),
                ActiveCashReservation("intent-sell", "broker", "account", "RUB", Decimal("40"), "SELL", "ACCEPTED"),
            )
        )
        accepted = await service.try_reserve("account", "RUB", "intent-new", Decimal("125"))
        return service, accepted

    service, accepted = asyncio.run(scenario())

    assert accepted is True
    assert asyncio.run(service.reserved("account", "RUB")) == Decimal("200")
    assert asyncio.run(service.available("account", "RUB")) == Decimal()


def test_rejects_reservation_above_free_cash_by_minimal_amount() -> None:
    async def scenario() -> tuple[bool, bool]:
        service = AccountCashReservationService()
        await service.replace_snapshot("account", "RUB", Decimal("100.00"))
        above = await service.try_reserve("account", "RUB", "too-large", Decimal("100.01"))
        exact = await service.try_reserve("account", "RUB", "exact", Decimal("100.00"))
        return above, exact

    assert asyncio.run(scenario()) == (False, True)


def test_committed_reservation_batch_is_all_or_nothing_and_preserves_existing() -> None:
    async def scenario():
        service = AccountCashReservationService()
        await service.reserve_committed_batch((CommittedCashReservation("account", "RUB", "existing", Decimal("25")),))
        with suppress(ValueError):
            await service.reserve_committed_batch(
                (
                    CommittedCashReservation("account", "RUB", "new", Decimal("50")),
                    CommittedCashReservation("account", "RUB", "existing", Decimal("30")),
                )
            )
        return service

    service = asyncio.run(scenario())

    assert asyncio.run(service.reserved("account", "RUB")) == Decimal("25")
    assert asyncio.run(service.available("account", "RUB")) == Decimal()
