"""Domain contracts for configured broker API/account scopes."""

from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, model_validator

from sentinel_contracts.base import PositionalModel


class BrokerApiNotFoundError(LookupError):
    """A requested broker API module is not registered."""


class BrokerApiEnvironmentError(ValueError):
    """A broker API module does not support the requested environment."""


class BrokerApiRegistryDuplicateError(ValueError):
    """More than one broker API module declares the same stable slug."""


class UserBrokerNotFoundError(LookupError):
    """A configured user-broker scope does not exist."""


class UserBrokerDuplicateError(ValueError):
    """A configured API/account scope already exists."""


class UserBrokerConstraintError(ValueError):
    """User-broker persistence rejected an invalid record."""


class UserBrokerState(StrEnum):
    """Lifecycle of one configured API/account scope."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class BrokerApiFieldDescriptor(PositionalModel):
    """One UI-facing field declared by a code-owned API module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    required: bool
    value_type: str


class BrokerApiDescriptor(PositionalModel):
    """Non-persisted description of a broker API module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_slug: str
    display_name: str
    environments: tuple[str, ...]
    fields: tuple[BrokerApiFieldDescriptor, ...]


class UserBrokerDraft(PositionalModel):
    """Validated settings for one selected API and external account."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    api_slug: str
    display_name: str
    environment: str
    fqdn: str
    settings: dict[str, object]
    external_account_id: str | None
    state: UserBrokerState

    @model_validator(mode="after")
    def validate_active_account(self) -> "UserBrokerDraft":
        if self.state is UserBrokerState.ACTIVE and not (self.external_account_id or "").strip():
            raise ValueError("ACTIVE user broker requires an external account ID")
        return self


class UserBroker(UserBrokerDraft):
    """Persisted configured broker API/account scope."""

    id: str
    created_at: datetime
    updated_at: datetime

    @property
    def adapter_code(self) -> str:
        return self.api_slug

    @property
    def provider_code(self) -> str:
        return "TINVEST"

    @property
    def environment_code(self) -> str:
        return "SANDBOX" if self.environment == "TEST" else self.environment

    @property
    def enabled(self) -> bool:
        return self.state is UserBrokerState.ACTIVE

    @property
    def account_id(self) -> str | None:
        """Expose the selected account through the shared broker port name."""
        return self.external_account_id

    @property
    def is_test(self) -> bool:
        return self.environment == "TEST"

    @property
    def fields(self) -> tuple[object, ...]:
        from moex_sentinel.domain.brokers import BrokerField  # noqa: PLC0415

        values = {name: str(value) for name, value in self.settings.items()}
        values["fqdn"] = self.fqdn
        return tuple(BrokerField(name=name, value=value) for name, value in sorted(values.items()))
