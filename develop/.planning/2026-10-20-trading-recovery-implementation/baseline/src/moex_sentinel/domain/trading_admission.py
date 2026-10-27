"""Canonical fingerprint for the broker connection identity used by Core and Worker."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sentinel_contracts.broker_execution import OrderSide


@dataclass(frozen=True)
class AdmissionOrderState:
    request_id: str
    broker_order_id: str | None
    instrument_uid: str | None
    side: OrderSide | None
    requested_lots: int | None
    executed_lots: int | None
    executed_amount: Decimal | None
    executed_commission: Decimal | None
    currency: str | None
    status: str
    attributable: bool
    unknown_reason: str | None


@dataclass(frozen=True)
class AdmissionInstrument:
    instrument_uid: str
    instrument_type: str
    currency: str
    lot_size: int
    price_increment: Decimal
    api_trade_available: bool
    mark_price: Decimal | None
    mark_captured_at: datetime | None


class AdmissionConflictError(ValueError):
    """An admission write lost its revision or no longer matches the active owner."""


def broker_configuration_fingerprint(
    *,
    account_id: str,
    access_mode: str,
    adapter_code: str,
    broker_id: str,
    environment: str,
    target: str,
    token: str,
) -> str:
    identity = {
        "account_id": account_id,
        "access_mode": access_mode,
        "adapter_code": adapter_code,
        "broker_id": broker_id,
        "environment": environment,
        "target": target,
        "token": token,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()
