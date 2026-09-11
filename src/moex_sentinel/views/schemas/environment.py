"""HTTP schemas for active environment selection."""

from typing import Literal

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class EnvironmentSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")

    active_environment: Literal["TEST", "PROD"]
    can_create_broker: bool
    can_prepare_test_environment: bool


class SwitchEnvironmentSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")

    active_environment: Literal["TEST", "PROD"]
