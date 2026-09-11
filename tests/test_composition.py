"""Application composition smoke tests."""

from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.composition import build_application_usecases
from moex_sentinel.domain.brokers import BrokerDraft, BrokerField
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base


def test_composition_wires_broker_usecases_to_shared_business_service() -> None:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = create_session_factory(engine)
    usecases = build_application_usecases(factory)
    draft = BrokerDraft(
        display_name="Primary sandbox",
        provider_code="TINVEST",
        environment_code="SANDBOX",
        adapter_code="TINVEST_SANDBOX",
        enabled=True,
        fields=(
            BrokerField(name="token", value="synthetic-token"),
            BrokerField(name="fqdn", value="sandbox-invest-public-api.tbank.ru:443"),
        ),
    )

    created = usecases.save_broker_settings.execute(None, draft)

    assert usecases.view_broker_settings.execute().brokers == (created,)
    automation_service = usecases.view_trading_automation._service
    assert usecases.create_trading_automation._service is automation_service
    assert usecases.hold_automation._service is automation_service
    assert usecases.resume_automation._service is automation_service
    assert usecases.close_automation._service is automation_service
    assert not hasattr(usecases, "update_automation_strategy")
    assert not hasattr(usecases, "view_strategy_templates")
    assert usecases.synchronize_broker_instruments._position_adoption is not None
    engine.dispose()
