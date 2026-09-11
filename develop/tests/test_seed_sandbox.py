# ruff: noqa: INP001

from __future__ import annotations

import importlib
import io
import json
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))
seed_sandbox = importlib.import_module("develop.scripts.seed_sandbox")


SYNTHETIC_TOKEN = "synthetic-token-for-tests"
BROKER_ID = "10000000-0000-4000-8000-000000000001"
INSTRUMENT_ID = "20000000-0000-4000-8000-000000000001"
AUTOMATION_ID = "30000000-0000-4000-8000-000000000001"


def health_response() -> dict[str, object]:
    return {
        "status": "ok",
        "service": "backend",
        "version": "0.1.0",
        "database": "ok",
        "schema": "compatible",
    }


def broker_response(
    *,
    account_id: str | None = "sandbox-account-1",
    enabled: bool | None = None,
    display_name: str = "T-Invest Sandbox",
    field_value: str = SYNTHETIC_TOKEN,
) -> dict[str, object]:
    return {
        "id": BROKER_ID,
        "display_name": display_name,
        "provider_code": "TINVEST",
        "environment_code": "SANDBOX",
        "adapter_code": "TINVEST_SANDBOX",
        "enabled": account_id is not None if enabled is None else enabled,
        "is_test": True,
        "account_id": account_id,
        "fields": [
            {"name": "token", "value": field_value},
            {"name": "fqdn", "value": "sandbox-invest-public-api.tbank.ru:443"},
        ],
        "created_at": "2026-09-08T10:00:00Z",
        "updated_at": "2026-09-08T10:00:00Z",
    }


def automation_response(
    *,
    state: str = "IN_QUEUE",
    broker_id: str = BROKER_ID,
    account_id: str = "sandbox-account-1",
    instrument_id: str = INSTRUMENT_ID,
) -> dict[str, object]:
    return {
        "id": AUTOMATION_ID,
        "broker_id": broker_id,
        "account_id": account_id,
        "instrument_id": instrument_id,
        "state": state,
        "suspended_from_state": None,
        "revision": 1,
        "last_sequence_number": 0,
        "resume_requested": False,
        "currency": "RUB",
        "strategy_code": "adaptive_scalping",
        "strategy_version": "1",
        "quantity_lots": 0,
        "average_price": "0",
        "invested_amount": "0",
        "realized_pnl": "0",
        "unrealized_pnl": "0",
        "net_pnl": "0",
        "actual_commissions": "0",
        "broker_name": "T-Invest Sandbox",
        "ticker": "SYNTH",
        "instrument_name": "Synthetic instrument",
    }


def account_response(account_id: str, *, status: str = "ACCOUNT_STATUS_OPEN") -> dict[str, object]:
    return {
        "broker_id": BROKER_ID,
        "broker_name": "T-Invest Sandbox",
        "account_id": account_id,
        "name": "Synthetic sandbox account",
        "status": status,
        "account_type": "ACCOUNT_TYPE_TINKOFF",
        "total_amount": {"amount": "0", "currency": "RUB"},
        "free_cash": {"amount": "0", "currency": "RUB"},
        "realized_pnl": {"amount": "0", "currency": "RUB"},
        "unrealized_pnl": {"amount": "0", "currency": "RUB"},
    }


class RecordingTransport:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def invoke(
    arguments: list[str],
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    environ: dict[str, str] | None = None,
) -> tuple[int, dict[str, object], RecordingTransport, str]:
    recorder = RecordingTransport(handler)
    output = io.StringIO()
    exit_code = seed_sandbox.main(
        arguments,
        environ={} if environ is None else environ,
        transport=httpx.MockTransport(recorder),
        output=output,
    )
    raw_output = output.getvalue()
    return exit_code, json.loads(raw_output), recorder, raw_output


def test_dry_run_needs_no_token_and_performs_only_read_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            return httpx.Response(200, json=health_response())
        if request.url.path == "/api/brokers":
            return httpx.Response(
                200,
                json={"adapters": [], "brokers": [broker_response()]},
            )
        if request.url.path == "/api/trading-automations":
            return httpx.Response(200, json={"items": [automation_response()]})
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    exit_code, result, recorder, raw_output = invoke([], handler)

    assert exit_code == 0
    assert result == {
        "ok": True,
        "mode": "dry-run",
        "counts": {
            "brokers": 1,
            "matching_brokers": 1,
            "automations": 1,
        },
    }
    assert [request.method for request in recorder.requests] == ["GET", "GET", "GET"]
    assert SYNTHETIC_TOKEN not in raw_output


def test_apply_without_token_fails_before_any_http_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Request must not be sent: {request.method} {request.url.path}")

    exit_code, result, recorder, raw_output = invoke(["--apply"], handler)

    assert exit_code == 2
    assert result == {"ok": False, "code": "TOKEN_REQUIRED"}
    assert recorder.requests == []
    assert "TINVEST_SANDBOX_TOKEN" not in raw_output


@pytest.mark.parametrize(
    "base_url",
    [
        "https://localhost:8080",
        "http://user@localhost:8080",
        "http://localhost:8080?mode=unsafe",
        "http://localhost:8080#fragment",
        "http://192.0.2.10:8080",
    ],
)
def test_non_loopback_or_ambiguous_base_url_is_rejected_before_network(base_url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Request must not be sent: {request.method} {request.url.path}")

    exit_code, result, recorder, _raw_output = invoke(["--base-url", base_url], handler)

    assert exit_code == 2
    assert result == {"ok": False, "code": "INVALID_BASE_URL"}
    assert recorder.requests == []


def test_apply_rejects_account_read_errors_without_leaking_remote_details() -> None:
    expected_create = {
        "display_name": "T-Invest Sandbox",
        "provider_code": "TINVEST",
        "environment_code": "SANDBOX",
        "adapter_code": "TINVEST_SANDBOX",
        "enabled": True,
        "fields": [
            {"name": "token", "value": SYNTHETIC_TOKEN},
            {"name": "fqdn", "value": "sandbox-invest-public-api.tbank.ru:443"},
        ],
        "is_test": True,
        "account_id": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/health":
            return httpx.Response(200, json=health_response())
        if request.method == "GET" and request.url.path == "/api/brokers":
            return httpx.Response(200, json={"adapters": [], "brokers": []})
        if request.method == "POST" and request.url.path == "/api/brokers":
            assert json.loads(request.content) == expected_create
            return httpx.Response(201, json=broker_response(account_id=None))
        if request.method == "POST" and request.url.path == f"/api/brokers/{BROKER_ID}/check":
            return httpx.Response(
                200,
                json={"broker_id": BROKER_ID, "available": True, "accounts_count": 2},
            )
        if request.method == "GET" and request.url.path == f"/api/brokers/{BROKER_ID}/accounts":
            return httpx.Response(
                200,
                json={
                    "accounts": [account_response("account-a"), account_response("account-b")],
                    "total_amounts": [],
                    "total_free_cash": [],
                    "errors": [
                        {
                            "broker_id": BROKER_ID,
                            "broker_name": "T-Invest Sandbox",
                            "account_id": None,
                            "code": "SYNTHETIC_REMOTE_WARNING",
                            "message": "remote-private-detail",
                        }
                    ],
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    exit_code, result, recorder, raw_output = invoke(
        ["--apply"],
        handler,
        environ={"TINVEST_SANDBOX_TOKEN": SYNTHETIC_TOKEN},
    )

    assert exit_code == 2
    assert result == {
        "ok": False,
        "code": "ACCOUNT_READ_FAILED",
        "counts": {"errors": 1},
    }
    assert all(request.method != "PUT" for request in recorder.requests)
    assert SYNTHETIC_TOKEN not in raw_output
    assert "remote-private-detail" not in raw_output


def test_apply_requires_explicit_account_when_multiple_open_accounts_exist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/health":
            return httpx.Response(200, json=health_response())
        if request.method == "GET" and request.url.path == "/api/brokers":
            return httpx.Response(
                200,
                json={"adapters": [], "brokers": [broker_response(account_id=None)]},
            )
        if request.method == "POST" and request.url.path == f"/api/brokers/{BROKER_ID}/check":
            return httpx.Response(
                200,
                json={"broker_id": BROKER_ID, "available": True, "accounts_count": 2},
            )
        if request.method == "GET" and request.url.path == f"/api/brokers/{BROKER_ID}/accounts":
            return httpx.Response(
                200,
                json={
                    "accounts": [account_response("account-a"), account_response("account-b")],
                    "total_amounts": [],
                    "total_free_cash": [],
                    "errors": [],
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    exit_code, result, recorder, _raw_output = invoke(
        ["--apply"],
        handler,
        environ={"TINVEST_SANDBOX_TOKEN": SYNTHETIC_TOKEN},
    )

    assert exit_code == 2
    assert result == {
        "ok": False,
        "code": "ACCOUNT_SELECTION_REQUIRED",
        "counts": {"open_accounts": 2},
    }
    assert all(request.method != "PUT" for request in recorder.requests)


def test_apply_rerun_reuses_existing_broker_and_automation_without_duplicates() -> None:
    automation_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal automation_reads
        if request.method == "GET" and request.url.path == "/api/health":
            return httpx.Response(200, json=health_response())
        if request.method == "GET" and request.url.path == "/api/brokers":
            return httpx.Response(
                200,
                json={"adapters": [], "brokers": [broker_response()]},
            )
        if request.method == "POST" and request.url.path == f"/api/brokers/{BROKER_ID}/check":
            return httpx.Response(
                200,
                json={"broker_id": BROKER_ID, "available": True, "accounts_count": 1},
            )
        if request.method == "GET" and request.url.path == f"/api/brokers/{BROKER_ID}/accounts":
            return httpx.Response(
                200,
                json={
                    "accounts": [account_response("sandbox-account-1")],
                    "total_amounts": [],
                    "total_free_cash": [],
                    "errors": [],
                },
            )
        if request.method == "POST" and request.url.path == f"/api/brokers/{BROKER_ID}/instruments/synchronize":
            return httpx.Response(
                200,
                json={
                    "broker_id": BROKER_ID,
                    "added": 2,
                    "updated": 1,
                    "deactivated": 0,
                    "synchronized_at": "2026-09-08T10:01:00Z",
                },
            )
        if request.method == "GET" and request.url.path == "/api/trading-automations":
            automation_reads += 1
            return httpx.Response(200, json={"items": [automation_response()]})
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    exit_code, result, recorder, _raw_output = invoke(
        [
            "--apply",
            "--display-name",
            "  T-Invest Sandbox  ",
            "--instrument-id",
            INSTRUMENT_ID,
        ],
        handler,
        environ={"TINVEST_SANDBOX_TOKEN": SYNTHETIC_TOKEN},
    )

    assert exit_code == 0
    assert result == {
        "ok": True,
        "code": "SANDBOX_READY",
        "mode": "apply",
        "counts": {
            "broker_created": 0,
            "broker_settings_updated": 0,
            "open_accounts": 1,
            "instruments_added": 2,
            "instruments_updated": 1,
            "instruments_deactivated": 0,
            "automations_requested": 1,
            "automations_created": 0,
            "automations_reused": 1,
            "automations_for_broker": 1,
        },
        "states": {"IN_QUEUE": 1},
        "trading_started": False,
    }
    assert automation_reads == 2
    assert all(request.method != "PUT" for request in recorder.requests)
    assert all(not (request.method == "POST" and request.url.path.endswith("/trade")) for request in recorder.requests)


def test_argument_error_does_not_echo_arbitrary_values(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Request must not be sent: {request.method} {request.url.path}")

    recorder = RecordingTransport(handler)
    output = io.StringIO()
    exit_code = seed_sandbox.main(
        [f"--unexpected={SYNTHETIC_TOKEN}"],
        environ={},
        transport=httpx.MockTransport(recorder),
        output=output,
    )

    assert exit_code == 2
    assert json.loads(output.getvalue()) == {"ok": False, "code": "INVALID_ARGUMENTS"}
    captured = capsys.readouterr()
    assert SYNTHETIC_TOKEN not in output.getvalue()
    assert SYNTHETIC_TOKEN not in captured.err
    assert recorder.requests == []


def test_token_sources_are_mutually_exclusive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Request must not be sent: {request.method} {request.url.path}")

    exit_code, result, recorder, _raw_output = invoke(
        [
            "--apply",
            "--token-env",
            "SYNTHETIC_TOKEN_ENV",
            "--token-file",
            str(Path.cwd() / "synthetic-token-file"),
        ],
        handler,
    )

    assert exit_code == 2
    assert result == {"ok": False, "code": "INVALID_ARGUMENTS"}
    assert recorder.requests == []


def test_apply_creates_broker_selects_explicit_account_and_queues_requested_automation() -> None:
    automation_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal automation_reads
        if request.method == "GET" and request.url.path == "/api/health":
            return httpx.Response(200, json=health_response())
        if request.method == "GET" and request.url.path == "/api/brokers":
            return httpx.Response(200, json={"adapters": [], "brokers": []})
        if request.method == "POST" and request.url.path == "/api/brokers":
            assert json.loads(request.content)["account_id"] is None
            return httpx.Response(201, json=broker_response(account_id=None))
        if request.method == "POST" and request.url.path == f"/api/brokers/{BROKER_ID}/check":
            return httpx.Response(
                200,
                json={"broker_id": BROKER_ID, "available": True, "accounts_count": 2},
            )
        if request.method == "GET" and request.url.path == f"/api/brokers/{BROKER_ID}/accounts":
            return httpx.Response(
                200,
                json={
                    "accounts": [account_response("account-a"), account_response("account-b")],
                    "total_amounts": [],
                    "total_free_cash": [],
                    "errors": [],
                },
            )
        if request.method == "PUT" and request.url.path == f"/api/brokers/{BROKER_ID}":
            payload = json.loads(request.content)
            assert payload == {
                "display_name": "T-Invest Sandbox",
                "provider_code": "TINVEST",
                "environment_code": "SANDBOX",
                "adapter_code": "TINVEST_SANDBOX",
                "enabled": True,
                "fields": [
                    {"name": "token", "value": SYNTHETIC_TOKEN},
                    {"name": "fqdn", "value": "sandbox-invest-public-api.tbank.ru:443"},
                ],
                "is_test": True,
                "account_id": "account-b",
            }
            return httpx.Response(200, json=broker_response(account_id="account-b"))
        if request.method == "POST" and request.url.path == f"/api/brokers/{BROKER_ID}/instruments/synchronize":
            return httpx.Response(
                200,
                json={
                    "broker_id": BROKER_ID,
                    "added": 0,
                    "updated": 0,
                    "deactivated": 0,
                    "synchronized_at": "2026-09-08T10:01:00Z",
                },
            )
        if request.method == "GET" and request.url.path == "/api/trading-automations":
            automation_reads += 1
            return httpx.Response(
                200,
                json={
                    "items": [] if automation_reads == 1 else [automation_response(account_id="account-b")],
                },
            )
        if request.method == "POST" and request.url.path == f"/api/instruments/{BROKER_ID}/{INSTRUMENT_ID}/trade":
            assert json.loads(request.content) == {"account_id": "account-b"}
            return httpx.Response(201, json=automation_response(account_id="account-b"))
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    exit_code, result, _recorder, raw_output = invoke(
        [
            "--apply",
            "--account-id",
            "account-b",
            "--instrument-id",
            INSTRUMENT_ID,
        ],
        handler,
        environ={"TINVEST_SANDBOX_TOKEN": SYNTHETIC_TOKEN},
    )

    assert exit_code == 0
    assert result["code"] == "SANDBOX_READY"
    assert result["counts"] == {
        "broker_created": 1,
        "broker_settings_updated": 0,
        "open_accounts": 2,
        "instruments_added": 0,
        "instruments_updated": 0,
        "instruments_deactivated": 0,
        "automations_requested": 1,
        "automations_created": 1,
        "automations_reused": 0,
        "automations_for_broker": 1,
    }
    assert result["states"] == {"IN_QUEUE": 1}
    assert result["trading_started"] is False
    assert SYNTHETIC_TOKEN not in raw_output


def test_apply_does_not_enable_an_existing_disabled_active_broker() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/health":
            return httpx.Response(200, json=health_response())
        if request.method == "GET" and request.url.path == "/api/brokers":
            return httpx.Response(
                200,
                json={"adapters": [], "brokers": [broker_response(enabled=False)]},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    exit_code, result, recorder, _raw_output = invoke(
        ["--apply"],
        handler,
        environ={"TINVEST_SANDBOX_TOKEN": SYNTHETIC_TOKEN},
    )

    assert exit_code == 2
    assert result == {"ok": False, "code": "BROKER_DISABLED"}
    assert [request.method for request in recorder.requests] == ["GET", "GET"]


def test_apply_refreshes_changed_settings_before_connection_check() -> None:
    request_paths: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_paths.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/health":
            return httpx.Response(200, json=health_response())
        if request.method == "GET" and request.url.path == "/api/brokers":
            return httpx.Response(
                200,
                json={
                    "adapters": [],
                    "brokers": [broker_response(field_value="stale-synthetic-token")],
                },
            )
        if request.method == "PUT" and request.url.path == f"/api/brokers/{BROKER_ID}":
            payload = json.loads(request.content)
            assert payload["account_id"] == "sandbox-account-1"
            assert payload["fields"][0] == {"name": "token", "value": SYNTHETIC_TOKEN}
            return httpx.Response(200, json=broker_response())
        if request.method == "POST" and request.url.path == f"/api/brokers/{BROKER_ID}/check":
            return httpx.Response(
                200,
                json={"broker_id": BROKER_ID, "available": True, "accounts_count": 1},
            )
        if request.method == "GET" and request.url.path == f"/api/brokers/{BROKER_ID}/accounts":
            return httpx.Response(
                200,
                json={
                    "accounts": [account_response("sandbox-account-1")],
                    "total_amounts": [],
                    "total_free_cash": [],
                    "errors": [],
                },
            )
        if request.method == "GET" and request.url.path == "/api/trading-automations":
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    exit_code, result, _recorder, raw_output = invoke(
        ["--apply", "--no-sync"],
        handler,
        environ={"TINVEST_SANDBOX_TOKEN": SYNTHETIC_TOKEN},
    )

    assert exit_code == 0
    assert result["counts"]["broker_settings_updated"] == 1
    assert request_paths.index(("PUT", f"/api/brokers/{BROKER_ID}")) < request_paths.index(
        ("POST", f"/api/brokers/{BROKER_ID}/check")
    )
    assert SYNTHETIC_TOKEN not in raw_output


def test_unexpected_failure_emits_only_fixed_safe_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"internal detail contains {SYNTHETIC_TOKEN}")

    exit_code, result, _recorder, raw_output = invoke([], handler)

    assert exit_code == 2
    assert result == {"ok": False, "code": "UNEXPECTED_ERROR"}
    assert SYNTHETIC_TOKEN not in raw_output
