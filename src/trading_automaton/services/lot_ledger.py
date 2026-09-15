"""Durable internal LIFO attribution for broker position lots."""

from datetime import datetime
from decimal import Decimal

from trading_automaton.domain.storage_dtos import (
    TradeLotRecord,
)
from trading_automaton.storage.repository import (
    LocalAutomationRepository,
)


class LotLedgerMismatchError(RuntimeError):
    pass


class ExecutionPriceError(ValueError):
    pass


class LotLedgerService:
    def __init__(self, repository: LocalAutomationRepository) -> None:
        self._repository = repository

    def record_buy_execution(
        self,
        *,
        automation_id: str,
        intent_id: str,
        quantity_lots: int,
        price: Decimal,
        commission: Decimal,
        executed_at: datetime,
    ) -> TradeLotRecord:
        if quantity_lots <= 0:
            raise ValueError("A filled buy must contain at least one lot.")
        return self._repository.create_trade_lot(
            automation_id=automation_id,
            source_intent_id=intent_id,
            source="EXECUTED",
            quantity_lots=quantity_lots,
            entry_price=price,
            entry_commission=commission,
            opened_at=executed_at,
        )

    def open_lots(self, automation_id: str) -> tuple[TradeLotRecord, ...]:
        return tuple(self._repository.list_open_lots(automation_id))

    def normalize_execution_price(
        self,
        *,
        executed_amount: Decimal,
        executed_lots: int,
        lot_size: int,
        broker_price: Decimal,
        limit_price: Decimal,
    ) -> Decimal:
        if executed_amount > 0 and executed_lots > 0 and lot_size > 0:
            price = executed_amount / (Decimal(executed_lots) * Decimal(lot_size))
        else:
            price = broker_price
        if price <= 0 or limit_price <= 0:
            raise ExecutionPriceError("PRICE_UNAVAILABLE")
        if abs(price - limit_price) / limit_price > Decimal("0.20"):
            raise ExecutionPriceError("PRICE_OUT_OF_RANGE")
        return price

    def allocate_sell_execution(
        self,
        *,
        automation_id: str,
        intent_id: str,
        quantity_lots: int,
        price: Decimal,
        commission: Decimal,
        executed_at: datetime,
        lot_size: int,
    ) -> None:
        try:
            self._repository.allocate_sell_lifo(
                automation_id=automation_id,
                sell_intent_id=intent_id,
                quantity_lots=quantity_lots,
                exit_price=price,
                exit_commission=commission,
                closed_at=executed_at,
                lot_size=lot_size,
            )
        except ValueError as error:
            raise LotLedgerMismatchError(str(error)) from error

    def reconcile(
        self,
        *,
        automation_id: str,
        broker_lots: int,
        average_price: Decimal,
        occurred_at: datetime,
    ) -> list[TradeLotRecord]:
        lots = self._repository.list_open_lots(automation_id)
        ledger_lots = sum(item.remaining_lots for item in lots)
        if not lots and broker_lots > 0:
            self._repository.create_trade_lot(
                automation_id=automation_id,
                source_intent_id=None,
                source="RECONCILED",
                quantity_lots=broker_lots,
                entry_price=average_price,
                entry_commission=Decimal(),
                opened_at=occurred_at,
            )
            return self._repository.list_open_lots(automation_id)
        if ledger_lots != broker_lots:
            raise LotLedgerMismatchError(f"Broker lots ({broker_lots}) differ from ledger lots ({ledger_lots}).")
        return lots
