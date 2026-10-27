"""Offline contract checks for the pinned official T-Invest SDK."""

from importlib.metadata import version
from typing import Any

from t_tech.invest import AsyncClient
from t_tech.invest.async_services import (
    InstrumentsService,
    MarketDataService,
    OperationsService,
    OrdersService,
    SandboxService,
)
from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX


def _has_callables(target: Any, methods: tuple[str, ...]) -> None:
    for method_name in methods:
        assert callable(getattr(target, method_name)), f"Missing method: {target.__name__}.{method_name}"


def test_official_tinvest_sdk_uses_the_pinned_1_49_line() -> None:
    installed = tuple(int(part) for part in version("t-tech-investments").split(".")[:3])

    assert (1, 49, 3) <= installed < (1, 50, 0)


def test_sdk_exposes_the_async_read_only_sandbox_surface() -> None:
    assert INVEST_GRPC_API_SANDBOX == "sandbox-invest-public-api.tbank.ru"
    assert AsyncClient is not None
    assert callable(SandboxService.open_sandbox_account)
    assert callable(SandboxService.sandbox_pay_in)
    assert callable(SandboxService.get_sandbox_accounts)
    assert callable(SandboxService.get_sandbox_portfolio)
    assert callable(SandboxService.get_sandbox_positions)
    assert callable(OperationsService.get_operations_by_cursor)


def test_sdk_market_data_surface_is_complete_for_adapters() -> None:
    _has_callables(
        MarketDataService,
        (
            "get_last_prices",
            "get_order_book",
            "get_candles",
            "get_trading_status",
        ),
    )


def test_sdk_instruments_surface_is_complete_for_catalog_and_search() -> None:
    _has_callables(
        InstrumentsService,
        (
            "shares",
            "bonds",
            "currencies",
            "futures",
            "etfs",
            "find_instrument",
            "get_instrument_by",
        ),
    )


def test_sdk_orders_surface_is_complete_for_execution_flow() -> None:
    _has_callables(
        OrdersService,
        (
            "post_order",
            "get_order_price",
            "get_order_state",
            "get_orders",
            "cancel_order",
        ),
    )
