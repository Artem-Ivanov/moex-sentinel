import asyncio
from datetime import UTC, datetime

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.domain.brokers import Broker, BrokerField
from moex_sentinel.domain.portfolio import BrokerAccount
from moex_sentinel.services.connections import BrokerConnectionService


class BrokerRepository:
    def __init__(self, record: Broker) -> None:
        self.record = record

    def get(self, broker_id: str) -> Broker:
        return self.record


def broker() -> Broker:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    return Broker(
        id="broker-1",
        display_name="First",
        provider_code="TINVEST",
        environment_code="SANDBOX",
        adapter_code="TINVEST_SANDBOX",
        enabled=True,
        fields=(BrokerField(name="token", value="synthetic-token"),),
        created_at=now,
        updated_at=now,
    )


class Adapter:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def list_accounts(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise TInvestAdapterError("BROKER_UNAVAILABLE", "Недоступно.", retryable=True)
        return (BrokerAccount("account-1", "Main", "OPEN", "BROKER"),)


def test_connection_service_retries_retryable_error_once() -> None:
    record = broker()
    adapter = Adapter(failures=1)
    service = BrokerConnectionService(BrokerRepository(record), lambda _: adapter)

    status = asyncio.run(service.check(record.id))

    assert status.accounts_count == 1
    assert adapter.calls == 2
