"""Safe connection-check results."""

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class BrokerConnectionStatus(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_id: str
    available: bool
    accounts_count: int
