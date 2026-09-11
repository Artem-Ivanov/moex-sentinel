"""Compatibility exports for the Core-owned SDK-neutral stream adapter."""

from moex_sentinel.adapters.tinvest.streaming import (
    MarketDataStreamManager,
    MarketStreamEvent,
    TInvestStreamingAdapter,
    _event,
)

__all__ = ["MarketDataStreamManager", "MarketStreamEvent", "TInvestStreamingAdapter", "_event"]
