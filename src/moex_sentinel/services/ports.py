"""Dependency-injection ports consumed by business Services."""

from typing import Protocol

from moex_sentinel.domain.brokers import BrokerAdapterDefinition
from moex_sentinel.domain.user_brokers import UserBroker


class UserBrokerLookupPort(Protocol):
    """Read one configured scope, raising BrokerRecordNotFoundError if absent."""

    def get(self, broker_id: str) -> UserBroker: ...


class UserBrokerReadPort(UserBrokerLookupPort, Protocol):
    """Read persisted scopes, including archived history."""

    def list(self) -> list[UserBroker]: ...


class BrokerRegistryPort(Protocol):
    def list(self) -> tuple[BrokerAdapterDefinition, ...]: ...

    def get(self, adapter_code: str) -> BrokerAdapterDefinition: ...
