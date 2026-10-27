"""Safety transition for a failed durable decision write."""

import asyncio
from typing import Protocol

from sentinel_contracts.trading_facts import AutomationCommand


class PersistenceFailureRepositoryPort(Protocol):
    def hold_active(self, reason: str, automation_id: str | None = None) -> None: ...


class PersistenceFailureService:
    def __init__(self, repository: PersistenceFailureRepositoryPort) -> None:
        self._repository = repository

    async def hold(self, commands: tuple[AutomationCommand, ...]) -> None:
        for command in commands:
            await asyncio.to_thread(
                self._repository.hold_active,
                "Durable decision persistence failed; manual RESUME is required",
                str(command.automation_id),
            )
