"""Application environment selection."""

from typing import Literal

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel

Environment = Literal["TEST", "PROD"]


class EnvironmentState(PositionalModel):
    model_config = ConfigDict(frozen=True)
    active_environment: Environment
    can_create_broker: bool
    can_prepare_test_environment: bool
