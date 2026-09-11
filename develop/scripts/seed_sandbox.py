"""Safely seed a T-Invest sandbox broker through the public Core HTTP API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit
from uuid import UUID

import httpx

PROVIDER_CODE = "TINVEST"
ENVIRONMENT_CODE = "SANDBOX"
ADAPTER_CODE = "TINVEST_SANDBOX"
SANDBOX_FQDN = "sandbox-invest-public-api.tbank.ru:443"
OPEN_ACCOUNT_STATUS = "ACCOUNT_STATUS_OPEN"


class SeedFailure(RuntimeError):
    def __init__(self, code: str, *, counts: dict[str, int] | None = None) -> None:
        self.code = code
        self.counts = counts
        super().__init__(code)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise SeedFailure("INVALID_ARGUMENTS")


def _parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(prog="seed_sandbox.py", description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080")
    token_source = parser.add_mutually_exclusive_group()
    token_source.add_argument("--token-env", default="TINVEST_SANDBOX_TOKEN")
    token_source.add_argument("--token-file")
    parser.add_argument("--display-name", default="T-Invest Sandbox")
    parser.add_argument("--account-id")
    parser.add_argument("--instrument-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-sync", action="store_true")
    return parser


def _emit(output: TextIO, value: dict[str, object]) -> None:
    json.dump(value, output, ensure_ascii=False, sort_keys=True)
    output.write("\n")


def _valid_base_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in {"", "/"}
    )


def _request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    try:
        response = client.request(method, path, json=payload)
    except httpx.TransportError as error:
        raise SeedFailure("HTTP_TRANSPORT_ERROR") from error
    if not 200 <= response.status_code < 300:
        raise SeedFailure("HTTP_STATUS_ERROR", counts={"status": response.status_code})
    try:
        value = response.json()
    except ValueError as error:
        raise SeedFailure("INVALID_RESPONSE") from error
    if not isinstance(value, dict):
        raise SeedFailure("INVALID_RESPONSE")
    return value


def _require_ready_health(health: dict[str, object]) -> None:
    if health.get("status") != "ok" or health.get("database") != "ok" or health.get("schema") != "compatible":
        raise SeedFailure("CORE_NOT_READY")


def _broker_payload(display_name: str, token: str, account_id: str | None) -> dict[str, object]:
    return {
        "display_name": display_name,
        "provider_code": PROVIDER_CODE,
        "environment_code": ENVIRONMENT_CODE,
        "adapter_code": ADAPTER_CODE,
        "enabled": True,
        "fields": [
            {"name": "token", "value": token},
            {"name": "fqdn", "value": SANDBOX_FQDN},
        ],
        "is_test": True,
        "account_id": account_id,
    }


def _list(value: dict[str, object], key: str) -> list[dict[str, object]]:
    items = value.get(key)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise SeedFailure("INVALID_RESPONSE")
    return items


def _validate_existing_broker(broker: dict[str, object], *, allow_draft: bool = False) -> None:
    if (
        broker.get("provider_code") != PROVIDER_CODE
        or broker.get("environment_code") != ENVIRONMENT_CODE
        or broker.get("adapter_code") != ADAPTER_CODE
        or broker.get("is_test") is not True
    ):
        raise SeedFailure("BROKER_CONFIGURATION_CONFLICT")
    if broker.get("enabled") is not True and not (allow_draft and broker.get("account_id") is None):
        raise SeedFailure("BROKER_DISABLED")


def _broker_fields_match(broker: dict[str, object], token: str) -> bool:
    fields = broker.get("fields")
    if not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields):
        raise SeedFailure("INVALID_RESPONSE")
    values = {str(field.get("name")): field.get("value") for field in fields if isinstance(field.get("name"), str)}
    return values == {"token": token, "fqdn": SANDBOX_FQDN}


def _uuid_text(value: object, *, code: str = "INVALID_RESPONSE") -> str:
    if not isinstance(value, str):
        raise SeedFailure(code)
    try:
        return str(UUID(value))
    except ValueError as error:
        raise SeedFailure(code) from error


def _prepare_broker(
    client: httpx.Client,
    *,
    brokers: list[dict[str, object]],
    display_name: str,
    token: str,
    requested_account_id: str | None,
) -> tuple[dict[str, object], str, bool, int, int]:
    matching = [broker for broker in brokers if broker.get("display_name") == display_name]
    if len(matching) > 1:
        raise SeedFailure("BROKER_NAME_AMBIGUOUS", counts={"matching_brokers": len(matching)})
    created = not matching
    settings_updated = 0
    if matching:
        broker = matching[0]
        _validate_existing_broker(broker, allow_draft=broker.get("account_id") is None)
        current_account = broker.get("account_id")
        if current_account is not None and not isinstance(current_account, str):
            raise SeedFailure("INVALID_RESPONSE")
        if requested_account_id is not None and current_account is not None and requested_account_id != current_account:
            raise SeedFailure("ACCOUNT_CONFLICT")
        if not _broker_fields_match(broker, token):
            broker_id = _uuid_text(broker.get("id"))
            broker = _request_json(
                client,
                "PUT",
                f"/api/brokers/{broker_id}",
                payload=_broker_payload(display_name, token, current_account),
            )
            settings_updated = 1
            _validate_existing_broker(broker, allow_draft=current_account is None)
    else:
        broker = _request_json(
            client,
            "POST",
            "/api/brokers",
            payload=_broker_payload(display_name, token, None),
        )
        current_account = None
        _validate_existing_broker(broker, allow_draft=True)

    broker_id = _uuid_text(broker.get("id"))
    check = _request_json(client, "POST", f"/api/brokers/{broker_id}/check")
    if check.get("available") is not True:
        raise SeedFailure("BROKER_CHECK_FAILED")
    accounts_response = _request_json(client, "GET", f"/api/brokers/{broker_id}/accounts")
    errors = _list(accounts_response, "errors")
    if errors:
        raise SeedFailure("ACCOUNT_READ_FAILED", counts={"errors": len(errors)})
    open_accounts = [
        account
        for account in _list(accounts_response, "accounts")
        if account.get("status") == OPEN_ACCOUNT_STATUS and isinstance(account.get("account_id"), str)
    ]
    available_ids = {str(account["account_id"]) for account in open_accounts}
    if current_account is not None:
        if current_account not in available_ids:
            raise SeedFailure("ACCOUNT_NOT_AVAILABLE", counts={"open_accounts": len(open_accounts)})
        selected_account = current_account
    elif requested_account_id is not None:
        if requested_account_id not in available_ids:
            raise SeedFailure("ACCOUNT_NOT_AVAILABLE", counts={"open_accounts": len(open_accounts)})
        selected_account = requested_account_id
    elif len(open_accounts) == 1:
        selected_account = str(open_accounts[0]["account_id"])
    else:
        raise SeedFailure("ACCOUNT_SELECTION_REQUIRED", counts={"open_accounts": len(open_accounts)})

    if current_account is None:
        broker = _request_json(
            client,
            "PUT",
            f"/api/brokers/{broker_id}",
            payload=_broker_payload(display_name, token, selected_account),
        )
        _validate_existing_broker(broker)
        if broker.get("account_id") != selected_account:
            raise SeedFailure("INVALID_RESPONSE")
    return broker, selected_account, created, settings_updated, len(open_accounts)


def _count(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise SeedFailure("INVALID_RESPONSE")
    return item


def _ensure_automations(
    client: httpx.Client,
    *,
    broker_id: str,
    account_id: str,
    instrument_ids: tuple[str, ...],
) -> tuple[int, int]:
    before = _list(_request_json(client, "GET", "/api/trading-automations"), "items")
    created = 0
    reused = 0
    for instrument_id in instrument_ids:
        matching = [
            automation
            for automation in before
            if automation.get("broker_id") == broker_id
            and automation.get("account_id") == account_id
            and automation.get("instrument_id") == instrument_id
        ]
        active = [automation for automation in matching if automation.get("state") != "CLOSED"]
        if len(active) > 1:
            raise SeedFailure("AUTOMATION_AMBIGUOUS", counts={"matching_automations": len(active)})
        if active:
            reused += 1
            continue
        if matching:
            raise SeedFailure("AUTOMATION_CLOSED")
        created_automation = _request_json(
            client,
            "POST",
            f"/api/instruments/{broker_id}/{instrument_id}/trade",
            payload={"account_id": account_id},
        )
        if (
            created_automation.get("broker_id") != broker_id
            or created_automation.get("account_id") != account_id
            or created_automation.get("instrument_id") != instrument_id
        ):
            raise SeedFailure("INVALID_RESPONSE")
        created += 1
        before.append(created_automation)
    return created, reused


def _apply(
    client: httpx.Client,
    *,
    arguments: argparse.Namespace,
    token: str,
    broker_items: list[dict[str, object]],
) -> dict[str, object]:
    broker, account_id, broker_created, settings_updated, open_accounts = _prepare_broker(
        client,
        brokers=broker_items,
        display_name=arguments.display_name,
        token=token,
        requested_account_id=arguments.account_id,
    )
    broker_id = _uuid_text(broker.get("id"))
    sync_counts = {"added": 0, "updated": 0, "deactivated": 0}
    if not arguments.no_sync:
        sync = _request_json(client, "POST", f"/api/brokers/{broker_id}/instruments/synchronize")
        sync_counts = {name: _count(sync, name) for name in sync_counts}
    created, reused = _ensure_automations(
        client,
        broker_id=broker_id,
        account_id=account_id,
        instrument_ids=arguments.instrument_ids,
    )
    final_automations = _list(_request_json(client, "GET", "/api/trading-automations"), "items")
    states = Counter(
        str(automation.get("state"))
        for automation in final_automations
        if automation.get("broker_id") == broker_id and isinstance(automation.get("state"), str)
    )
    return {
        "ok": True,
        "code": "SANDBOX_READY",
        "mode": "apply",
        "counts": {
            "broker_created": int(broker_created),
            "broker_settings_updated": settings_updated,
            "open_accounts": open_accounts,
            "instruments_added": sync_counts["added"],
            "instruments_updated": sync_counts["updated"],
            "instruments_deactivated": sync_counts["deactivated"],
            "automations_requested": len(arguments.instrument_ids),
            "automations_created": created,
            "automations_reused": reused,
            "automations_for_broker": sum(states.values()),
        },
        "states": dict(sorted(states.items())),
        "trading_started": states.get("IN_WORK", 0) > 0,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    output: TextIO | None = None,
) -> int:
    stream = output or sys.stdout
    try:
        arguments = _parser().parse_args(argv)
    except SeedFailure as error:
        _emit(stream, {"ok": False, "code": error.code})
        return 2
    if not _valid_base_url(arguments.base_url):
        _emit(stream, {"ok": False, "code": "INVALID_BASE_URL"})
        return 2
    arguments.display_name = arguments.display_name.strip()
    if not arguments.display_name:
        _emit(stream, {"ok": False, "code": "INVALID_DISPLAY_NAME"})
        return 2
    try:
        arguments.instrument_ids = tuple(
            dict.fromkeys(_uuid_text(value, code="INVALID_INSTRUMENT_ID") for value in arguments.instrument_id)
        )
    except SeedFailure as error:
        _emit(stream, {"ok": False, "code": error.code})
        return 2
    environment = os.environ if environ is None else environ
    token: str | None = None
    if arguments.apply:
        if arguments.token_file:
            try:
                token = Path(arguments.token_file).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                _emit(stream, {"ok": False, "code": "TOKEN_FILE_READ_FAILED"})
                return 2
        else:
            token = environment.get(arguments.token_env, "").strip()
        if not token:
            _emit(stream, {"ok": False, "code": "TOKEN_REQUIRED"})
            return 2
    try:
        with httpx.Client(
            base_url=arguments.base_url,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            timeout=httpx.Timeout(120.0, connect=10.0),
        ) as client:
            health = _request_json(client, "GET", "/api/health")
            _require_ready_health(health)
            broker_settings = _request_json(client, "GET", "/api/brokers")
            broker_items = _list(broker_settings, "brokers")
            if not arguments.apply:
                automation_response = _request_json(client, "GET", "/api/trading-automations")
                automation_items = _list(automation_response, "items")
                _emit(
                    stream,
                    {
                        "ok": True,
                        "mode": "dry-run",
                        "counts": {
                            "brokers": len(broker_items),
                            "matching_brokers": sum(
                                item.get("display_name") == arguments.display_name for item in broker_items
                            ),
                            "automations": len(automation_items),
                        },
                    },
                )
                return 0
            if token is None:
                _emit(stream, {"ok": False, "code": "TOKEN_REQUIRED"})
                return 2
            result = _apply(
                client,
                arguments=arguments,
                token=token,
                broker_items=broker_items,
            )
            _emit(stream, result)
            return 0
    except SeedFailure as error:
        result: dict[str, object] = {"ok": False, "code": error.code}
        if error.counts is not None:
            result["counts"] = error.counts
        _emit(stream, result)
        return 2
    except Exception:
        _emit(stream, {"ok": False, "code": "UNEXPECTED_ERROR"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
