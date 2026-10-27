"""Pure decision pipeline for one automation iteration."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from sentinel_contracts.trading import DecisionKind
from trading_automaton.domain.dtos import DecisionContext, TradeDecision


class TradingStrategy(Protocol):
    """Strategy contract used by the decision pipeline."""

    code: str
    version: str

    def decide(self, context: DecisionContext) -> TradeDecision: ...


class DecisionRule(Protocol):
    """Pre-decision guard that can short-circuit pipeline."""

    def evaluate(self, context: DecisionContext) -> TradeDecision | None: ...


class DecisionRulePipeline:
    """Evaluates rule chain and returns the first non-empty decision."""

    def __init__(self, rules: Sequence[DecisionRule] | None = None) -> None:
        self._rules = tuple(rules or ())

    def apply(self, context: DecisionContext) -> TradeDecision | None:
        for rule in self._rules:
            decision = rule.evaluate(context)
            if decision is not None:
                return decision
        return None


class CoreAvailableRule:
    """Stops processing if market/core state is temporarily unavailable."""

    def evaluate(self, context: DecisionContext) -> TradeDecision | None:
        if context.core_available:
            return None
        return TradeDecision(
            kind=DecisionKind.WAIT,
            quantity_lots=0,
            limit_price=None,
            reason_code="CORE_UNAVAILABLE",
        )


class ActiveIntentRule:
    """Stops processing if there is an intent already executed or pending."""

    def evaluate(self, context: DecisionContext) -> TradeDecision | None:
        if not context.has_active_intent:
            return None
        return TradeDecision(
            kind=DecisionKind.WAIT,
            quantity_lots=0,
            limit_price=None,
            reason_code="ACTIVE_INTENT",
        )


class StopLossRule:
    """Stops active position when protective loss level is crossed."""

    def evaluate(self, context: DecisionContext) -> TradeDecision | None:
        if context.quantity_lots <= 0:
            return None
        stop_price = context.average_price * (Decimal("1") - context.settings.stop_loss_percent / Decimal("100"))
        if context.current_price > stop_price:
            return None
        return TradeDecision(
            kind=DecisionKind.SELL_ALL,
            quantity_lots=context.quantity_lots,
            limit_price=context.best_bid,
            reason_code="STOP_LOSS",
        )


class TakeProfitRule:
    """Secures a position when target take-profit level is hit."""

    def evaluate(self, context: DecisionContext) -> TradeDecision | None:
        if context.quantity_lots <= 0:
            return None
        cost = context.average_price * context.lot_size * context.quantity_lots
        proceeds = context.best_bid * context.lot_size * context.quantity_lots
        buy_commission = (
            sum(
                (lot.entry_commission * lot.remaining_lots / lot.original_lots for lot in context.lots),
                start=Decimal(),
            )
            if context.lots
            else context.buy_commission_per_lot * context.quantity_lots
        )
        net_profit = proceeds - cost - buy_commission - context.commission_schedule.estimate("SELL", proceeds)
        target = cost * context.settings.take_profit_percent / Decimal("100")
        if net_profit < target:
            return None
        return TradeDecision(
            kind=DecisionKind.SELL_ALL,
            quantity_lots=context.quantity_lots,
            limit_price=context.best_bid,
            reason_code="TAKE_PROFIT",
        )


def _default_strategy() -> TradingStrategy:
    from trading_automaton.services.strategies import AdaptiveScalpingStrategy  # noqa: PLC0415

    return AdaptiveScalpingStrategy()


class TradeDecisionService:
    """Pure orchestration of decision rules and terminal strategy selection."""

    def __init__(
        self,
        strategy: TradingStrategy | None = None,
        rules: Sequence[DecisionRule] | None = None,
        pipeline: DecisionRulePipeline | None = None,
    ) -> None:
        self._strategy = strategy or _default_strategy()
        self._pipeline = pipeline or DecisionRulePipeline(rules or self._build_default_rules())

    @property
    def strategy_code(self) -> str:
        return self._strategy.code

    @property
    def strategy_version(self) -> str:
        return self._strategy.version

    def decide(self, context: DecisionContext) -> TradeDecision:
        if not context.settings.enabled:
            return TradeDecision(
                kind=DecisionKind.WAIT,
                quantity_lots=0,
                limit_price=None,
                reason_code="STRATEGY_DISABLED",
            )
        if (decision := self._pipeline.apply(context)) is not None:
            return decision
        return self._strategy.decide(context)

    @staticmethod
    def _build_default_rules() -> tuple[DecisionRule, ...]:
        return (
            CoreAvailableRule(),
            ActiveIntentRule(),
            StopLossRule(),
            TakeProfitRule(),
        )
