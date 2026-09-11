"""User intents for baseline trading automation lifecycle."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from moex_sentinel.domain.automations import (
    PositionDetailsError,
    TradingAutomationDetails,
    TradingAutomationStatuses,
)
from moex_sentinel.domain.market_data import CandleInterval, HistoricCandle
from moex_sentinel.domain.portfolio import BrokerOperation, BrokerOperationsView
from moex_sentinel.domain.repository_records import AutomationRecord
from moex_sentinel.services.automations import AutomationStateConflictError
from moex_sentinel.storage.repositories import DuplicateRecordError, RecordNotFoundError
from moex_sentinel.storage.repositories.automations import RevisionConflictError
from moex_sentinel.usecases.errors import UseCaseError


class AutomationServicePort(Protocol):
    def create(self, *, broker_id: str, account_id: str, instrument_id: str) -> AutomationRecord: ...
    def get(self, automation_id: str) -> AutomationRecord: ...
    def get_many(self, automation_ids: list[str]) -> list[AutomationRecord]: ...
    def list_active(self) -> list[AutomationRecord]: ...
    def hold(self, automation_id: str, reason: str) -> AutomationRecord: ...
    def resume(self, automation_id: str) -> AutomationRecord: ...
    def close(self, automation_id: str) -> AutomationRecord: ...


class PositionOperationsServicePort(Protocol):
    async def view_position_operations(
        self,
        broker_id: str,
        account_id: str,
        instrument_id: str,
        limit: int,
    ) -> BrokerOperationsView: ...


class PositionMarketDataServicePort(Protocol):
    async def candles(
        self,
        broker_id: str,
        instrument_id: str,
        start: datetime,
        end: datetime,
        interval: CandleInterval,
    ) -> tuple[HistoricCandle, ...]: ...


class CreateTradingAutomationUsecase:
    def __init__(self, service: AutomationServicePort) -> None:
        self._service = service

    async def execute(self, broker_id: str, account_id: str, instrument_id: str) -> AutomationRecord:
        try:
            return self._service.create(
                broker_id=broker_id,
                account_id=account_id,
                instrument_id=instrument_id,
            )
        except Exception as error:
            raise _automation_error(error) from error


class ViewTradingAutomationUsecase:
    def __init__(self, service: AutomationServicePort) -> None:
        self._service = service

    def execute(self, automation_id: str) -> AutomationRecord:
        try:
            return self._service.get(automation_id)
        except Exception as error:
            raise _automation_error(error) from error


class ViewTradingAutomationStatusesUsecase:
    def __init__(self, service: AutomationServicePort) -> None:
        self._service = service

    def execute(self, automation_ids: list[str]) -> TradingAutomationStatuses:
        records = self._service.get_many(automation_ids)
        found = {record.id for record in records}
        return TradingAutomationStatuses(
            tuple(records),
            tuple(automation_id for automation_id in automation_ids if automation_id not in found),
        )


class ViewTradingAutomationsUsecase:
    def __init__(self, service: AutomationServicePort) -> None:
        self._service = service

    def execute(self) -> list[AutomationRecord]:
        return self._service.list_active()


class ViewTradingAutomationDetailsUsecase:
    def __init__(
        self,
        automations: AutomationServicePort,
        operations: PositionOperationsServicePort,
        market_data: PositionMarketDataServicePort,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._automations = automations
        self._operations = operations
        self._market_data = market_data
        self._now = now

    async def execute(self, automation_id: str) -> TradingAutomationDetails:
        try:
            automation = self._automations.get(automation_id)
        except Exception as error:
            raise _automation_error(error) from error
        end = self._now()
        start = end - timedelta(hours=2)
        operations: tuple[BrokerOperation, ...] = ()
        candles: tuple[HistoricCandle, ...] = ()
        errors: list[PositionDetailsError] = []
        try:
            operation_view = await self._operations.view_position_operations(
                automation.broker_id,
                automation.account_id,
                automation.instrument_id,
                50,
            )
            operations = operation_view.items
            errors.extend(PositionDetailsError("operations", item.code, item.message) for item in operation_view.errors)
        except Exception:
            errors.append(
                PositionDetailsError(
                    "operations",
                    "BROKER_OPERATIONS_UNAVAILABLE",
                    "Не удалось получить брокерские операции.",  # noqa: RUF001
                )
            )
        try:
            candles = await self._market_data.candles(
                automation.broker_id,
                automation.instrument_id,
                start,
                end,
                CandleInterval.MIN_1,
            )
        except Exception:
            errors.append(
                PositionDetailsError(
                    "candles",
                    "MARKET_CANDLES_UNAVAILABLE",
                    "Не удалось получить минутные свечи.",  # noqa: RUF001
                )
            )
        return TradingAutomationDetails(automation, operations, candles, tuple(errors))


class HoldAutomationUsecase:
    def __init__(self, service: AutomationServicePort) -> None:
        self._service = service

    def execute(self, automation_id: str) -> AutomationRecord:
        try:
            return self._service.hold(automation_id, "User requested hold")
        except Exception as error:
            raise _automation_error(error) from error


class ResumeAutomationUsecase:
    def __init__(self, service: AutomationServicePort) -> None:
        self._service = service

    def execute(self, automation_id: str) -> AutomationRecord:
        try:
            return self._service.resume(automation_id)
        except Exception as error:
            raise _automation_error(error) from error


class CloseAutomationUsecase:
    def __init__(self, service: AutomationServicePort) -> None:
        self._service = service

    def execute(self, automation_id: str) -> AutomationRecord:
        try:
            return self._service.close(automation_id)
        except Exception as error:
            raise _automation_error(error) from error


def _automation_error(error: Exception) -> UseCaseError:
    if isinstance(error, RecordNotFoundError):
        return UseCaseError("AUTOMATION_NOT_FOUND", "Торговый автомат не найден.")
    if isinstance(error, DuplicateRecordError):
        return UseCaseError("AUTOMATION_ALREADY_ACTIVE", "Для инструмента уже есть автомат.")
    if isinstance(error, RevisionConflictError):
        return UseCaseError("AUTOMATION_REVISION_CONFLICT", "Автомат уже изменился.")
    if isinstance(error, AutomationStateConflictError):
        return UseCaseError("AUTOMATION_STATE_CONFLICT", "Действие недоступно в текущем состоянии.")
    raise error
