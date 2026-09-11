import asyncio
from decimal import Decimal

from sentinel_contracts.broker_execution import BrokerPosition
from trading_automaton.services.portfolio_state_cache import PortfolioStateCacheService


def position(instrument_id: str, lots: str) -> BrokerPosition:
    return BrokerPosition(
        instrument_id,
        Decimal(lots),
        Decimal("100"),
        Decimal("101"),
        "RUB",
    )


def test_replaces_account_snapshot_and_applies_position_events() -> None:
    async def scenario():
        cache = PortfolioStateCacheService()
        await cache.replace_snapshot("account-1", (position("i1", "1"),))
        first = await cache.position("account-1", "i1")
        await cache.apply_position_event("account-1", position("i1", "2"))
        second = await cache.position("account-1", "i1")
        missing = await cache.position("account-1", "i2")
        return first, second, missing

    first, second, missing = asyncio.run(scenario())

    assert first is not None
    assert first.quantity_lots == Decimal("1")
    assert second is not None
    assert second.quantity_lots == Decimal("2")
    assert missing is None


def test_replacing_snapshot_removes_positions_no_longer_returned_by_broker() -> None:
    async def scenario():
        cache = PortfolioStateCacheService()
        await cache.replace_snapshot("account-1", (position("i1", "1"), position("i2", "1")))
        await cache.replace_snapshot("account-1", (position("i2", "1"),))
        return await cache.position("account-1", "i1")

    assert asyncio.run(scenario()) is None
