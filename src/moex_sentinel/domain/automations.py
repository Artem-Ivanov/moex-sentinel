"""Domain DTOs used by trading automation use-cases."""

from __future__ import annotations

from pydantic import ConfigDict, SkipValidation

from moex_sentinel.domain.market_data import HistoricCandle
from moex_sentinel.domain.portfolio import BrokerOperation
from moex_sentinel.domain.repository_records import AutomationRecord
from sentinel_contracts.base import PositionalModel


class PositionDetailsError(PositionalModel):
    source: str
    code: str
    message: str

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    def __init__(self, source: str, code: str, message: str) -> None:
        super().__init__(source=source, code=code, message=message)


class TradingAutomationDetails(PositionalModel):
    automation: SkipValidation[AutomationRecord]
    operations: tuple[SkipValidation[BrokerOperation], ...]
    candles: tuple[SkipValidation[HistoricCandle], ...]
    errors: tuple[PositionDetailsError, ...]

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    def __init__(
        self,
        automation: AutomationRecord,
        operations: tuple[BrokerOperation, ...],
        candles: tuple[HistoricCandle, ...],
        errors: tuple[PositionDetailsError, ...],
    ) -> None:
        super().__init__(
            automation=automation,
            operations=operations,
            candles=candles,
            errors=errors,
        )


class TradingAutomationStatuses(PositionalModel):
    automations: tuple[SkipValidation[AutomationRecord], ...]
    missing_automation_ids: tuple[str, ...]

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    def __init__(
        self,
        automations: tuple[AutomationRecord, ...],
        missing_automation_ids: tuple[str, ...],
    ) -> None:
        super().__init__(automations=automations, missing_automation_ids=missing_automation_ids)
