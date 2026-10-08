"""Persistence capability failures independent of a storage implementation."""


class StorageError(Exception):
    """Base error for repository operations."""


class DuplicateRecordError(StorageError):
    """A repository uniqueness rule was violated."""


class RecordNotFoundError(StorageError):
    """A requested persistence record does not exist."""


class ConstraintViolationError(StorageError):
    """A persistence constraint other than repository uniqueness was violated."""


class RevisionConflictError(ValueError):
    """The caller's expected revision no longer matches durable state."""
