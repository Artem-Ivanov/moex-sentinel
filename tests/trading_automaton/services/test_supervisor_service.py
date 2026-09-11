"""Round-robin and fail-safe ordering for one worker iteration."""

import asyncio
from dataclasses import dataclass

from trading_automaton.services.supervisor import TradingSupervisorService


@dataclass
class Item:
    automation_id: str


class RepositoryStub:
    def list_active(self):
        return [Item("a-1"), Item("a-2")]


class CoreStub:
    def __init__(self, calls: list[str], states: dict[str, str] | None = None) -> None:
        self.calls = calls
        self.states = states or {}

    async def automation_status(self, automation_id: str):
        self.calls.append(f"core:{automation_id}")
        return {"state": self.states.get(automation_id, "IN_WORK"), "revision": 1}


class BrokerStub:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def read_state(self, item: Item):
        self.calls.append(f"broker:{item.automation_id}")
        return {"price": "100"}


class ProcessorStub:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def process(self, item: Item, core_state: object, broker_state: object):
        self.calls.append(f"process:{item.automation_id}")


async def no_sleep(_delay: float) -> None:
    return None


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def test_checks_core_before_broker_and_processes_items_sequentially() -> None:
    calls: list[str] = []
    service = TradingSupervisorService(
        RepositoryStub(),
        CoreStub(calls),
        BrokerStub(calls),
        ProcessorStub(calls),
        monotonic=iter([0.0, 1.2]).__next__,
        sleep=no_sleep,
    )

    asyncio.run(service.run_iteration())

    assert calls == [
        "core:a-1",
        "broker:a-1",
        "process:a-1",
        "core:a-2",
        "broker:a-2",
        "process:a-2",
    ]


def test_hold_and_stopped_items_skip_broker_and_decision_processing() -> None:
    calls: list[str] = []
    service = TradingSupervisorService(
        RepositoryStub(),
        CoreStub(calls, {"a-1": "HOLD", "a-2": "CLOSED"}),
        BrokerStub(calls),
        ProcessorStub(calls),
        monotonic=iter([0.0, 0.1]).__next__,
        sleep=no_sleep,
    )

    asyncio.run(service.run_iteration())

    assert calls == ["core:a-1", "core:a-2"]


def test_waits_only_for_the_remaining_part_of_one_second_target() -> None:
    sleep = SleepRecorder()
    service = TradingSupervisorService(
        RepositoryStub(),
        CoreStub([]),
        BrokerStub([]),
        ProcessorStub([]),
        monotonic=iter([10.0, 10.25]).__next__,
        sleep=sleep,
    )

    asyncio.run(service.run_iteration())

    assert sleep.delays == [0.75]
