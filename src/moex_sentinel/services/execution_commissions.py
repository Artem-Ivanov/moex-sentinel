"""Validate bounded operation pages before the Core fee journal advances."""

import asyncio
from datetime import datetime
from typing import Protocol

from moex_sentinel.domain.execution_commissions import (
    BrokerFeeObservation,
    BrokerFeeScanClaim,
    normalize_broker_fee,
)
from moex_sentinel.domain.portfolio import OperationsPage
from moex_sentinel.domain.user_brokers import UserBroker
from moex_sentinel.services.portfolio_ports import PortfolioPort
from moex_sentinel.services.ports import UserBrokerLookupPort
from sentinel_contracts.time import utc_now_ms


class ExecutionCommissionRepositoryPort(Protocol):
    def claim_next_page(self, now: datetime) -> BrokerFeeScanClaim | None: ...

    def apply_page(
        self,
        claim: BrokerFeeScanClaim,
        observations: tuple[BrokerFeeObservation, ...],
        next_cursor: str | None,
        now: datetime,
    ) -> bool: ...

    def fail_page(self, claim: BrokerFeeScanClaim, error_code: str) -> bool: ...


class ExecutionCommissionService:
    """Keep source calls outside storage transactions and expose only one page per tick."""

    def __init__(self, repository: ExecutionCommissionRepositoryPort, *, now=utc_now_ms) -> None:
        self._repository = repository
        self._now = now

    def claim_next_page(self) -> BrokerFeeScanClaim | None:
        return self._repository.claim_next_page(self._now())

    def accept_page(self, claim: BrokerFeeScanClaim, page: OperationsPage) -> bool:
        try:
            if len(page.items) > 100:
                self._repository.fail_page(claim, "SOURCE_PAGE_TOO_LARGE")
                return False
            if any(operation.account_id != claim.account_id for operation in page.items):
                self._repository.fail_page(claim, "SOURCE_ACCOUNT_MISMATCH")
                return False
            observations = tuple(
                fee
                for operation in page.items
                if (fee := normalize_broker_fee(claim.user_broker_id, operation)) is not None
            )
        except (ValueError, ArithmeticError):
            self._repository.fail_page(claim, "INVALID_FEE_SOURCE")
            return False
        return self._repository.apply_page(claim, observations, page.next_cursor, self._now())

    def fail_page(self, claim: BrokerFeeScanClaim, error_code: str = "SOURCE_FAILURE") -> bool:
        return self._repository.fail_page(claim, error_code)


class ExecutionCommissionCollectionService:
    """Read one bounded page for a claimed PROD scope and persist it atomically."""

    def __init__(
        self,
        service: ExecutionCommissionService,
        brokers: UserBrokerLookupPort,
        adapter_factory,
    ) -> None:
        self._service = service
        self._brokers = brokers
        self._adapter_factory = adapter_factory

    async def execute(self) -> bool:
        claim = self._service.claim_next_page()
        if claim is None:
            return False
        try:
            broker = self._brokers.get(claim.user_broker_id)
            if (
                not isinstance(broker, UserBroker)
                or not broker.enabled
                or broker.environment != "PROD"
                or broker.account_id != claim.account_id
            ):
                self._service.fail_page(claim, "BROKER_SCOPE_CHANGED")
                return False
            adapter: PortfolioPort = self._adapter_factory(broker)
            page = await asyncio.wait_for(
                adapter.get_operations(
                    claim.account_id, claim.cursor, 100, from_at=claim.lower_bound, to_at=claim.upper_bound
                ),
                timeout=10.0,
            )
            return self._service.accept_page(claim, page)
        except Exception:
            self._service.fail_page(claim)
            return False
