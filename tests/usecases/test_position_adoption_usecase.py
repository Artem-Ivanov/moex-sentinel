"""Position adoption use-case boundary."""

import asyncio
from datetime import UTC, datetime

import pytest

from moex_sentinel.domain.position_adoption import PositionAdoptionResult
from moex_sentinel.domain.user_brokers import UserBroker, UserBrokerState
from moex_sentinel.services.position_adoption import ConfiguredPositionAdoptionService
from moex_sentinel.usecases.position_adoption import AdoptBrokerPositionsUsecase


class Service:
    async def adopt(self, user_broker_id: str, account_id: str, broker: object) -> PositionAdoptionResult:
        assert user_broker_id == "broker-1"
        assert account_id == "account-1"
        assert broker == "adapter"
        return PositionAdoptionResult()


def configured_broker(state=UserBrokerState.ACTIVE, account_id="account-1") -> UserBroker:
    return UserBroker(
        id="broker-1",
        api_slug="TINVEST_SANDBOX",
        display_name="Sandbox",
        environment="TEST",
        fqdn="sandbox.example",
        settings={},
        external_account_id=account_id,
        state=state,
        created_at=datetime(2026, 9, 15, tzinfo=UTC),
        updated_at=datetime(2026, 9, 15, tzinfo=UTC),
    )


def test_usecase_resolves_active_broker_and_delegates() -> None:
    broker = configured_broker()
    usecase = AdoptBrokerPositionsUsecase(
        ConfiguredPositionAdoptionService(Service(), lambda broker_id: broker, lambda value: "adapter")
    )

    result = asyncio.run(usecase.execute("broker-1"))

    assert result == PositionAdoptionResult()


@pytest.mark.parametrize(
    ("state", "account_id"),
    [
        pytest.param(UserBrokerState.DRAFT, None, id="draft-without-account"),
        pytest.param(UserBrokerState.DISABLED, "account-1", id="disabled-with-account"),
        pytest.param(UserBrokerState.ERROR, "account-1", id="failed-with-account"),
    ],
)
def test_inactive_broker_is_rejected_before_adapter_creation(state, account_id):
    broker = configured_broker(state, account_id)
    calls = []
    usecase = AdoptBrokerPositionsUsecase(
        ConfiguredPositionAdoptionService(Service(), lambda broker_id: broker, lambda value: calls.append(value))
    )

    with pytest.raises(ValueError, match="Position adoption requires an active broker account"):
        asyncio.run(usecase.execute("broker-1"))

    assert calls == []
