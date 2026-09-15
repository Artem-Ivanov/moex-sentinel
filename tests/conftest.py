"""Minimal pytest support for async tests in offline environments."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import ExitStack

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.trading_automaton.storage.worker_storage_helpers import NOW
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base
from trading_automaton.storage.repository import LocalAutomationRepository


@pytest.fixture
def worker_repository_factory():
    with ExitStack() as resources:

        def create():
            engine = create_engine("sqlite:///:memory:")
            resources.callback(engine.dispose)
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            return LocalAutomationRepository(factory, fact_writer=FactOutboxWriter(clock=lambda: NOW)), factory

        yield create


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "asyncio: Mark coroutine test functions to be executed in an event loop.",
    )


@pytest.hookimpl
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> object | None:
    if "asyncio" in pyfuncitem.keywords:
        test_func = pyfuncitem.obj
        if inspect.iscoroutinefunction(test_func):
            func_kwargs = {arg_name: pyfuncitem.funcargs[arg_name] for arg_name in pyfuncitem._fixtureinfo.argnames}
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(test_func(**func_kwargs))
            finally:
                loop.close()
            return True
    return None
