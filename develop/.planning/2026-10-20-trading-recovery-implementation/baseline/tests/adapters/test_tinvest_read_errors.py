"""Real SDK exception contracts at the public read-adapter boundary."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from grpc import StatusCode
from t_tech.invest.exceptions import AioRequestError

import moex_sentinel.adapters.tinvest.market_data as market_module
import moex_sentinel.adapters.tinvest.portfolio as portfolio_module
from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.adapters.tinvest.market_data import TInvestMarketDataAdapter
from moex_sentinel.adapters.tinvest.portfolio import SANDBOX_TARGET, TInvestPortfolioAdapter


@pytest.fixture(params=[pytest.param("portfolio", id="portfolio"), pytest.param("market", id="market")])
def read_boundary(request):
    sdk_call = AsyncMock()

    class ClientContext:
        async def __aenter__(self):
            return SimpleNamespace(
                sandbox=SimpleNamespace(get_sandbox_accounts=sdk_call),
                market_data=SimpleNamespace(get_last_prices=sdk_call),
            )

        async def __aexit__(self, *_args):
            return None

    factory = lambda *_args, **_kwargs: ClientContext()  # noqa: E731 - local SDK factory
    if request.param == "portfolio":
        adapter = TInvestPortfolioAdapter("synthetic-token", SANDBOX_TARGET, client_factory=factory)
        read = adapter.list_accounts
    else:
        adapter = TInvestMarketDataAdapter("synthetic-token", SANDBOX_TARGET, client_factory=factory)
        read = lambda: adapter.get_last_prices(("uid-1",))  # noqa: E731 - same public-call contract
    return read, sdk_call, request.param


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        pytest.param(StatusCode.UNAUTHENTICATED, "BROKER_AUTH_FAILED", False, id="authentication"),
        pytest.param(StatusCode.PERMISSION_DENIED, "BROKER_FORBIDDEN", False, id="permission"),
        pytest.param(StatusCode.RESOURCE_EXHAUSTED, "BROKER_RATE_LIMITED", True, id="rate-limit"),
        pytest.param(StatusCode.UNAVAILABLE, "BROKER_UNAVAILABLE", True, id="unavailable"),
        pytest.param(StatusCode.DEADLINE_EXCEEDED, "BROKER_UNAVAILABLE", True, id="deadline"),
        pytest.param(StatusCode.INTERNAL, "BROKER_UNAVAILABLE", False, id="unknown-sdk-status"),
    ],
)
def test_real_sdk_errors_preserve_status_retryability_and_secrecy(read_boundary, status, code, retryable):
    read, sdk_call, _kind = read_boundary
    sdk_call.side_effect = AioRequestError(status, "transport detail synthetic-token", metadata=None)

    with pytest.raises(TInvestAdapterError) as error:
        asyncio.run(read())

    assert error.value.code == code
    assert error.value.retryable is retryable
    assert "transport detail" not in str(error.value)
    assert "synthetic-token" not in str(error.value)
    sdk_call.assert_awaited_once()


def test_unexpected_converter_error_propagates_unchanged(read_boundary, monkeypatch):
    read, sdk_call, kind = read_boundary
    failure = RuntimeError("synthetic converter defect")

    def fail_conversion(*_args, **_kwargs):
        raise failure

    if kind == "portfolio":
        sdk_call.return_value = SimpleNamespace(
            accounts=[SimpleNamespace(id="account-1", name="Main", status="OPEN", type="BROKER")]
        )
        monkeypatch.setattr(portfolio_module, "BrokerAccount", fail_conversion)
    else:
        sdk_call.return_value = SimpleNamespace(
            last_prices=[
                SimpleNamespace(
                    instrument_uid="uid-1",
                    price=SimpleNamespace(units=100, nano=0),
                    time=datetime(2026, 8, 5, tzinfo=UTC),
                )
            ]
        )
        monkeypatch.setattr(market_module, "quotation_to_decimal", fail_conversion)

    with pytest.raises(RuntimeError) as error:
        asyncio.run(read())

    assert error.value is failure


def test_malformed_sdk_payload_has_safe_nonretryable_error(read_boundary):
    read, sdk_call, kind = read_boundary
    if kind == "portfolio":
        sdk_call.return_value = SimpleNamespace(
            accounts=[SimpleNamespace(id=None, name="synthetic-token", status="OPEN", type="BROKER")]
        )
    else:
        sdk_call.return_value = SimpleNamespace(
            last_prices=[
                SimpleNamespace(
                    instrument_uid="uid-1",
                    price=SimpleNamespace(units=100, nano=0),
                    time="synthetic-token-invalid-timestamp",
                )
            ]
        )

    with pytest.raises(TInvestAdapterError) as error:
        asyncio.run(read())

    assert error.value.code == "BROKER_UNAVAILABLE"
    assert error.value.retryable is False
    assert "synthetic-token" not in str(error.value)
    assert "validation" not in str(error.value).lower()
