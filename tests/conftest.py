"""Minimal pytest support for async tests in offline environments."""

from __future__ import annotations

import asyncio
import inspect
import dataclasses

import pytest
from sentinel_contracts.base import PositionalModel


_original_replace = dataclasses.replace
_original_asdict = dataclasses.asdict


def _is_legacy_positional(obj: object) -> bool:
    return isinstance(obj, PositionalModel)


def _replace(model: object, /, **changes: object) -> object:
    if _is_legacy_positional(model):
        if not changes:
            return model
        return model.__class__(**{**model.model_dump(), **changes})
    return _original_replace(model, **changes)


def _asdict(model: object) -> dict:
    if _is_legacy_positional(model):
        return model.model_dump()
    return _original_asdict(model)


if not getattr(dataclasses.replace, "_legacy_positional_compat", False):
    dataclasses.replace = _replace
    dataclasses.replace._legacy_positional_compat = True  # type: ignore[attr-defined]
if not getattr(dataclasses.asdict, "_legacy_positional_compat", False):
    dataclasses.asdict = _asdict
    dataclasses.asdict._legacy_positional_compat = True  # type: ignore[attr-defined]


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
