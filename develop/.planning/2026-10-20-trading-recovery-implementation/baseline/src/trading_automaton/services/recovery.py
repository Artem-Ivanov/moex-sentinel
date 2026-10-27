"""Fail-safe restart recovery."""

from typing import Protocol

from sentinel_contracts.trading_facts import AutomationCommand


class RecoveryRepositoryPort(Protocol):
    def list_monitored(self) -> list[AutomationCommand]: ...

    def get_active_intent(self, automation_id: str) -> object | None: ...

    def hold_active(self, reason: str, automation_id: str | None = None) -> None: ...


class RecoveryService:
    def __init__(self, repository: RecoveryRepositoryPort) -> None:
        self._repository = repository

    def recover(self, *, unclean_shutdown: bool) -> None:
        if not unclean_shutdown:
            return
        for command in self._repository.list_monitored():
            if command.bootstrap is not None:
                continue
            if self._repository.get_active_intent(str(command.automation_id)) is not None:
                continue
            self._repository.hold_active("Worker restarted; manual RESUME is required", str(command.automation_id))
