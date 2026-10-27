"""Safe errors emitted by the T-Invest adapter boundary."""

from sentinel_contracts.broker_errors import BrokerOperationError


class TInvestAdapterError(BrokerOperationError):
    """A stable public code and safe message, without SDK details or metadata."""
