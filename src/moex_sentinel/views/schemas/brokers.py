"""HTTP schemas for broker settings."""

from datetime import datetime

from pydantic import ConfigDict

from moex_sentinel.domain.brokers import BrokerDraft, BrokerField, BrokerSettings
from sentinel_contracts.base import PositionalModel


class BrokerFieldSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: str


class BrokerDraftSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str
    provider_code: str
    environment_code: str
    adapter_code: str
    enabled: bool
    fields: list[BrokerFieldSchema]
    is_test: bool
    account_id: str | None = None

    def to_domain(self) -> BrokerDraft:
        return BrokerDraft(
            display_name=self.display_name,
            provider_code=self.provider_code,
            environment_code=self.environment_code,
            adapter_code=self.adapter_code,
            enabled=self.enabled,
            fields=tuple(BrokerField(name=field.name, value=field.value) for field in self.fields),
            is_test=self.is_test,
            account_id=self.account_id,
        )


class BrokerFieldDefinitionSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    required: bool
    default_value: str | None


class BrokerAdapterSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")

    adapter_code: str
    provider_code: str
    environment_code: str
    fields: list[BrokerFieldDefinitionSchema]


class BrokerSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    provider_code: str
    environment_code: str
    adapter_code: str
    enabled: bool
    is_test: bool
    account_id: str | None
    fields: list[BrokerFieldSchema]
    created_at: datetime
    updated_at: datetime


class BrokerSettingsSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")

    adapters: list[BrokerAdapterSchema]
    brokers: list[BrokerSchema]

    @classmethod
    def from_domain(cls, settings: BrokerSettings) -> "BrokerSettingsSchema":
        return cls(
            adapters=[
                BrokerAdapterSchema(
                    adapter_code=adapter.adapter_code,
                    provider_code=adapter.provider_code,
                    environment_code=adapter.environment_code,
                    fields=[
                        BrokerFieldDefinitionSchema(
                            name=field.name,
                            required=field.required,
                            default_value=field.default_value,
                        )
                        for field in adapter.fields
                    ],
                )
                for adapter in settings.adapters
            ],
            brokers=[broker_to_schema(broker) for broker in settings.brokers],
        )


def broker_to_schema(broker: object) -> BrokerSchema:
    return BrokerSchema.model_validate(broker, from_attributes=True)
