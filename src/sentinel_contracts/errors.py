"""Stable cross-process contract failures."""


class ContractConflictError(Exception):
    """Base class for an optimistic contract conflict."""
