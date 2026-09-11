"""Single-threaded round-robin coordination for active automations."""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol


class ActiveItem(Protocol):
    automation_id: str


class RepositoryPort(Protocol):
    def list_active(self) -> list[ActiveItem]: ...


class CorePort(Protocol):
    async def automation_status(self, automation_id: str) -> dict[str, Any]: ...


class BrokerPort(Protocol):
    async def read_state(self, item: ActiveItem) -> object: ...


class ProcessorPort(Protocol):
    async def process(self, item: ActiveItem, core_state: object, broker_state: object) -> None: ...


class TradingSupervisorService:
    def __init__(
        self,
        repository: RepositoryPort,
        core: CorePort,
        broker: BrokerPort,
        processor: ProcessorPort,
        *,
        monotonic: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        target_interval_seconds: float = 1.0,
    ) -> None:
        self._repository = repository
        self._core = core
        self._broker = broker
        self._processor = processor
        self._monotonic = monotonic
        self._sleep = sleep
        self._target_interval = target_interval_seconds

    async def run_iteration(self) -> None:
        started_at = self._monotonic()
        for item in self._repository.list_active():
            core_state = await self._core.automation_status(item.automation_id)
            if core_state["state"] in {"HOLD", "CLOSED"}:
                continue
            broker_state = await self._broker.read_state(item)
            await self._processor.process(item, core_state, broker_state)
        elapsed = self._monotonic() - started_at
        remaining = self._target_interval - elapsed
        if remaining > 0:
            await self._sleep(remaining)
