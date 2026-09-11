"""User-intent use cases for broker settings."""

from typing import Protocol

from pydantic import ConfigDict, SkipValidation

from moex_sentinel.domain.brokers import Broker, BrokerDraft, BrokerSettings
from moex_sentinel.services.brokers import (
    BrokerNotFoundError,
    DuplicateBrokerError,
    InvalidBrokerConfigurationError,
    UnknownBrokerAdapterError,
)
from moex_sentinel.usecases.errors import UseCaseError
from sentinel_contracts.base import PositionalModel


class BrokerConfigurationServicePort(Protocol):
    def view_settings(self) -> BrokerSettings: ...

    def save_settings(self, broker_id: str | None, draft: BrokerDraft) -> Broker: ...

    def delete_settings(self, broker_id: str) -> None: ...


class ViewBrokerSettingsUsecase(PositionalModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    service: SkipValidation[BrokerConfigurationServicePort]

    def execute(self) -> BrokerSettings:
        try:
            return self.service.view_settings()
        except BrokerNotFoundError as error:
            raise UseCaseError("BROKER_NOT_FOUND", "Настройки площадки не найдены.") from error


class SaveBrokerSettingsUsecase(PositionalModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    service: SkipValidation[BrokerConfigurationServicePort]

    def execute(self, broker_id: str | None, draft: BrokerDraft) -> Broker:
        try:
            return self.service.save_settings(broker_id, draft)
        except DuplicateBrokerError as error:
            raise UseCaseError("DUPLICATE_BROKER", "Такая настройка площадки уже существует.") from error
        except UnknownBrokerAdapterError as error:
            raise UseCaseError("UNKNOWN_ADAPTER", "Выбранный адаптер площадки не поддерживается.") from error
        except InvalidBrokerConfigurationError as error:
            raise UseCaseError(
                "INVALID_BROKER_CONFIGURATION",
                "Проверьте настройки брокера.",
                error.fields,
            ) from error


class DeleteBrokerSettingsUsecase(PositionalModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    service: SkipValidation[BrokerConfigurationServicePort]

    def execute(self, broker_id: str) -> None:
        try:
            self.service.delete_settings(broker_id)
        except BrokerNotFoundError as error:
            raise UseCaseError("BROKER_NOT_FOUND", "Настройки площадки не найдены.") from error
