"""Behavior tests for configured broker API/account scopes."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from moex_sentinel.domain.user_brokers import (
    BrokerApiDescriptor,
    BrokerApiFieldDescriptor,
    UserBroker,
    UserBrokerDraft,
    UserBrokerState,
)


def test_active_user_broker_requires_external_account_id() -> None:
    with pytest.raises(ValidationError) as caught:
        UserBrokerDraft(
            api_slug="t_invest",
            display_name="Sandbox",
            environment="TEST",
            fqdn="sandbox-invest-public-api.tbank.ru:443",
            settings={"token": "synthetic-token"},
            external_account_id=None,
            state=UserBrokerState.ACTIVE,
        )

    assert "synthetic-token" not in str(caught.value)


def test_draft_user_broker_allows_missing_external_account_id() -> None:
    draft = UserBrokerDraft(
        api_slug="t_invest",
        display_name="Sandbox",
        environment="TEST",
        fqdn="sandbox-invest-public-api.tbank.ru:443",
        settings={"token": "synthetic-token"},
        external_account_id=None,
        state=UserBrokerState.DRAFT,
    )

    assert draft.external_account_id is None
    assert draft.state is UserBrokerState.DRAFT


@pytest.mark.parametrize(
    ("model_type", "values"),
    [
        (
            BrokerApiFieldDescriptor,
            {"name": "token", "required": True, "value_type": "string"},
        ),
        (
            BrokerApiDescriptor,
            {
                "api_slug": "t_invest",
                "display_name": "T-Invest",
                "environments": ("TEST",),
                "fields": (),
            },
        ),
        (
            UserBrokerDraft,
            {
                "api_slug": "t_invest",
                "display_name": "Sandbox",
                "environment": "TEST",
                "fqdn": "sandbox-invest-public-api.tbank.ru:443",
                "settings": {"token": "synthetic-token"},
                "external_account_id": None,
                "state": UserBrokerState.DRAFT,
            },
        ),
        (
            UserBroker,
            {
                "api_slug": "t_invest",
                "display_name": "Sandbox",
                "environment": "TEST",
                "fqdn": "sandbox-invest-public-api.tbank.ru:443",
                "settings": {"token": "synthetic-token"},
                "external_account_id": "synthetic-account",
                "state": UserBrokerState.ACTIVE,
                "id": "user-broker-id",
                "created_at": datetime(2026, 8, 10, tzinfo=UTC),
                "updated_at": datetime(2026, 8, 10, tzinfo=UTC),
            },
        ),
    ],
)
def test_user_broker_contracts_reject_unknown_fields(model_type: type, values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        model_type.model_validate({**values, "unexpected": True})
