"""Assemble the real broker iteration and lifecycle for runtime scenarios."""

from trading_automaton.runtime.analytics_broker import AnalyticsBrokerRuntime
from trading_automaton.services.analytics_frame import AnalyticsFrameService
from trading_automaton.services.order_book_validation import OrderBookValidationService
from trading_automaton.usecases.broker_iteration import RunBrokerIterationUsecase


def build_analytics_runtime(
    source,
    tick,
    *,
    preparation,
    metrics,
    source_id,
    fallback,
    now,
    tick_seconds=1.0,
    retry_limit=5,
    persistence_failure=None,
):
    """Build isolated runtime state while keeping each scenario's dependencies explicit."""
    iteration = RunBrokerIterationUsecase(
        AnalyticsFrameService(source, now=now, order_books=OrderBookValidationService()),
        tick,
        preparation=preparation,
        metrics=metrics,
        source_id=source_id,
        fallback=fallback,
        persistence_failure=persistence_failure,
    )
    return AnalyticsBrokerRuntime(source, iteration, tick_seconds=tick_seconds, retry_limit=retry_limit)
