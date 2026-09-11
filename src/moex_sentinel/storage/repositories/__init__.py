"""Application-facing persistence repositories and errors."""

from moex_sentinel.storage.repositories.reference_catalog import ReferenceCatalogRepository
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository


class StorageError(Exception):
    """Base error for repository operations."""


class DuplicateRecordError(StorageError):
    """A repository uniqueness rule was violated."""


class RecordNotFoundError(StorageError):
    """A requested persistence record does not exist."""


class ConstraintViolationError(StorageError):
    """A persistence constraint other than repository uniqueness was violated."""


__all__ = [
    "ConstraintViolationError",
    "DuplicateRecordError",
    "RecordNotFoundError",
    "ReferenceCatalogRepository",
    "StorageError",
    "UserBrokerRepository",
]
