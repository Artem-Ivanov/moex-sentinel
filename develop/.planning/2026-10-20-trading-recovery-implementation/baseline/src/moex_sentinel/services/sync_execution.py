"""Transport-neutral execution of a synchronous operation in the current context."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeVar, cast

T = TypeVar("T")
SyncRunner = Callable[[Callable[[], object]], Awaitable[object]]
_RUNNER: ContextVar[SyncRunner | None] = ContextVar("sync_runner", default=None)


async def inline_runner(operation: Callable[[], T]) -> T:
    return operation()


async def run_sync(operation: Callable[[], T], *, fallback: SyncRunner = inline_runner) -> T:
    return cast(T, await (_RUNNER.get() or fallback)(operation))


@contextmanager
def bind_sync_runner(runner: SyncRunner) -> Iterator[None]:
    token = _RUNNER.set(runner)
    try:
        yield
    finally:
        _RUNNER.reset(token)
