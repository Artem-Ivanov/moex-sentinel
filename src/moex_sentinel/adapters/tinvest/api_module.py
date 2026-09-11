"""Code-owned T-Invest API declaration."""

from pydantic import BaseModel, ConfigDict, Field

from moex_sentinel.adapters.broker_api_registry import BrokerApiModule
from moex_sentinel.domain.user_brokers import (
    BrokerApiDescriptor,
    BrokerApiEnvironmentError,
    BrokerApiFieldDescriptor,
)

TINVEST_API_SLUG = "t_invest"
TINVEST_SANDBOX_FQDN = "sandbox-invest-public-api.tbank.ru:443"


class TInvestSettings(BaseModel):
    """Persisted T-Invest connection settings."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    token: str = Field(min_length=1)


class TInvestApiModule(BrokerApiModule):
    """T-Invest settings and endpoint contract available to Core."""

    descriptor = BrokerApiDescriptor(
        api_slug=TINVEST_API_SLUG,
        display_name="T-Invest",
        environments=("TEST",),
        fields=(BrokerApiFieldDescriptor(name="token", required=True, value_type="string"),),
    )

    def default_fqdn(self, environment: str) -> str:
        if environment != "TEST":
            raise BrokerApiEnvironmentError(environment)
        return TINVEST_SANDBOX_FQDN

    def validate_settings(self, value: object) -> dict[str, object]:
        return TInvestSettings.model_validate(value).model_dump(mode="json")
