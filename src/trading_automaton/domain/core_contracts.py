"""Contract DTOs for Core-automaton HTTP integration."""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class PublishAcknowledgement(PositionalModel):
    model_config = ConfigDict(frozen=True)

    automation_id: str
    accepted_through_sequence: int
    current_revision: int


class PublishFailure(PositionalModel):
    model_config = ConfigDict(frozen=True)

    automation_id: str
    code: str


class PublishResult(PositionalModel):
    model_config = ConfigDict(frozen=True)

    results: tuple[PublishAcknowledgement, ...]
    failures: tuple[PublishFailure, ...] = ()


class AutomationStatusesResult(PositionalModel):
    model_config = ConfigDict(frozen=True)

    automations: dict[str, dict[str, Any]]
    missing_automation_ids: tuple[str, ...]
