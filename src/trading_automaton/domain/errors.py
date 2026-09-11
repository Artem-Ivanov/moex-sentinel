"""Typed failures crossing trading-worker service boundaries."""


class DurableDecisionPersistenceError(RuntimeError):
    """A decision batch was not committed, so broker dispatch is forbidden."""
