"""Trading session state aggregates for automation monitoring."""

from __future__ import annotations

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class TradingSessionsStatus(PositionalModel):
    """Aggregate status of trading sessions for active automations."""

    model_config = ConfigDict(frozen=True)

    status: str
    total: int
    open: int
    closed: int
    unavailable: int
