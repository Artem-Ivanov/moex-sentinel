"""Synthetic baseline trading fact values."""

from datetime import UTC, datetime
from decimal import Decimal

from moex_sentinel.storage.models import BrokerInstrumentModel, UserBrokerModel

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


def user_broker_model(record_id: str, account_id: str) -> UserBrokerModel:
    return UserBrokerModel(
        id=record_id,
        api_slug="t_invest",
        display_name=record_id,
        environment="TEST",
        fqdn="sandbox-invest-public-api.tbank.ru:443",
        settings={"token": "synthetic-token"},
        external_account_id=account_id,
        state="ACTIVE",
        created_at=NOW,
        updated_at=NOW,
    )


def instrument_model(record_id: str, user_broker_id: str) -> BrokerInstrumentModel:
    return BrokerInstrumentModel(
        id=record_id,
        user_broker_id=user_broker_id,
        external_instrument_id=f"external-{record_id}",
        external_identifiers={},
        ticker=record_id.upper(),
        name=f"Synthetic {record_id}",
        instrument_type="SHARE",
        class_code="TQBR",
        currency="RUB",
        lot_size=10,
        min_price_increment=Decimal("0.01"),
        api_trade_available=True,
        is_active=True,
        is_selected=True,
        first_seen_at=NOW,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
