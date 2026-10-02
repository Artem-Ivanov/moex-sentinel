#!/usr/bin/env python3
"""Primary PROD API read-only acceptance; never writes to broker or application."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

from t_tech.invest import AsyncClient
from t_tech.invest.schemas import AccessLevel, AccountStatus

TARGET = "invest-public-api.tbank.ru:443"
TOKEN_FILE = Path("/etc/moex-sentinel/prod-readonly-token")


def read_token(path: Path = TOKEN_FILE) -> tuple[str | None, str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None, "TOKEN_MISSING"
    except OSError:
        return None, "TOKEN_UNAVAILABLE"
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as file:
            metadata = os.fstat(file.fileno())
            if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o077 or not stat.S_ISREG(metadata.st_mode):
                return None, "TOKEN_PERMISSIONS_INVALID"
            token = file.read(4097).strip()
            if not token or len(token) > 4096 or any(character.isspace() for character in token):
                return None, "TOKEN_INVALID"
            return token, "READY"
    except (OSError, UnicodeError):
        return None, "TOKEN_UNAVAILABLE"


async def probe_reads(token: str, *, client_factory: Any = AsyncClient) -> dict[str, Any]:
    sdk_logger = logging.getLogger("t_tech.invest.logging")
    previous_disabled = sdk_logger.disabled
    sdk_logger.disabled = True  # SDK error details are not part of the safe report.
    try:
        async with client_factory(token, target=TARGET) as services:
            accounts = await asyncio.wait_for(services.users.get_accounts(), timeout=15)
            open_accounts = [
                account for account in accounts.accounts if account.status == AccountStatus.ACCOUNT_STATUS_OPEN
            ]
            if not open_accounts:
                return {"status": "NO_OPEN_ACCOUNTS"}
            if any(account.access_level != AccessLevel.ACCOUNT_ACCESS_LEVEL_READ_ONLY for account in open_accounts):
                return {"status": "TOKEN_NOT_READ_ONLY"}
            statuses = []
            for account in open_accounts:
                account_id = account.id
                for read in (
                    services.operations.get_portfolio,
                    services.operations.get_positions,
                    services.orders.get_orders,
                    services.stop_orders.get_stop_orders,
                ):
                    await asyncio.wait_for(read(account_id=account_id), timeout=15)
                statuses.append({"account": hashlib.sha256(account_id.encode()).hexdigest()[:12], "reads": "PASS"})
            return {"status": "PASS", "endpoint": TARGET, "accounts": statuses}
    except Exception:
        # SDK exceptions may contain token/account or request metadata.
        return {"status": "RPC_FAILED"}
    finally:
        sdk_logger.disabled = previous_disabled


def main() -> None:
    token, status = read_token()
    result = {"status": status} if token is None else asyncio.run(probe_reads(token))
    print(json.dumps(result))  # noqa: T201 - explicit safe allowlist only
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
