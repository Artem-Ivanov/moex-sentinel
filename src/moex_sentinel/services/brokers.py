"""Business rules for broker settings."""

from moex_sentinel.domain.brokers import (
    Broker,
    BrokerAdapterNotFoundError,
    BrokerDraft,
    BrokerField,
    BrokerRecordConstraintError,
    BrokerRecordDuplicateError,
    BrokerRecordNotFoundError,
    BrokerSettings,
)
from moex_sentinel.domain.errors import FieldError
from moex_sentinel.domain.user_brokers import (
    UserBroker,
    UserBrokerConstraintError,
    UserBrokerDraft,
    UserBrokerDuplicateError,
    UserBrokerNotFoundError,
    UserBrokerState,
)
from moex_sentinel.services.environment import EnvironmentStatePort
from moex_sentinel.services.ports import BrokerRegistryPort

SANDBOX_FQDN = "sandbox-invest-public-api.tbank.ru:443"


class BrokerConfigurationError(Exception):
    """Base business error for broker settings."""


class BrokerNotFoundError(BrokerConfigurationError):
    """The requested broker does not exist."""


class DuplicateBrokerError(BrokerConfigurationError):
    """A broker with the same identity already exists."""


class UnknownBrokerAdapterError(BrokerConfigurationError):
    """The selected broker adapter is not supported."""


class InvalidBrokerConfigurationError(BrokerConfigurationError):
    """Broker settings violate business rules."""

    def __init__(self, fields: tuple[FieldError, ...]) -> None:
        super().__init__("Broker settings are invalid.")
        self.fields = fields


class UserBrokerRepositoryPort:
    def list(self) -> list[UserBroker]: ...

    def get(self, user_broker_id: str) -> UserBroker: ...

    def create(self, draft: UserBrokerDraft) -> UserBroker: ...

    def replace(self, user_broker_id: str, draft: UserBrokerDraft) -> UserBroker: ...

    def disable(self, user_broker_id: str) -> UserBroker: ...


class BrokerConfigurationService:
    def __init__(
        self,
        repository: UserBrokerRepositoryPort,
        registry: BrokerRegistryPort,
        environment: EnvironmentStatePort | None = None,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._environment = environment

    def view_settings(self) -> BrokerSettings:
        is_test = self._active_environment() == "TEST"
        return BrokerSettings(
            adapters=self._registry.list() if is_test else (),
            brokers=tuple(self._broker(item) for item in self._repository.list() if item.is_test is is_test),
        )

    def save_settings(self, broker_id: str | None, draft: BrokerDraft) -> Broker:
        self._validate(draft)
        try:
            current = None if broker_id is None else self._repository.get(broker_id)
            user_draft = self._draft(draft, current)
            if broker_id is None:
                return self._broker(self._repository.create(user_draft))
            return self._broker(self._repository.replace(broker_id, user_draft))
        except (BrokerRecordDuplicateError, UserBrokerDuplicateError) as error:
            raise DuplicateBrokerError("A broker with this identity already exists.") from error
        except (BrokerRecordNotFoundError, UserBrokerNotFoundError) as error:
            raise BrokerNotFoundError("Broker was not found.") from error
        except (BrokerRecordConstraintError, UserBrokerConstraintError) as error:
            raise InvalidBrokerConfigurationError(
                (FieldError("broker", "CONSTRAINT", "Настройки брокера некорректны."),)
            ) from error

    def delete_settings(self, broker_id: str) -> None:
        try:
            self._repository.disable(broker_id)
        except UserBrokerNotFoundError:
            raise BrokerNotFoundError("Broker was not found.")

    @staticmethod
    def _draft(draft: BrokerDraft, current: UserBroker | None) -> UserBrokerDraft:
        values = {field.name: field.value.strip() for field in draft.fields}
        fqdn = values.pop("fqdn")
        account_id = draft.account_id or (None if current is None else current.external_account_id)
        state = (
            UserBrokerState.DISABLED
            if not draft.enabled
            else UserBrokerState.ACTIVE if account_id else UserBrokerState.DRAFT
        )
        return UserBrokerDraft(
            api_slug=draft.adapter_code,
            display_name=draft.display_name.strip(),
            environment="TEST",
            fqdn=fqdn,
            settings=values,
            external_account_id=account_id,
            state=state,
        )

    @staticmethod
    def _broker(value: UserBroker) -> Broker:
        fields = tuple(BrokerField(name=name, value=str(item)) for name, item in sorted(value.settings.items()))
        fields += (BrokerField(name="fqdn", value=value.fqdn),)
        return Broker(
            id=value.id,
            display_name=value.display_name,
            provider_code="TINVEST",
            environment_code="SANDBOX",
            adapter_code=value.api_slug,
            enabled=value.enabled,
            fields=fields,
            created_at=value.created_at,
            updated_at=value.updated_at,
            is_test=value.is_test,
            account_id=value.external_account_id,
        )

    def _validate(self, draft: BrokerDraft) -> None:
        errors: list[FieldError] = []
        if self._active_environment() != "TEST" or not draft.is_test:
            errors.append(FieldError("is_test", "TEST_REQUIRED", "Доступен только тестовый контур."))
        if not draft.display_name.strip():
            errors.append(FieldError("display_name", "REQUIRED", "Укажите название брокера."))
        if errors:
            raise InvalidBrokerConfigurationError(tuple(errors))
        try:
            adapter = self._registry.get(draft.adapter_code)
        except BrokerAdapterNotFoundError as error:
            raise UnknownBrokerAdapterError("Broker adapter is not supported.") from error

        if draft.provider_code != adapter.provider_code or draft.environment_code != adapter.environment_code:
            raise InvalidBrokerConfigurationError(
                (FieldError("adapter_code", "MISMATCH", "Параметры адаптера не совпадают."),)
            )

        names = [field.name for field in draft.fields]
        definitions = {field.name: field for field in adapter.fields}
        missing = {field.name for field in adapter.fields if field.required} - set(names)
        unknown = set(names) - definitions.keys()
        if len(names) != len(set(names)) or missing or unknown:
            field_errors = tuple(
                FieldError(f"fields.{name}", "INVALID", "Проверьте поле подключения.")
                for name in sorted(missing | unknown)
            )
            raise InvalidBrokerConfigurationError(
                field_errors or (FieldError("fields", "DUPLICATE", "Поля подключения повторяются."),)
            )
        if any(not field.name.strip() or not field.value.strip() for field in draft.fields):
            invalid = tuple(
                FieldError(f"fields.{field.name}", "REQUIRED", "Заполните поле подключения.")
                for field in draft.fields
                if not field.name.strip() or not field.value.strip()
            )
            raise InvalidBrokerConfigurationError(invalid)

        values = {field.name: field.value for field in draft.fields}
        if values["fqdn"] != SANDBOX_FQDN:
            raise InvalidBrokerConfigurationError((FieldError("fields.fqdn", "NOT_ALLOWED", "Адрес API не разрешён."),))

    def _active_environment(self) -> str:
        return "TEST" if self._environment is None else self._environment.view().active_environment
