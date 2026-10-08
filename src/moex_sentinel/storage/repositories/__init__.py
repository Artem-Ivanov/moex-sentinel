"""Application-facing persistence repositories and errors."""

from moex_sentinel.domain.persistence_errors import (
    ConstraintViolationError,
    DuplicateRecordError,
    RecordNotFoundError,
    StorageError,
)
from moex_sentinel.storage.repositories.reference_catalog import ReferenceCatalogRepository
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository

__all__ = [
    "ConstraintViolationError",
    "DuplicateRecordError",
    "RecordNotFoundError",
    "ReferenceCatalogRepository",
    "StorageError",
    "UserBrokerRepository",
]
