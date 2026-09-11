"""Replaceable atomic trading strategies."""

from decimal import ROUND_DOWN, Decimal

from sentinel_contracts.strategy import STRATEGY_CODE, STRATEGY_VERSION
from sentinel_contracts.trading import DecisionKind
from trading_automaton.domain.dtos import DecisionContext, TradeDecision


class AdaptiveScalpingStrategy:
    code = STRATEGY_CODE
    version = STRATEGY_VERSION

    def decide(self, context: DecisionContext) -> TradeDecision:
        if not context.settings.enabled:
            return TradeDecision(
                kind=DecisionKind.WAIT,
                quantity_lots=0,
                limit_price=None,
                reason_code="STRATEGY_DISABLED",
            )
        if context.quantity_lots <= 0:
            return self._entry(context)
        sell = self._sell(context)
        if sell is not None:
            return sell
        buy = self._buy(context)
        if buy is not None:
            return buy
        return TradeDecision(kind=DecisionKind.WAIT, quantity_lots=0, limit_price=None, reason_code="NO_THRESHOLD")

    def _entry(self, context: DecisionContext) -> TradeDecision:
        if (guard := self._buy_market_guard(context)) is not None:
            return guard
        if context.cycle is None or context.indicators is None:
            return TradeDecision(
                kind=DecisionKind.WAIT, quantity_lots=0, limit_price=None, reason_code="ENTRY_REVERSAL_WAIT"
            )
        if not context.cycle.sell_armed:
            return TradeDecision(
                kind=DecisionKind.WAIT,
                quantity_lots=0,
                limit_price=None,
                reason_code="ENTRY_PULLBACK_WAIT",
            )
        if context.cycle.pending_low is None:
            return TradeDecision(
                kind=DecisionKind.WAIT, quantity_lots=0, limit_price=None, reason_code="ENTRY_REVERSAL_WAIT"
            )
        if (
            context.indicators.last_candle_at is not None
            and context.cycle.last_buy_candle_at == context.indicators.last_candle_at
        ):
            return TradeDecision(
                kind=DecisionKind.WAIT, quantity_lots=0, limit_price=None, reason_code="BUY_CANDLE_COOLDOWN"
            )
        reversal = context.current_price - context.cycle.pending_low
        required_ticks = context.min_price_increment * 2
        required_percent = context.cycle.pending_low * context.averaging_step_percent / 200
        if reversal < max(required_ticks, required_percent):
            return TradeDecision(DecisionKind.WAIT, 0, None, "ENTRY_REVERSAL_WAIT")
        return TradeDecision(
            kind=DecisionKind.BUY_MORE,
            quantity_lots=context.settings.buy_order_lots,
            limit_price=context.best_ask,
            reason_code="ENTRY_REVERSAL_CONFIRMED",
        )

    def _sell(self, context: DecisionContext) -> TradeDecision | None:
        if context.cycle is not None and not context.cycle.sell_armed:
            return TradeDecision(
                kind=DecisionKind.WAIT, quantity_lots=0, limit_price=None, reason_code="SELL_CYCLE_DISARMED"
            )
        if context.lots:
            lot = context.lots[0]
            lots = min(
                lot.remaining_lots,
                max(
                    1,
                    int(
                        (
                            Decimal(context.quantity_lots) * context.settings.partial_sell_percent / 100
                        ).to_integral_value(rounding=ROUND_DOWN)
                    ),
                ),
            )
            buy_commission = lot.entry_commission * Decimal(lots) / lot.original_lots
            gross = (context.best_bid - lot.entry_price) * context.lot_size * lots
            target = lot.entry_price * context.lot_size * lots * (context.minimum_net_profit_percent / 100)
            sell_commission = context.commission_schedule.estimate(
                "SELL",
                context.best_bid * context.lot_size * lots,
            )
            if gross > target + buy_commission + sell_commission:
                return TradeDecision(
                    kind=DecisionKind.SELL_PART,
                    quantity_lots=lots,
                    limit_price=context.best_bid,
                    reason_code="LIFO_LOT_TAKE_PROFIT",
                    target_lot_id=lot.id,
                )
            if gross > target:
                return TradeDecision(
                    kind=DecisionKind.WAIT,
                    quantity_lots=0,
                    limit_price=None,
                    reason_code="COMMISSION_EXCEEDS_PARTIAL_PROFIT",
                )
            return None

        next_level = context.completed_partial_sell_steps + 1
        if next_level > context.settings.max_partial_sell_steps:
            return None
        if context.current_price <= context.average_price:
            return None
        partial_price = context.average_price * (1 + context.minimum_net_profit_percent * next_level / 100)
        if context.current_price < partial_price and context.completed_partial_sell_steps > 0:
            return TradeDecision(
                kind=DecisionKind.NO_ACTION,
                quantity_lots=0,
                limit_price=None,
                reason_code="NO_THRESHOLD",
            )
        lots = min(
            context.quantity_lots,
            max(
                1,
                int(
                    (Decimal(context.quantity_lots) * context.settings.partial_sell_percent / 100).to_integral_value(
                        rounding=ROUND_DOWN
                    )
                ),
            ),
        )
        gross = (context.best_bid - context.average_price) * context.lot_size * lots
        target = context.average_price * context.lot_size * lots * (context.minimum_net_profit_percent / 100)
        sell_commission = context.commission_schedule.estimate(
            "SELL",
            context.best_bid * context.lot_size * lots,
        )
        if gross <= target + context.buy_commission_per_lot * lots + sell_commission:
            return TradeDecision(
                kind=DecisionKind.WAIT,
                quantity_lots=0,
                limit_price=None,
                reason_code="COMMISSION_EXCEEDS_PARTIAL_PROFIT",
            )
        return TradeDecision(
            kind=DecisionKind.SELL_PART,
            quantity_lots=lots,
            limit_price=context.best_bid,
            reason_code="PARTIAL_TAKE_PROFIT",
        )

    def _buy(self, context: DecisionContext) -> TradeDecision | None:
        trigger = context.last_buy_price * (1 - context.averaging_step_percent / 100)
        if context.best_ask > trigger:
            return None
        if (guard := self._buy_market_guard(context)) is not None:
            return guard
        cycle = context.cycle
        indicators = context.indicators
        if cycle is None or indicators is None or cycle.pending_low is None:
            return TradeDecision(DecisionKind.WAIT, 0, None, "PENDING_LOW_CREATED")
        if indicators.last_candle_at is not None and cycle.last_buy_candle_at == indicators.last_candle_at:
            return TradeDecision(
                kind=DecisionKind.WAIT,
                quantity_lots=0,
                limit_price=None,
                reason_code="BUY_CANDLE_COOLDOWN",
            )
        reversal = context.current_price - cycle.pending_low
        required_ticks = context.min_price_increment * 2
        required_percent = cycle.pending_low * context.averaging_step_percent / 200
        if reversal < max(required_ticks, required_percent):
            return TradeDecision(
                kind=DecisionKind.WAIT,
                quantity_lots=0,
                limit_price=None,
                reason_code="REVERSAL_NOT_CONFIRMED",
            )
        return TradeDecision(
            kind=DecisionKind.BUY_MORE,
            quantity_lots=context.settings.buy_order_lots,
            limit_price=context.best_ask,
            reason_code="AVERAGING_LEVEL",
        )

    @staticmethod
    def _buy_market_guard(context: DecisionContext) -> TradeDecision | None:
        indicators = context.indicators
        if indicators is None or not indicators.buy_window_confirmed:
            return TradeDecision(DecisionKind.WAIT, 0, None, "BUY_WINDOW_UNCONFIRMED")
        if indicators.downtrend:
            return TradeDecision(DecisionKind.WAIT, 0, None, "DOWNTREND_FILTER")
        low, high, mean = indicators.range_low, indicators.range_high, indicators.mean_20
        if (
            low is not None
            and high is not None
            and mean is not None
            and high > low
            and context.best_ask > mean
            and (context.best_ask - low) / (high - low) >= Decimal("0.75")
        ):
            return TradeDecision(DecisionKind.WAIT, 0, None, "BUY_UPPER_RANGE_FILTER")
        return None
