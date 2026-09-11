"""User intent for viewing the persisted trading summary."""

from typing import Protocol

from moex_sentinel.domain.trading_summary import TradingSummaryView


class TradingSummaryServicePort(Protocol):
    def view(self) -> TradingSummaryView: ...


class ViewTradingSummaryUsecase:
    def __init__(self, service: TradingSummaryServicePort) -> None:
        self._service = service

    def execute(self) -> TradingSummaryView:
        return self._service.view()
