"""Create Worker recovery state from an immutable broker-position snapshot."""

from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.config import StrategySettings


class PositionBootstrapRepositoryPort(Protocol):
    def ensure_position_bootstrap(self, command: AutomationCommand) -> bool: ...


class BootstrapResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    applied: bool
    emitted_facts: int


class PositionBootstrapService:
    def __init__(self, repository: PositionBootstrapRepositoryPort) -> None:
        self._repository = repository

    def ensure(
        self,
        command: AutomationCommand,
        settings: StrategySettings,
    ) -> BootstrapResult:
        del settings  # Bootstrap restores state even when new decisions are disabled.
        snapshot = command.bootstrap
        if snapshot is None:
            return BootstrapResult(applied=False, emitted_facts=0)
        if command.state is not AutomationState.HOLD:
            raise ValueError("Broker-position bootstrap command must remain HOLD until acknowledgement.")
        if snapshot.currency.upper() != command.currency.upper():
            raise ValueError("Broker-position bootstrap currency does not match the instrument.")
        expected_invested = snapshot.average_price * Decimal(command.lot_size) * snapshot.quantity_lots
        if snapshot.invested_amount != expected_invested:
            raise ValueError("Broker-position bootstrap invested amount is inconsistent.")
        applied = self._repository.ensure_position_bootstrap(command)
        return BootstrapResult(applied=applied, emitted_facts=4 if applied else 0)
