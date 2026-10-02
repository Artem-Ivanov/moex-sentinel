"""Read-only check of the configured T-Invest Sandbox account on the VPS."""

import json
import subprocess
import time
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

BASE_URL = "https://135.136.178.252"
SSH_COMMAND = [
    "ssh", "-i", "/Users/artemivanov/.ssh/id_rsa", "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
    "codex-deploy@135.136.178.252",
    "sudo -n cat /root/moex-sentinel-operator-password",
]
opener = build_opener(HTTPCookieProcessor(CookieJar()))
report = {"requests": [], "results": {}}
logged_in = False
csrf_token = None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def tail(value):
    return str(value)[-4:] if value else None


def code_from(body):
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = data.get("code") if isinstance(data, dict) else None
    if not isinstance(value, str):
        detail = data.get("detail") if isinstance(data, dict) else None
        value = detail.get("code") if isinstance(detail, dict) else None
    return value if isinstance(value, str) and len(value) <= 80 else None


def call(path, method="GET", body=None, csrf=False):
    headers = {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if csrf and csrf_token:
        headers["X-CSRF-Token"] = csrf_token
    started = time.monotonic()
    requested_at = utc_now()
    try:
        response = opener.open(Request(BASE_URL + path, data=data, headers=headers, method=method), timeout=30)
    except HTTPError as error:
        response = error
    except (URLError, TimeoutError, OSError) as error:
        report["requests"].append({"path": path, "method": method, "at": requested_at,
                                   "duration_ms": round((time.monotonic() - started) * 1000),
                                   "transport_error": type(error).__name__})
        raise RuntimeError("HTTPS request failed") from None
    with response:
        raw = response.read()
        status = response.status
    report["requests"].append({"path": path, "method": method, "at": requested_at,
                               "duration_ms": round((time.monotonic() - started) * 1000),
                               "status": status})
    try:
        payload = json.loads(raw) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    return status, response.headers, payload, code_from(raw)


def accounts(broker):
    broker_id = broker["id"]
    status, _, data, error_code = call(f"/api/brokers/{broker_id}/accounts")
    report["results"].setdefault("accounts", {})[broker_id] = {
        "status": status,
        "accounts": [{"account_id_tail4": tail(item.get("account_id")),
                      "status": item.get("status"), "total_amount": item.get("total_amount"),
                      "free_cash": item.get("free_cash")}
                     for item in (data or {}).get("accounts", [])],
        "errors": [{"code": item.get("code")} for item in (data or {}).get("errors", [])],
        "http_error_code": error_code,
    }


def collect() -> None:
    global logged_in, csrf_token
    try:
        ssh = subprocess.run(SSH_COMMAND, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Could not retrieve operator password over SSH") from None
    if ssh.returncode or not ssh.stdout.strip():
        raise RuntimeError("Could not retrieve operator password over SSH")
    password = ssh.stdout.strip()
    ssh.stdout = ""
    ssh.stderr = ""
    status, _, data, _ = call("/api/auth/login", "POST",
                              {"username": "operator", "password": password})
    del password
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("csrf_token"), str):
        raise RuntimeError("Operator login failed")
    logged_in = True
    csrf_token = data["csrf_token"]

    status, _, data, error_code = call("/api/brokers")
    broker_rows = data.get("brokers", []) if status == 200 and isinstance(data, dict) else []
    report["results"]["brokers"] = {
        "status": status,
        "adapters_count": len(data.get("adapters", [])) if isinstance(data, dict) else 0,
        "items": [{"id": broker.get("id"), "display_name": broker.get("display_name"),
                   "enabled": broker.get("enabled"), "is_test": broker.get("is_test"),
                   "account_id_tail4": tail(broker.get("account_id")),
                   "token_present": any(field.get("value") for field in broker.get("fields", [])
                                        if isinstance(field, dict) and
                                        any(word in field.get("name", "").lower()
                                            for word in ("token", "secret", "key")))}
                  for broker in broker_rows if isinstance(broker, dict)],
        "http_error_code": error_code,
    }
    for broker in broker_rows:
        if not isinstance(broker, dict) or not broker.get("is_test"):
            continue
        accounts(broker)
        if broker.get("enabled"):
            status, _, catalog, error_code = call(f"/api/brokers/{broker['id']}/instruments")
            state = catalog.get("sync_state", {}) if isinstance(catalog, dict) else {}
            report["results"].setdefault("instruments", {})[broker["id"]] = {
                "status": status, "items_count": len(catalog.get("items", [])) if isinstance(catalog, dict) else 0,
                "sync_state": {"status": state.get("status"), "last_attempt_at": state.get("last_attempt_at"),
                               "last_success_at": state.get("last_success_at"),
                               "error_present": bool(state.get("safe_error"))},
                "http_error_code": error_code,
            }
        else:
            report["results"].setdefault("instruments", {})[broker["id"]] = {
                "skipped": "broker_disabled_draft",
            }

    for key, path, items_key in (
        ("positions", "/api/positions", "items"),
        ("automations", "/api/trading-automations", "items"),
        ("operations", "/api/operations?limit=20", "items"),
    ):
        status, _, data, error_code = call(path)
        data = data if isinstance(data, dict) else {}
        items = data.get(items_key, [])
        result = {"status": status, "count": len(items),
                  "errors": [{"code": item.get("code")} for item in data.get("errors", [])
                             if isinstance(item, dict)], "http_error_code": error_code}
        if key == "positions":
            result["items"] = [{"ticker": item.get("ticker"), "quantity": item.get("quantity"),
                                "average_price": item.get("average_price"),
                                "current_price": item.get("current_price")}
                               for item in items if isinstance(item, dict)]
        elif key == "automations":
            result["items"] = [{"state": item.get("state"), "ticker": item.get("ticker"),
                                "quantity_lots": item.get("quantity_lots"),
                                "average_price": item.get("average_price"),
                                "unrealized_pnl": item.get("unrealized_pnl")}
                               for item in items if isinstance(item, dict)]
        else:
            result["types"] = sorted({str(item.get("operation_type")) for item in items
                                       if isinstance(item, dict) and item.get("operation_type")})
        report["results"][key] = result

    status, _, data, error_code = call("/api/trading/summary")
    data = data if isinstance(data, dict) else {}
    currencies = data.get("currencies", [])
    report["results"]["trading_summary"] = {
        "status": status, "captured_at": data.get("captured_at"),
        "currencies_count": len(currencies),
        "currencies": [{"currency": item.get("currency"), "portfolio_value": item.get("portfolio_value"),
                        "free_cash": item.get("free_cash"), "pnl_24h": item.get("pnl_24h"),
                        "pnl_7d": item.get("pnl_7d"), "pnl_30d": item.get("pnl_30d")}
                       for item in currencies if isinstance(item, dict)],
        "errors": [{"code": item.get("code")} for item in data.get("errors", [])
                   if isinstance(item, dict)], "http_error_code": error_code,
    }


def main() -> int:
    exit_code = 0
    try:
        collect()
    except RuntimeError as error:
        report["fatal_error"] = str(error)
        exit_code = 1
    finally:
        if logged_in:
            try:
                call("/api/auth/logout", "POST", csrf=True)
            except RuntimeError:
                report["logout_error"] = "HTTPS request failed"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
