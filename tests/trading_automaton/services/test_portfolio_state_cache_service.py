import asyncio
from decimal import Decimal

from sentinel_contracts.broker_execution import BrokerPosition
from trading_automaton.services.portfolio_state_cache import PortfolioStateCacheService


def test_positive_security_count_is_scoped_to_account_and_ignores_empty_positions() -> None:
    async def scenario() -> tuple[int, int]:
        portfolio = PortfolioStateCacheService()
        await portfolio.replace_snapshot(
            "account-1",
            (
                BrokerPosition("open", Decimal("2"), Decimal("10"), Decimal("11"), "RUB"),
                BrokerPosition("empty", Decimal(), Decimal(), Decimal(), "RUB"),
            ),
        )
        await portfolio.replace_snapshot(
            "account-2",
            (BrokerPosition("other", Decimal("1"), Decimal("20"), Decimal("21"), "RUB"),),
        )
        return (
            await portfolio.positive_security_count("account-1"),
            await portfolio.positive_security_count("account-2"),
        )

    assert asyncio.run(scenario()) == (1, 1)
