"""Concrete registry of broker adapters available in this build."""

from moex_sentinel.domain.brokers import (
    BrokerAdapterDefinition,
    BrokerAdapterNotFoundError,
    BrokerFieldDefinition,
)

SANDBOX_FQDN = "sandbox-invest-public-api.tbank.ru:443"


TINVEST_SANDBOX = BrokerAdapterDefinition(
    adapter_code="TINVEST_SANDBOX",
    provider_code="TINVEST",
    environment_code="SANDBOX",
    fields=(
        BrokerFieldDefinition(name="token", required=True),
        BrokerFieldDefinition(name="fqdn", required=True, default_value=SANDBOX_FQDN),
    ),
)


class BrokerAdapterRegistry:
    """Immutable adapter declaration registry."""

    _adapters = (TINVEST_SANDBOX,)

    def list(self) -> tuple[BrokerAdapterDefinition, ...]:
        return self._adapters

    def get(self, adapter_code: str) -> BrokerAdapterDefinition:
        for adapter in self._adapters:
            if adapter.adapter_code == adapter_code:
                return adapter
        raise BrokerAdapterNotFoundError(adapter_code)
