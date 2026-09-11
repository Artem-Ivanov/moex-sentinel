"""Domain records used as transport objects between repositories and services."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel
from sentinel_contracts.strategy import STRATEGY_CODE, STRATEGY_VERSION
from sentinel_contracts.trading import AutomationState


class TradingSession(PositionalModel):
    model_config = ConfigDict(frozen=True)

    id: str
    broker_id: str
    state: str
    mode: str
    started_at: datetime
    stopped_at: datetime | None
    emergency_stop: bool
    safe_error: str | None
    created_at: datetime
    updated_at: datetime


class SessionEvent(PositionalModel):
    model_config = ConfigDict(frozen=True)

    id: str
    broker_id: str
    session_id: str
    event_type: str
    safe_message: str
    metadata: dict[str, Any]
    occurred_at: datetime
    worker_id: str | None
    decision_id: str | None
    order_id: str | None


class AutomationRecord(PositionalModel):
    model_config = ConfigDict(frozen=True)

    id: str
    broker_id: str
    account_id: str
    instrument_id: str
    state: AutomationState
    suspended_from_state: AutomationState | None
    revision: int
    last_sequence_number: int
    resume_requested: bool
    currency: str
    strategy_code: str = STRATEGY_CODE
    strategy_version: str = STRATEGY_VERSION
    quantity_lots: int = 0
    average_price: Decimal = Decimal()
    invested_amount: Decimal = Decimal()
    realized_pnl: Decimal = Decimal()
    unrealized_pnl: Decimal = Decimal()
    net_pnl: Decimal = Decimal()
    actual_commissions: Decimal = Decimal()
    broker_name: str = ""
    ticker: str = ""
    instrument_name: str = ""


class HeartbeatRecord(PositionalModel):
    model_config = ConfigDict(frozen=True)

    worker_id: str
    occurred_at: datetime


class AppSettings(PositionalModel):
    model_config = ConfigDict(frozen=True)

    portfolio_refresh_seconds: int
    market_data_refresh_seconds: int
    scanner_refresh_seconds: int
    scanner_config: dict[str, Any]
    active_environment: str = "TEST"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WatchlistItem(PositionalModel):
    model_config = ConfigDict(frozen=True)

    id: str
    broker_id: str
    instrument_id: str
    source: str
    enabled: bool
    created_at: datetime
    updated_at: datetime
