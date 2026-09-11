"""Broker-scoped instrument catalog records."""

from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict

from moex_sentinel.domain.market_data import LastPrice
from sentinel_contracts.base import PositionalModel


class CatalogInstrumentNotFoundError(LookupError):
    """Instrument is absent from the selected broker catalog."""


class UserBrokerCatalogConstraintError(ValueError):
    """The scoped instrument catalog rejected an invalid snapshot."""


class UserBrokerCatalogInstrumentDraft(PositionalModel):
    """Instrument metadata returned by one configured broker API/account."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    external_instrument_id: str
    external_identifiers: dict[str, object]
    ticker: str
    name: str
    instrument_type: str
    class_code: str
    currency: str
    lot_size: int
    min_price_increment: Decimal
    api_trade_available: bool


class UserBrokerCatalogInstrument(UserBrokerCatalogInstrumentDraft):
    """Persisted instrument isolated by user-broker scope."""

    id: str
    user_broker_id: str
    is_active: bool
    is_selected: bool
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime


class UserBrokerCatalogSyncState(PositionalModel):
    """Latest catalog synchronization status for one user broker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_broker_id: str
    status: str
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    safe_error: str | None
    reconciliation_required: bool


class UserBrokerCatalogReconciliationResult(PositionalModel):
    """Counts produced by one atomic catalog reconciliation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_broker_id: str
    added: int
    updated: int
    deactivated: int
    synchronized_at: datetime


class CatalogInstrumentDraft(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    figi: str
    ticker: str
    name: str
    class_code: str
    instrument_type: str
    category: str
    currency: str | None
    lot: int
    min_price_increment: Decimal
    api_trade_available: bool


class CatalogInstrument(PositionalModel):
    model_config = ConfigDict(frozen=True)
    id: str
    broker_id: str
    instrument_id: str
    figi: str
    ticker: str
    name: str
    class_code: str
    instrument_type: str
    category: str
    currency: str | None
    lot: int
    min_price_increment: Decimal
    api_trade_available: bool
    is_active: bool
    is_selected: bool
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime


class CatalogSyncState(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_id: str
    status: str
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    safe_error: str | None


class CatalogReconciliationResult(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_id: str
    added: int
    updated: int
    deactivated: int
    synchronized_at: datetime


class CatalogCategory(PositionalModel):
    model_config = ConfigDict(frozen=True)
    code: str
    label: str
    count: int


class CatalogInstrumentPrice(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument: CatalogInstrument
    unit_price: Decimal | None
    lot_price: Decimal | None
    price_captured_at: datetime | None


class CatalogListView(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_id: str
    items: tuple[CatalogInstrumentPrice, ...]
    categories: tuple[CatalogCategory, ...]
    sync_state: CatalogSyncState
    currencies: tuple[str, ...] = ()


class InstrumentDetailsView(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_name: str
    instrument: CatalogInstrument
    last_price: LastPrice | None
    lot_price: Decimal | None
    sync_state: CatalogSyncState
