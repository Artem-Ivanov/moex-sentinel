"""HTTP wrappers that reuse the shared typed fact contracts."""

from uuid import UUID

from pydantic import ConfigDict, Field

from sentinel_contracts.base import PositionalModel
from sentinel_contracts.trading_facts import AutomationCommand, FactEnvelope


class StrictFactSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ClaimAutomationCommandsRequestSchema(StrictFactSchema):
    worker_id: str | None = None
    limit: int = Field(default=10, gt=0, le=1000)


class ClaimAutomationCommandsResponseSchema(StrictFactSchema):
    commands: list[AutomationCommand]


class AutomationStatusesRequestSchema(StrictFactSchema):
    automation_ids: list[UUID]


class PublishFactsRequestSchema(StrictFactSchema):
    facts: list[FactEnvelope]
