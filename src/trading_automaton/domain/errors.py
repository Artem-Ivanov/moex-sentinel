"""Typed failures crossing trading-worker service boundaries."""


class DurableDecisionPersistenceError(RuntimeError):
    """A decision batch was not committed, so broker dispatch is forbidden."""


class AnalyticsUnavailableError(Exception):
    """Analytics could not provide a market snapshot; recovery may continue."""


class CoreOperationError(Exception):
    """Safe Core failure with retry policy and optional protocol rejection status."""

    def __init__(self, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__("Core operation failed")
        self.retryable = retryable
        self.status_code = status_code
