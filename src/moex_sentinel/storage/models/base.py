"""Declarative base and shared columns."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from moex_sentinel.storage.types import UTCDateTime
from sentinel_contracts.time import utc_now_ms

MONEY_PRECISION = 28
MONEY_SCALE = 9


class Base(DeclarativeBase):
    """Base for all persistence models."""


class UuidPrimaryKeyMixin:
    """UUID string primary key for SQLite portability."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))


class TimestampMixin:
    """UTC creation and update timestamps."""

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now_ms, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now_ms,
        onupdate=utc_now_ms,
        nullable=False,
    )
