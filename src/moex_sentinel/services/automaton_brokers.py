"""Validated short-lived broker connection metadata for the worker."""

from typing import Protocol

from moex_sentinel.domain.brokers import Broker
from moex_sentinel.services.environment import EnvironmentMismatchError, EnvironmentStatePort
from sentinel_contracts.broker_execution import BrokerConnection, BrokerScope
from sentinel_contracts.tinvest import resolve_tinvest_endpoint


class BrokerRepositoryPort(Protocol):
    def get(self, broker_id: str) -> Broker: ...


class AutomatonBrokerService:
    def __init__(
        self,
        repository: BrokerRepositoryPort,
        *,
        access_mode: str = "READ_ONLY",
        environment: EnvironmentStatePort | None = None,
    ) -> None:
        self._repository = repository
        self._access_mode = access_mode
        self._environment = environment

    def scope(self, broker_id: str) -> BrokerScope:
        broker = self._repository.get(broker_id)
        environment = "TEST" if broker.is_test else "PROD"
        if self._environment is not None and environment != self._environment.view().active_environment:
            raise EnvironmentMismatchError("Broker belongs to inactive environment.")
        if not broker.account_id:
            raise ValueError("Broker scope requires a selected account.")
        return BrokerScope(broker_id=broker.id, environment=environment, account_id=broker.account_id)

    def connection(self, broker_id: str) -> BrokerConnection:
        broker = self._repository.get(broker_id)
        fields = {field.name: field.value for field in broker.fields}
        token = fields.get("token", "")
        target = fields.get("fqdn", "")
        state = getattr(broker, "state", None)
        state_value = getattr(state, "value", state)
        if not broker.enabled or state_value not in {None, "ACTIVE"} or not token:
            raise ValueError("Broker connection is unavailable for Sandbox execution.")
        environment = "TEST" if broker.is_test else "PROD"
        if self._environment is not None and environment != self._environment.view().active_environment:
            raise EnvironmentMismatchError("Broker belongs to inactive environment.")
        resolve_tinvest_endpoint(environment, broker.adapter_code, target)
        if environment == "PROD" and self._access_mode != "READ_ONLY":
            raise ValueError("PROD TRADE requires a separate trading admission.")
        return BrokerConnection(
            broker_id=broker.id,
            adapter_code=broker.adapter_code,
            target=target,
            token=token,
            is_test=broker.is_test,
            environment=environment,
            access_mode=self._access_mode,
            account_id=broker.account_id or "",
        )
