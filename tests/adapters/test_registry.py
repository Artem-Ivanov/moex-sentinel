"""Contract tests for concrete broker adapter declarations."""

import pytest

from moex_sentinel.adapters.registry import (
    SANDBOX_FQDN,
    BrokerAdapterNotFoundError,
    BrokerAdapterRegistry,
)


def test_registry_declares_only_tinvest_sandbox() -> None:
    adapters = BrokerAdapterRegistry().list()

    assert len(adapters) == 1
    adapter = adapters[0]
    assert adapter.adapter_code == "TINVEST_SANDBOX"
    assert adapter.provider_code == "TINVEST"
    assert adapter.environment_code == "SANDBOX"
    assert [(field.name, field.required) for field in adapter.fields] == [
        ("token", True),
        ("fqdn", True),
    ]
    assert adapter.fields[1].default_value == SANDBOX_FQDN


def test_registry_reports_unknown_adapter_without_configuration_values() -> None:
    with pytest.raises(BrokerAdapterNotFoundError, match="UNKNOWN"):
        BrokerAdapterRegistry().get("UNKNOWN")
