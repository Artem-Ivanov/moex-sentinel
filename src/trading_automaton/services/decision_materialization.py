"""Materialize prepared strategy decisions into batch, dispatch and audit artifacts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel
from sentinel_contracts.broker_execution import OrderSide
from sentinel_contracts.strategy import STRATEGY_CODE, STRATEGY_VERSION
from sentinel_contracts.streaming_market import InstrumentMarketState
from sentinel_contracts.trading import DecisionKind, PositionSnapshot
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import DispatchRequest, PositionWorkItem, PreparedDecision, TradeDecision
from trading_automaton.domain.position_valuation import lot_position_snapshot, net_position_pnl
from trading_automaton.domain.storage_dtos import DecisionBatchItem, IntentBatchItem


class CashReservationPort(Protocol):
    async def available(self, account_id: str, currency: str) -> Decimal: ...

    async def reserved(self, account_id: str, currency: str) -> Decimal: ...


class DecisionMaterializerPort(Protocol):
    async def materialize(
        self,
        prepared: PreparedDecision,
        work: PositionWorkItem,
        market: InstrumentMarketState,
        cycle_before: Decimal | None,
        *,
        cash: CashReservationPort | None,
        pending_cash: dict[tuple[str, str], Decimal],
        snapshot_at: datetime,
        intent_id: str | None = None,
    ) -> "DecisionMaterialization | None": ...


class DecisionMaterialization(PositionalModel):
    """Atomic artifact for one prepared automation decision."""

    model_config = ConfigDict(frozen=True)

    item: DecisionBatchItem
    request: DispatchRequest | None
    audit_values: dict[str, object]


class DecisionMaterializerService:
    """Build durable payloads and dispatch envelopes from one prepared decision."""

    def __init__(
        self,
        *,
        settings: StrategySettings | None = None,
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._settings = settings or StrategySettings()
        self._id_factory = id_factory

    async def materialize(
        self,
        prepared: PreparedDecision,
        work: PositionWorkItem,
        market: InstrumentMarketState,
        cycle_before: Decimal | None,
        *,
        cash: CashReservationPort | None,
        pending_cash: dict[tuple[str, str], Decimal],
        snapshot_at: datetime,
        intent_id: str | None = None,
    ) -> DecisionMaterialization | None:
        state = prepared.evaluation.state
        if state is None or market.order_book is None:
            return None

        command = prepared.command
        position = state.position
        decision = prepared.evaluation.decision
        estimated_commission = prepared.evaluation.estimated_commission
        process_id = state.process_id or ""
        try:
            UUID(process_id)
        except ValueError:
            process_id = self._id_factory()
        request: DispatchRequest | None = None
        resolved_intent_id = intent_id
        audit_cash: dict[str, str] = {}

        if decision.kind in {DecisionKind.BUY_MORE, DecisionKind.SELL_PART, DecisionKind.SELL_ALL}:
            if decision.limit_price is None:
                return None
            limit_price = decision.limit_price

            side = OrderSide.BUY if decision.kind is DecisionKind.BUY_MORE else OrderSide.SELL
            required_order_cash = limit_price * command.lot_size * decision.quantity_lots + estimated_commission
            if side is OrderSide.BUY and cash is not None:
                available_before, reserved_before = await self._cash_window(
                    cash,
                    command.account_id,
                    command.currency,
                )
                audit_cash.update(
                    free_cash=str(available_before + reserved_before),
                    reserved_cash=str(reserved_before),
                )

                cash_key = (command.account_id, command.currency.upper())
                projected_available = available_before - pending_cash.get(cash_key, Decimal())
                audit_cash["required_order_cash"] = str(required_order_cash)
                if projected_available < required_order_cash:
                    decision = TradeDecision(DecisionKind.WAIT, 0, None, "INSUFFICIENT_FREE_CASH")
                    estimated_commission = Decimal()
                    audit_cash["estimated_buy_commission"] = str(estimated_commission)
                else:
                    resolved_intent_id = resolved_intent_id or self._id_factory()
                    pending_cash[cash_key] = pending_cash.get(cash_key, Decimal()) + required_order_cash
                    projected_available -= required_order_cash
                    request = self._build_request(
                        resolved_intent_id,
                        command,
                        work.instrument_type,
                        side,
                        decision.quantity_lots,
                        limit_price,
                        process_id,
                        str(command.broker_id),
                        command.currency,
                        required_order_cash,
                    )
                audit_cash["estimated_buy_commission"] = str(estimated_commission)
                audit_cash["available_after_reserve"] = str(projected_available)
            else:
                # For SELL action and BUY actions when cash service is unavailable, keep request mandatory.
                required_order_cash = required_order_cash if side is OrderSide.BUY else Decimal()
                resolved_intent_id = resolved_intent_id or self._id_factory()
                audit_cash["required_order_cash"] = str(required_order_cash)
                request = self._build_request(
                    resolved_intent_id,
                    command,
                    work.instrument_type,
                    side,
                    decision.quantity_lots,
                    limit_price,
                    process_id,
                    str(command.broker_id),
                    command.currency,
                    required_order_cash,
                )
                audit_cash["estimated_buy_commission"] = str(
                    estimated_commission if side is OrderSide.BUY else Decimal()
                )
        elif decision.kind is DecisionKind.NO_ACTION:
            audit_cash["estimated_buy_commission"] = str(Decimal())
            audit_cash["required_order_cash"] = str(Decimal())

        invested_amount = position.average_price * command.lot_size * position.quantity_lots
        unrealized_pnl = (
            (market.order_book.best_bid.price - position.average_price) * command.lot_size * position.quantity_lots
        )
        position_snapshot = PositionSnapshot(
            quantity_lots=int(position.quantity_lots),
            average_price=position.average_price,
            invested_amount=invested_amount,
            realized_pnl=state.realized_pnl,
            unrealized_pnl=unrealized_pnl,
            net_pnl=net_position_pnl(state.realized_pnl, unrealized_pnl, state.lots),
            actual_commissions=state.history.actual_commissions,
        )
        if state.lots:
            position_snapshot = lot_position_snapshot(
                state.lots,
                lot_size=command.lot_size,
                mark_price=market.order_book.best_bid.price,
                realized_pnl=state.realized_pnl,
                actual_commissions=state.history.actual_commissions,
            )
        intent: IntentBatchItem | None = None
        if request is not None:
            if resolved_intent_id is None or decision.limit_price is None:
                raise RuntimeError("Dispatch request is missing its durable intent data.")
            intent = IntentBatchItem(
                resolved_intent_id,
                decision.kind.value,
                "BUY" if decision.kind is DecisionKind.BUY_MORE else "SELL",
                decision.quantity_lots,
                decision.limit_price,
            )

        item = DecisionBatchItem(
            automation_id=str(command.automation_id),
            broker_id=str(command.broker_id),
            account_id=command.account_id,
            currency=command.currency,
            instrument_id=command.external_instrument_id,
            instrument_type=work.instrument_type,
            quantity_lots=int(position.quantity_lots),
            lot_size=command.lot_size,
            average_price=position.average_price,
            current_price=market.order_book.best_bid.price,
            best_bid=market.order_book.best_bid.price,
            best_ask=market.order_book.best_ask.price,
            invested_amount=invested_amount,
            estimated_commission=estimated_commission,
            decision=decision.kind.value,
            reason_code=decision.reason_code,
            decision_quantity_lots=decision.quantity_lots,
            limit_price=decision.limit_price,
            strategy_snapshot=self._strategy_snapshot(),
            process_id=process_id,
            intent=intent,
            position_snapshot=position_snapshot.to_metadata(),
            position_snapshot_at=market.order_book.captured_at,
            iteration_id=prepared.snapshot_id,
            cycle_state=(
                None
                if state.has_active_intent
                else {
                    "pending_low": state.cycle.pending_low,
                    "last_buy_candle_at": state.cycle.last_buy_candle_at,
                    "sell_armed": state.cycle.sell_armed,
                    "last_sell_price": state.cycle.last_sell_price,
                    "updated_at": state.cycle.updated_at,
                }
            ),
            indicators={
                "last_candle_at": (
                    None if state.indicators.last_candle_at is None else state.indicators.last_candle_at.isoformat()
                ),
                "averaging_step_percent": str(state.indicators.averaging_step_percent),
                "minimum_net_profit_percent": str(state.indicators.minimum_net_profit_percent),
                "source": state.indicators.source,
                "mean_5": None if state.indicators.mean_5 is None else str(state.indicators.mean_5),
                "mean_20": None if state.indicators.mean_20 is None else str(state.indicators.mean_20),
                "change_10_percent": (
                    None if state.indicators.change_10_percent is None else str(state.indicators.change_10_percent)
                ),
            },
        )
        order_book_age = snapshot_at - market.order_book.captured_at
        order_book_age_ms = max(0, int(order_book_age.total_seconds() * 1000))
        trading_status = market.trading_status.status if market.trading_status is not None else "UNAVAILABLE"
        audit_values: dict[str, object] = {
            "process_id": process_id,
            "automation_id": str(command.automation_id),
            "broker_id": str(command.broker_id),
            "account_id": command.account_id,
            "instrument_id": command.external_instrument_id,
            "decision": decision.kind.value,
            "reason_code": decision.reason_code,
            "current_price": str(market.order_book.best_bid.price),
            "best_bid": str(market.order_book.best_bid.price),
            "best_ask": str(market.order_book.best_ask.price),
            "averaging_step_percent": str(state.indicators.averaging_step_percent),
            "minimum_net_profit_percent": str(state.indicators.minimum_net_profit_percent),
            "order_book_age_ms": order_book_age_ms,
            "trading_status": trading_status,
            "cycle_pending_low_before": (None if cycle_before is None else str(cycle_before)),
            "cycle_pending_low_after": (None if state.cycle.pending_low is None else str(state.cycle.pending_low)),
            **audit_cash,
        }

        return DecisionMaterialization(item=item, request=request, audit_values=audit_values)

    def _strategy_snapshot(self) -> dict[str, object]:
        return {
            "strategy_code": STRATEGY_CODE,
            "strategy_version": STRATEGY_VERSION,
            **{
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in self._settings.model_dump().items()
            },
        }

    async def _cash_window(
        self,
        cash: CashReservationPort,
        account_id: str,
        currency: str,
    ) -> tuple[Decimal, Decimal]:
        return await asyncio.gather(
            cash.available(account_id, currency),
            cash.reserved(account_id, currency),
        )

    @staticmethod
    def _build_request(
        intent_id: str,
        command: AutomationCommand,
        instrument_type: str,
        side: OrderSide,
        quantity_lots: int,
        limit_price: Decimal,
        process_id: str,
        broker_id: str,
        currency: str,
        required_order_cash: Decimal,
    ) -> DispatchRequest:
        return DispatchRequest(
            idempotency_key=intent_id,
            account_id=command.account_id,
            instrument_id=command.external_instrument_id,
            side=side,
            quantity_lots=quantity_lots,
            limit_price=limit_price,
            instrument_type=instrument_type,
            automation_id=str(command.automation_id),
            lot_size=command.lot_size,
            process_id=process_id,
            broker_id=broker_id,
            reservation_currency=currency,
            required_cash=required_order_cash,
        )
