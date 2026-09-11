import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_automaton.services.account_commission_profile import (
    AccountCommissionProfileService,
    CommissionQuote,
    CommissionRefreshRequest,
)
from trading_automaton.storage.repository import AccountCommissionProfileKey

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)
KEY = AccountCommissionProfileKey("broker", "account", "SHARE", "RUB")


class MemoryProfiles:
    def __init__(self) -> None:
        self.items = {}
        self.reads = 0

    def get_commission_profile(self, key):
        self.reads += 1
        return self.items.get(key)

    def upsert_commission_profile(self, profile):
        self.items[profile.key] = profile


def test_refresh_calculates_separate_rates_and_uses_them_without_api_calls() -> None:
    profiles = MemoryProfiles()
    service = AccountCommissionProfileService(profiles)

    profile = service.refresh(
        KEY,
        buy=CommissionQuote(
            order_amount=Decimal("10000"),
            total_commission=Decimal("0.5"),
            service_commission=Decimal("0.1"),
            deal_commission=Decimal("0.2"),
        ),
        sell=CommissionQuote(
            order_amount=Decimal("10000"),
            total_commission=Decimal("0.6"),
            service_commission=Decimal("0.1"),
            deal_commission=Decimal("0.2"),
        ),
        now=NOW,
    )

    assert profile.buy_rate == Decimal("0.00005")
    assert profile.sell_rate == Decimal("0.00006")
    assert profile.valid_until == NOW + timedelta(hours=24)
    assert service.estimate(KEY, "BUY", Decimal("2000"), now=NOW) == Decimal("0.10000")
    assert service.estimate(KEY, "SELL", Decimal("2000"), now=NOW) == Decimal("0.12000")
    assert profiles.reads == 0


def test_existing_profile_is_loaded_before_hot_path() -> None:
    profiles = MemoryProfiles()
    writer = AccountCommissionProfileService(profiles)
    writer.refresh(
        KEY,
        buy=CommissionQuote(Decimal("1000"), Decimal("0.5")),
        sell=CommissionQuote(Decimal("1000"), Decimal("0.6")),
        now=NOW,
    )
    service = AccountCommissionProfileService(profiles)

    assert service.estimate(KEY, "BUY", Decimal("1000"), now=NOW) is None
    assert service.load(KEY) is not None
    reads_after_load = profiles.reads
    assert service.estimate(KEY, "BUY", Decimal("1000"), now=NOW) == Decimal("0.5")
    assert profiles.reads == reads_after_load


def test_missing_or_expired_profile_has_no_estimate() -> None:
    profiles = MemoryProfiles()
    service = AccountCommissionProfileService(profiles)

    assert service.estimate(KEY, "BUY", Decimal("1000"), now=NOW) is None

    service.refresh(
        KEY,
        buy=CommissionQuote(Decimal("1000"), Decimal("1")),
        sell=CommissionQuote(Decimal("1000"), Decimal("1")),
        now=NOW,
    )

    assert service.estimate(KEY, "BUY", Decimal("1000"), now=NOW + timedelta(hours=24, microseconds=1)) is None


def test_schedule_uses_loaded_buy_and_sell_rates_without_another_repository_read() -> None:
    profiles = MemoryProfiles()
    service = AccountCommissionProfileService(profiles)
    service.refresh(
        KEY,
        buy=CommissionQuote(Decimal("10000"), Decimal("0.5")),
        sell=CommissionQuote(Decimal("10000"), Decimal("0.6")),
        now=NOW,
    )

    reads_before = profiles.reads
    schedule = service.schedule(KEY, snapshot_at=NOW)

    assert schedule is not None
    assert schedule.estimate("BUY", Decimal("2000")) == Decimal("0.10000")
    assert schedule.estimate("SELL", Decimal("2000")) == Decimal("0.12000")
    assert profiles.reads == reads_before


def test_schedule_is_available_at_valid_until_and_missing_after_boundary() -> None:
    profiles = MemoryProfiles()
    service = AccountCommissionProfileService(profiles)

    assert service.schedule(KEY, snapshot_at=NOW) is None
    profile = service.refresh(
        KEY,
        buy=CommissionQuote(Decimal("1000"), Decimal("1")),
        sell=CommissionQuote(Decimal("1000"), Decimal("1")),
        now=NOW,
    )

    assert service.schedule(KEY, snapshot_at=profile.valid_until) is not None
    assert service.schedule(KEY, snapshot_at=profile.valid_until + timedelta(microseconds=1)) is None


def test_executed_order_updates_only_observed_side_rate() -> None:
    profiles = MemoryProfiles()
    service = AccountCommissionProfileService(profiles)
    service.refresh(
        KEY,
        buy=CommissionQuote(Decimal("1000"), Decimal("0.5")),
        sell=CommissionQuote(Decimal("1000"), Decimal("0.6")),
        now=NOW,
    )

    updated = service.observe_execution(
        KEY,
        side="BUY",
        order_amount=Decimal("2000"),
        actual_commission=Decimal("2"),
        now=NOW + timedelta(hours=1),
    )

    assert updated is not None
    assert updated.buy_rate == Decimal("0.001")
    assert updated.sell_rate == Decimal("0.0006")
    assert updated.source == "EXECUTED_ORDER"


def test_daily_refresh_calls_quote_provider_outside_estimate() -> None:
    class Quotes:
        def __init__(self) -> None:
            self.sides = []

        async def quote(self, request, side):
            self.sides.append(side)
            return CommissionQuote(Decimal("1000"), Decimal("0.5"))

    async def scenario():
        profiles = MemoryProfiles()
        quotes = Quotes()
        service = AccountCommissionProfileService(profiles)
        request = CommissionRefreshRequest(KEY, "instrument", 1, Decimal("100"))
        first = await service.refresh_if_due(request, quotes, now=NOW)
        second = await service.refresh_if_due(request, quotes, now=NOW + timedelta(hours=23))
        return first, second, quotes

    first, second, quotes = asyncio.run(scenario())

    assert first == second
    assert quotes.sides == ["BUY", "SELL"]
