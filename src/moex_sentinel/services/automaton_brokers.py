"""Validated short-lived broker connection metadata for the worker."""

from typing import Protocol

from moex_sentinel.domain.brokers import Broker
from moex_sentinel.services.broker_factory import (
    TINVEST_SANDBOX_ADAPTER,
    TINVEST_SANDBOX_TARGET,
)
from sentinel_contracts.broker_execution import BrokerConnection


class BrokerRepositoryPort(Protocol):
    def get(self, broker_id: str) -> Broker: ...


class AutomatonBrokerService:
    def __init__(self, repository: BrokerRepositoryPort) -> None:
        self._repository = repository

    def connection(self, broker_id: str) -> BrokerConnection:
        broker = self._repository.get(broker_id)
        fields = {field.name: field.value for field in broker.fields}
        token = fields.get("token", "")
        target = fields.get("fqdn", "")
        state = getattr(broker, "state", None)
        state_value = getattr(state, "value", state)
        if (
            not broker.enabled
            or state_value not in {None, "ACTIVE"}
            or not broker.is_test
            or broker.adapter_code != TINVEST_SANDBOX_ADAPTER
            or target != TINVEST_SANDBOX_TARGET
            or not token
        ):
            raise ValueError("Broker connection is unavailable for Sandbox execution.")
        return BrokerConnection(
            broker_id=broker.id,
            adapter_code=broker.adapter_code,
            target=target,
            token=token,
            is_test=True,
        )
