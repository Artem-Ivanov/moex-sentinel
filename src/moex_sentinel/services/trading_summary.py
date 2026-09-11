"""Read-only aggregation of persisted portfolio snapshots."""

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from moex_sentinel.domain.trading_summary import (
    CurrencyTradingSummary,
    PortfolioSnapshotRunValue,
    PortfolioSnapshotValue,
    TradingPnlPeriod,
    TradingSummaryView,
    period_from_snapshots,
)


class PortfolioSnapshotReader(Protocol):
    """Snapshot queries required to build a trading summary."""

    def latest_run(self) -> PortfolioSnapshotRunValue | None: ...

    def latest_snapshots(self, run_id: str) -> tuple[PortfolioSnapshotValue, ...]: ...

    def common_baselines(
        self,
        latest: tuple[PortfolioSnapshotValue, ...],
        at_or_before: datetime,
    ) -> tuple[PortfolioSnapshotValue, ...]: ...


class TradingSummaryService:
    """Build an aggregate view without contacting broker APIs."""

    _WINDOWS = (timedelta(days=1), timedelta(days=7), timedelta(days=30))

    def __init__(self, repository: PortfolioSnapshotReader) -> None:
        self._repository = repository

    def view(self) -> TradingSummaryView:
        latest_run = self._repository.latest_run()
        if latest_run is None:
            return TradingSummaryView(None, (), ())

        grouped: dict[str, list[PortfolioSnapshotValue]] = defaultdict(list)
        for snapshot in self._repository.latest_snapshots(latest_run.id):
            grouped[snapshot.currency].append(snapshot)

        currencies = tuple(
            self._currency_summary(currency, tuple(grouped[currency]), latest_run) for currency in sorted(grouped)
        )
        return TradingSummaryView(latest_run.captured_at, currencies, latest_run.errors)

    def _currency_summary(
        self,
        currency: str,
        latest: tuple[PortfolioSnapshotValue, ...],
        latest_run: PortfolioSnapshotRunValue,
    ) -> CurrencyTradingSummary:
        periods = tuple(self._period(latest, latest_run, window) for window in self._WINDOWS)
        return CurrencyTradingSummary(
            currency=currency,
            portfolio_value=sum((snapshot.total_value for snapshot in latest), Decimal()),
            free_cash=sum((snapshot.free_cash for snapshot in latest), Decimal()),
            pnl_24h=periods[0],
            pnl_7d=periods[1],
            pnl_30d=periods[2],
        )

    def _period(
        self,
        latest: tuple[PortfolioSnapshotValue, ...],
        latest_run: PortfolioSnapshotRunValue,
        window: timedelta,
    ) -> TradingPnlPeriod:
        requested_from = latest_run.captured_at - window
        baselines = self._repository.common_baselines(latest, requested_from)
        if len(baselines) != len(latest):
            return TradingPnlPeriod(None, None, latest_run.captured_at, False)
        actual_from = baselines[0].captured_at

        aggregate_latest = latest[0].model_copy(
            update={"cumulative_pnl": sum((snapshot.cumulative_pnl for snapshot in latest), Decimal())}
        )
        aggregate_baseline = baselines[0].model_copy(
            update={
                "cumulative_pnl": sum((snapshot.cumulative_pnl for snapshot in baselines), Decimal()),
                "captured_at": actual_from,
            }
        )
        return period_from_snapshots(aggregate_latest, aggregate_baseline, requested_from=requested_from)
