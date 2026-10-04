"""Business lifecycle for baseline trading automations owned by Core."""

from typing import Protocol

from moex_sentinel.domain.repository_records import AutomationRecord
from moex_sentinel.services.environment import EnvironmentMismatchError, EnvironmentStatePort
from moex_sentinel.services.ports import BrokerRepositoryPort
from sentinel_contracts.automation_lifecycle import (
    InvalidAutomationTransition,
    TransitionOrigin,
    validate_automation_transition,
)
from sentinel_contracts.trading import AutomationState


class AutomationStateConflictError(ValueError):
    pass


class AutomationRepositoryPort(Protocol):
    def create(self, *, broker_id: str, account_id: str, instrument_id: str) -> AutomationRecord: ...
    def get(self, automation_id: str) -> AutomationRecord: ...
    def get_many(self, automation_ids: list[str]) -> list[AutomationRecord]: ...
    def list_active(self) -> list[AutomationRecord]: ...
    def set_state(
        self,
        automation_id: str,
        state: AutomationState,
        *,
        expected_revision: int,
        hold_reason: str | None = None,
    ) -> AutomationRecord: ...
    def resume_to_queue(self, automation_id: str, *, expected_revision: int) -> AutomationRecord: ...


class AutomationService:
    def __init__(
        self,
        repository: AutomationRepositoryPort,
        *,
        access_mode: str = "READ_ONLY",
        environment: EnvironmentStatePort | None = None,
        brokers: BrokerRepositoryPort | None = None,
    ) -> None:
        self._repository = repository
        self._access_mode = access_mode
        self._environment = environment
        self._brokers = brokers

    def create(self, *, broker_id: str, account_id: str, instrument_id: str) -> AutomationRecord:
        self._require_trade()
        if not self._broker_in_environment(broker_id):
            raise EnvironmentMismatchError("Broker belongs to inactive environment.")
        return self._repository.create(broker_id=broker_id, account_id=account_id, instrument_id=instrument_id)

    def get(self, automation_id: str) -> AutomationRecord:
        value = self._repository.get(automation_id)
        if not self._in_environment(value):
            raise EnvironmentMismatchError("Automation belongs to inactive environment.")
        return value

    def get_many(self, automation_ids: list[str]) -> list[AutomationRecord]:
        return self._filter_environment(self._repository.get_many(automation_ids))

    def list_active(self) -> list[AutomationRecord]:
        return self._filter_environment(self._repository.list_active())

    def _filter_environment(self, values: list[AutomationRecord]) -> list[AutomationRecord]:
        if not values or self._environment is None or self._brokers is None:
            return values
        brokers = {broker.id: broker for broker in self._brokers.list()}
        is_test = None
        records = []
        for value in values:
            broker = brokers.get(value.broker_id)
            if broker is None:
                broker = self._brokers.get(value.broker_id)
            if is_test is None:
                is_test = self._environment.view().active_environment == "TEST"
            if broker.is_test == is_test:
                records.append(value)
        return records

    def hold(self, automation_id: str, reason: str) -> AutomationRecord:
        current = self.get(automation_id)
        if not self._validate_transition(current.state, AutomationState.HOLD):
            return current
        return self._repository.set_state(
            automation_id,
            AutomationState.HOLD,
            expected_revision=current.revision,
            hold_reason=reason,
        )

    def resume(self, automation_id: str) -> AutomationRecord:
        self._require_trade()
        current = self.get(automation_id)
        if not self._validate_transition(current.state, AutomationState.IN_QUEUE):
            return current
        return self._repository.resume_to_queue(automation_id, expected_revision=current.revision)

    def close(self, automation_id: str) -> AutomationRecord:
        self._require_trade()
        current = self.get(automation_id)
        if not self._validate_transition(current.state, AutomationState.CLOSED):
            return current
        return self._repository.set_state(
            automation_id,
            AutomationState.CLOSED,
            expected_revision=current.revision,
        )

    def _in_environment(self, value: AutomationRecord) -> bool:
        return self._broker_in_environment(value.broker_id)

    def _broker_in_environment(self, broker_id: str) -> bool:
        return (
            self._environment is None
            or self._brokers is None
            or self._brokers.get(broker_id).is_test == (self._environment.view().active_environment == "TEST")
        )

    def _require_trade(self) -> None:
        if self._access_mode != "TRADE":
            raise ValueError("Trading commands are forbidden in READ_ONLY.")

    @staticmethod
    def _validate_transition(current: AutomationState, target: AutomationState) -> bool:
        try:
            return validate_automation_transition(current, target, origin=TransitionOrigin.USER_COMMAND)
        except InvalidAutomationTransition as error:
            raise AutomationStateConflictError(str(error)) from error
