"""Daily account commission profiles kept outside the trading hot path."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from trading_automaton.domain.dtos import CommissionQuote, CommissionRefreshRequest, CommissionSchedule
from trading_automaton.domain.storage_dtos import (
    AccountCommissionProfile,
    AccountCommissionProfileKey,
)


class CommissionProfileRepositoryPort(Protocol):
    def get_commission_profile(self, key: AccountCommissionProfileKey) -> AccountCommissionProfile | None: ...

    def upsert_commission_profile(self, profile: AccountCommissionProfile) -> None: ...


class CommissionQuoteProviderPort(Protocol):
    async def quote(self, request: CommissionRefreshRequest, side: str) -> CommissionQuote: ...


class AccountCommissionProfileService:
    def __init__(
        self,
        repository: CommissionProfileRepositoryPort,
        *,
        ttl: timedelta = timedelta(hours=24),
    ) -> None:
        self._repository = repository
        self._ttl = ttl
        self._profiles: dict[AccountCommissionProfileKey, AccountCommissionProfile] = {}

    def load(self, key: AccountCommissionProfileKey) -> AccountCommissionProfile | None:
        profile = self._repository.get_commission_profile(key)
        if profile is not None:
            self._profiles[key] = profile
        return profile

    def estimate(
        self,
        key: AccountCommissionProfileKey,
        side: str,
        order_amount: Decimal,
        *,
        now: datetime,
    ) -> Decimal | None:
        profile = self._profiles.get(key)
        if profile is None or now > profile.valid_until:
            return None
        rate = self._side_rate(profile, side)
        return order_amount * rate

    def schedule(
        self,
        key: AccountCommissionProfileKey,
        *,
        snapshot_at: datetime,
    ) -> CommissionSchedule | None:
        """Return only already-loaded rates valid for the immutable snapshot."""
        profile = self._profiles.get(key)
        if profile is None or snapshot_at > profile.valid_until:
            return None
        return CommissionSchedule(profile.buy_rate, profile.sell_rate)

    async def refresh_if_due(
        self,
        request: CommissionRefreshRequest,
        provider: CommissionQuoteProviderPort,
        *,
        now: datetime,
    ) -> AccountCommissionProfile:
        profile = self._profiles.get(request.key)
        if profile is None:
            profile = self.load(request.key)
        if profile is not None and now <= profile.valid_until:
            return profile
        buy, sell = await asyncio.gather(
            provider.quote(request, "BUY"),
            provider.quote(request, "SELL"),
        )
        return self.refresh(request.key, buy=buy, sell=sell, now=now)

    def refresh(
        self,
        key: AccountCommissionProfileKey,
        *,
        buy: CommissionQuote,
        sell: CommissionQuote,
        now: datetime,
    ) -> AccountCommissionProfile:
        profile = AccountCommissionProfile(
            key=key,
            buy_rate=buy.rate(buy.total_commission),
            sell_rate=sell.rate(sell.total_commission),
            service_rate=max(buy.rate(buy.service_commission), sell.rate(sell.service_commission)),
            deal_rate=max(buy.rate(buy.deal_commission), sell.rate(sell.deal_commission)),
            source="GET_ORDER_PRICE",
            calculated_at=now,
            valid_until=now + self._ttl,
        )
        self._repository.upsert_commission_profile(profile)
        self._profiles[key] = profile
        return profile

    def observe_execution(
        self,
        key: AccountCommissionProfileKey,
        *,
        side: str,
        order_amount: Decimal,
        actual_commission: Decimal,
        now: datetime,
    ) -> AccountCommissionProfile | None:
        profile = self._profiles.get(key)
        if profile is None:
            profile = self.load(key)
        if profile is None or order_amount <= 0 or actual_commission < 0:
            return None
        rate = actual_commission / order_amount
        if side == "BUY":
            updated = profile.model_copy(update={"buy_rate": rate})
        elif side == "SELL":
            updated = profile.model_copy(update={"sell_rate": rate})
        else:
            raise ValueError(f"Unsupported order side: {side}")
        updated = updated.model_copy(
            update={"source": "EXECUTED_ORDER", "calculated_at": now, "valid_until": now + self._ttl}
        )
        self._repository.upsert_commission_profile(updated)
        self._profiles[key] = updated
        return updated

    @staticmethod
    def _side_rate(profile: AccountCommissionProfile, side: str) -> Decimal:
        if side == "BUY":
            return profile.buy_rate
        if side == "SELL":
            return profile.sell_rate
        raise ValueError(f"Unsupported order side: {side}")
