"""Behavioral tests for broker configuration business rules."""

from datetime import UTC, datetime

import pytest

from moex_sentinel.domain.brokers import (
    BrokerAdapterDefinition,
    BrokerAdapterNotFoundError,
    BrokerDraft,
    BrokerField,
    BrokerFieldDefinition,
)
from moex_sentinel.domain.environment import EnvironmentState
from moex_sentinel.domain.user_brokers import (
    UserBroker,
    UserBrokerDraft,
    UserBrokerDuplicateError,
    UserBrokerState,
)
from moex_sentinel.services.brokers import (
    BrokerConfigurationService,
    DuplicateBrokerError,
    InvalidBrokerConfigurationError,
    UnknownBrokerAdapterError,
)

SANDBOX_FQDN = "sandbox-invest-public-api.tbank.ru:443"


class FakeBrokerRepository:
    def __init__(self) -> None:
        self.records: dict[str, UserBroker] = {}
        self.created: list[UserBrokerDraft] = []

    def list(self) -> list[UserBroker]:
        return list(self.records.values())

    def get(self, broker_id: str) -> UserBroker:
        return self.records[broker_id]

    def create(self, draft: UserBrokerDraft) -> UserBroker:
        self.created.append(draft)
        now = datetime(2026, 8, 4, tzinfo=UTC)
        record = UserBroker(
            id="broker-1",
            api_slug=draft.api_slug,
            display_name=draft.display_name,
            environment=draft.environment,
            fqdn=draft.fqdn,
            settings=draft.settings,
            external_account_id=draft.external_account_id,
            state=draft.state,
            created_at=now,
            updated_at=now,
        )
        self.records[record.id] = record
        return record

    def replace(self, broker_id: str, draft: UserBrokerDraft) -> UserBroker:
        current = self.records[broker_id]
        record = current.model_copy(
            update={
                "display_name": draft.display_name,
                "api_slug": draft.api_slug,
                "environment": draft.environment,
                "fqdn": draft.fqdn,
                "settings": draft.settings,
                "external_account_id": draft.external_account_id,
                "state": draft.state,
            }
        )
        self.records[broker_id] = record
        return record

    def disable(self, broker_id: str) -> UserBroker:
        record = self.records[broker_id].model_copy(update={"state": UserBrokerState.DISABLED})
        self.records[broker_id] = record
        return record


class DuplicateBrokerRepository(FakeBrokerRepository):
    def create(self, draft: UserBrokerDraft) -> UserBroker:
        raise UserBrokerDuplicateError


class FakeBrokerRegistry:
    adapter = BrokerAdapterDefinition(
        adapter_code="TINVEST_SANDBOX",
        provider_code="TINVEST",
        environment_code="SANDBOX",
        fields=(
            BrokerFieldDefinition(name="token", required=True),
            BrokerFieldDefinition(name="fqdn", required=True, default_value=SANDBOX_FQDN),
        ),
    )

    def list(self) -> tuple[BrokerAdapterDefinition, ...]:
        return (self.adapter,)

    def get(self, adapter_code: str) -> BrokerAdapterDefinition:
        if adapter_code != self.adapter.adapter_code:
            raise BrokerAdapterNotFoundError(adapter_code)
        return self.adapter


class ProdEnvironment:
    def view(self) -> EnvironmentState:
        return EnvironmentState("PROD", False, False)


def draft(name: str = "Primary sandbox") -> BrokerDraft:
    return BrokerDraft(
        display_name=name,
        provider_code="TINVEST",
        environment_code="SANDBOX",
        adapter_code="TINVEST_SANDBOX",
        enabled=True,
        fields=(
            BrokerField(name="token", value="synthetic-token"),
            BrokerField(name="fqdn", value=SANDBOX_FQDN),
        ),
    )


def test_service_uses_injected_dependencies_to_view_and_save_settings() -> None:
    repository = FakeBrokerRepository()
    service = BrokerConfigurationService(repository, FakeBrokerRegistry())

    created = service.save_settings(None, draft())

    assert repository.created[0].api_slug == draft().adapter_code
    assert service.view_settings().brokers == (created,)
    assert service.view_settings().adapters == (FakeBrokerRegistry.adapter,)


@pytest.mark.parametrize(
    "invalid_draft",
    [
        draft().model_copy(update={"display_name": " "}),
        draft().model_copy(update={"fields": (BrokerField(name="fqdn", value=SANDBOX_FQDN),)}),
        draft().model_copy(
            update={
                "fields": (
                    BrokerField(name="token", value="synthetic-token"),
                    BrokerField(name="fqdn", value="invest-public-api.tbank.ru:443"),
                )
            }
        ),
    ],
)
def test_service_rejects_invalid_configuration_before_mutation(
    invalid_draft: BrokerDraft,
) -> None:
    repository = FakeBrokerRepository()
    service = BrokerConfigurationService(repository, FakeBrokerRegistry())

    with pytest.raises(InvalidBrokerConfigurationError):
        service.save_settings(None, invalid_draft)

    assert repository.created == []


def test_service_maps_unknown_adapter_without_exposing_fields() -> None:
    service = BrokerConfigurationService(FakeBrokerRepository(), FakeBrokerRegistry())
    invalid = draft().model_copy(update={"adapter_code": "UNKNOWN"})

    with pytest.raises(UnknownBrokerAdapterError) as error:
        service.save_settings(None, invalid)

    assert "synthetic-token" not in str(error.value)


def test_service_maps_repository_duplicate_error() -> None:
    service = BrokerConfigurationService(DuplicateBrokerRepository(), FakeBrokerRegistry())

    with pytest.raises(DuplicateBrokerError):
        service.save_settings(None, draft())


def test_service_reports_fields_without_repeating_submitted_values() -> None:
    service = BrokerConfigurationService(FakeBrokerRepository(), FakeBrokerRegistry())
    invalid = draft().model_copy(update={"display_name": " "})

    with pytest.raises(InvalidBrokerConfigurationError) as caught:
        service.save_settings(None, invalid)

    assert {item.path for item in caught.value.fields} == {"display_name"}
    assert "synthetic-token" not in str(caught.value)


def test_prod_environment_hides_test_adapters_and_brokers() -> None:
    repository = FakeBrokerRepository()
    test_service = BrokerConfigurationService(repository, FakeBrokerRegistry())
    test_service.save_settings(None, draft())

    settings = BrokerConfigurationService(repository, FakeBrokerRegistry(), ProdEnvironment()).view_settings()

    assert settings.adapters == ()
    assert settings.brokers == ()
