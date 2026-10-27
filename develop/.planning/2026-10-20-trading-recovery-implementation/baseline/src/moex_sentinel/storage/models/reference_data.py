"""User-broker-scoped reference data models."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from moex_sentinel.storage.models.base import MONEY_PRECISION, MONEY_SCALE, Base, TimestampMixin, UuidPrimaryKeyMixin
from moex_sentinel.storage.types import UTCDateTime


class BrokerInstrumentModel(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """Catalog instrument isolated by configured user-broker scope."""

    __tablename__ = "broker_instruments"
    __table_args__ = (
        UniqueConstraint(
            "user_broker_id",
            "external_instrument_id",
            name="uq_broker_instruments_scope_external_instrument",
        ),
        UniqueConstraint("user_broker_id", "id", name="uq_broker_instruments_scope_id"),
        CheckConstraint("lot_size > 0", name="ck_broker_instruments_positive_lot_size"),
        CheckConstraint(
            "min_price_increment > 0",
            name="ck_broker_instruments_positive_price_increment",
        ),
    )

    user_broker_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("user_brokers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_instrument_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_identifiers: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(64), nullable=False)
    class_code: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    lot_size: Mapped[int] = mapped_column(nullable=False)
    min_price_increment: Mapped[Decimal] = mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    api_trade_available: Mapped[bool] = mapped_column(nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    is_selected: Mapped[bool] = mapped_column(default=False, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class InstrumentSyncStateModel(TimestampMixin, Base):
    """Latest catalog synchronization state for one user broker."""

    __tablename__ = "instrument_sync_state"

    user_broker_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("user_brokers.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    safe_error: Mapped[str | None] = mapped_column(String(1000))
    reconciliation_required: Mapped[bool] = mapped_column(default=True, nullable=False)
