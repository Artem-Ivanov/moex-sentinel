"""Domain records for broker settings."""

from datetime import datetime

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class BrokerRecordNotFoundError(LookupError):
    """Broker persistence port could not find a record."""


class BrokerRecordDuplicateError(ValueError):
    """Broker persistence port rejected a duplicate identity."""


class BrokerRecordConstraintError(ValueError):
    """Broker persistence port rejected invalid persisted values."""


class BrokerAdapterNotFoundError(LookupError):
    """Broker registry port could not find an adapter declaration."""


class BrokerField(PositionalModel):
    model_config = ConfigDict(frozen=True)
    name: str
    value: str


class BrokerDraft(PositionalModel):
    model_config = ConfigDict(frozen=True)
    display_name: str
    provider_code: str
    environment_code: str
    adapter_code: str
    enabled: bool
    fields: tuple[BrokerField, ...]
    is_test: bool = True
    account_id: str | None = None


class Broker(PositionalModel):
    model_config = ConfigDict(frozen=True)
    id: str
    display_name: str
    provider_code: str
    environment_code: str
    adapter_code: str
    enabled: bool
    fields: tuple[BrokerField, ...]
    created_at: datetime
    updated_at: datetime
    is_test: bool = True
    account_id: str | None = None


class BrokerFieldDefinition(PositionalModel):
    model_config = ConfigDict(frozen=True)
    name: str
    required: bool
    default_value: str | None = None


class BrokerAdapterDefinition(PositionalModel):
    model_config = ConfigDict(frozen=True)
    adapter_code: str
    provider_code: str
    environment_code: str
    fields: tuple[BrokerFieldDefinition, ...]


class BrokerSettings(PositionalModel):
    model_config = ConfigDict(frozen=True)
    adapters: tuple[BrokerAdapterDefinition, ...]
    brokers: tuple[Broker, ...]
