import asyncio
from datetime import UTC, datetime

import pytest

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


@pytest.mark.parametrize("account_ids", [(), ("foreign",)])
def test_check_rejects_missing_selected_account_without_retry(account_ids):
    record = broker().model_copy(update={"account_id": "selected"})

    class AccountsAdapter:
        calls = 0

        async def list_accounts(self):
            self.calls += 1
            return tuple(BrokerAccount(account_id, "Main", "OPEN", "BROKER") for account_id in account_ids)

    adapter = AccountsAdapter()
    service = BrokerConnectionService(BrokerRepository(record), lambda _: adapter)

    with pytest.raises(TInvestAdapterError, match="Выбранный счёт не найден на площадке.") as error:
        asyncio.run(service.check(record.id))

    assert error.value.code == "BROKER_ACCOUNT_NOT_FOUND"
    assert error.value.retryable is False
    assert adapter.calls == 1


def test_check_selected_account_preserves_available_count():
    record = broker().model_copy(update={"account_id": "account-1"})

    class AccountsAdapter:
        async def list_accounts(self):
            return (
                BrokerAccount("foreign", "Other", "OPEN", "BROKER"),
                BrokerAccount("account-1", "Main", "OPEN", "BROKER"),
            )

    service = BrokerConnectionService(BrokerRepository(record), lambda _: AccountsAdapter())
    status = asyncio.run(service.check(record.id))
    assert status.available is True
    assert status.accounts_count == 2
