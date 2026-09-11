"""Behavioral tests for user-intent broker use cases."""

from dataclasses import dataclass, field

import pytest

from moex_sentinel.domain.brokers import BrokerDraft, BrokerField, BrokerSettings
from moex_sentinel.services.brokers import BrokerNotFoundError, InvalidBrokerConfigurationError
from moex_sentinel.usecases.brokers import (
    DeleteBrokerSettingsUsecase,
    SaveBrokerSettingsUsecase,
    ViewBrokerSettingsUsecase,
)
from moex_sentinel.usecases.errors import FieldError, UseCaseError


def draft() -> BrokerDraft:
    return BrokerDraft(
        display_name="Primary sandbox",
        provider_code="TINVEST",
        environment_code="SANDBOX",
        adapter_code="TINVEST_SANDBOX",
        enabled=True,
        fields=(
            BrokerField(name="token", value="synthetic-token"),
            BrokerField(name="fqdn", value="sandbox-invest-public-api.tbank.ru:443"),
        ),
    )


@dataclass
class StubBrokerConfigurationService:
    settings: BrokerSettings = field(default_factory=lambda: BrokerSettings((), ()))
    saved: list[tuple[str | None, BrokerDraft]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    error: Exception | None = None

    def view_settings(self) -> BrokerSettings:
        if self.error:
            raise self.error
        return self.settings

    def save_settings(self, broker_id: str | None, value: BrokerDraft):
        if self.error:
            raise self.error
        self.saved.append((broker_id, value))
        return "saved"

    def delete_settings(self, broker_id: str) -> None:
        if self.error:
            raise self.error
        self.deleted.append(broker_id)


def test_usecases_reflect_view_save_and_delete_user_intents() -> None:
    service = StubBrokerConfigurationService()

    assert ViewBrokerSettingsUsecase(service).execute() == BrokerSettings((), ())
    assert SaveBrokerSettingsUsecase(service).execute(None, draft()) == "saved"
    DeleteBrokerSettingsUsecase(service).execute("broker-1")

    assert service.saved == [(None, draft())]
    assert service.deleted == ["broker-1"]


def test_usecase_maps_service_error_to_readable_transport_neutral_error() -> None:
    service = StubBrokerConfigurationService(error=BrokerNotFoundError())

    with pytest.raises(UseCaseError) as error:
        DeleteBrokerSettingsUsecase(service).execute("missing")

    assert error.value.code == "BROKER_NOT_FOUND"
    assert error.value.message == "Настройки площадки не найдены."


def test_usecase_preserves_safe_field_errors() -> None:
    field_error = FieldError("display_name", "REQUIRED", "Укажите название брокера.")
    service = StubBrokerConfigurationService(error=InvalidBrokerConfigurationError((field_error,)))

    with pytest.raises(UseCaseError) as caught:
        SaveBrokerSettingsUsecase(service).execute(None, draft())

    assert caught.value.fields == (field_error,)
