"""Behavior tests for code-owned broker API modules."""

import pytest
from pydantic import ValidationError

from moex_sentinel.adapters.broker_api_registry import BrokerApiRegistry
from moex_sentinel.adapters.tinvest.api_module import TInvestApiModule
from moex_sentinel.domain.user_brokers import (
    BrokerApiEnvironmentError,
    BrokerApiNotFoundError,
    BrokerApiRegistryDuplicateError,
)


def test_registry_exposes_tinvest_by_stable_slug() -> None:
    registry = BrokerApiRegistry((TInvestApiModule(),))

    module = registry.get("t_invest")

    assert registry.list() == (module.descriptor,)
    assert module.descriptor.api_slug == "t_invest"
    assert module.descriptor.environments == ("TEST",)
    assert module.default_fqdn("TEST") == "sandbox-invest-public-api.tbank.ru:443"


def test_registry_rejects_unknown_slug_without_repeating_settings() -> None:
    registry = BrokerApiRegistry((TInvestApiModule(),))

    with pytest.raises(BrokerApiNotFoundError) as caught:
        registry.validate_settings("missing", {"token": "synthetic-token"})

    assert "synthetic-token" not in str(caught.value)


def test_tinvest_settings_validate_connection_token_only() -> None:
    result = BrokerApiRegistry((TInvestApiModule(),)).validate_settings(
        "t_invest",
        {"token": "synthetic-token"},
    )

    assert result == {"token": "synthetic-token"}


def test_tinvest_settings_reject_provisioning_fields() -> None:
    registry = BrokerApiRegistry((TInvestApiModule(),))

    with pytest.raises(ValidationError) as caught:
        registry.validate_settings(
            "t_invest",
            {"token": "synthetic-token", "initial_balance": "10000"},
        )

    assert "synthetic-token" not in str(caught.value)


def test_tinvest_module_rejects_unsupported_environment() -> None:
    with pytest.raises(BrokerApiEnvironmentError, match="PROD"):
        TInvestApiModule().default_fqdn("PROD")


def test_registry_rejects_duplicate_api_slugs() -> None:
    with pytest.raises(BrokerApiRegistryDuplicateError, match="t_invest"):
        BrokerApiRegistry((TInvestApiModule(), TInvestApiModule()))
