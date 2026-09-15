"""Session-bound position-cycle, lot and allocation repository."""

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from moex_sentinel.domain.trading_facts import (
    ExecutionLotAllocationDraft,
    PositionCycleDraft,
    PositionCycleState,
    PositionLotDraft,
    PositionLotSource,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.models.automation_facts import PositionCycleModel, TradingAutomationModel
from moex_sentinel.storage.models.order_facts import TradeExecutionModel
from moex_sentinel.storage.models.position_facts import (
    ExecutionLotAllocationModel,
    PositionLotModel,
)
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.repositories.trading_facts_support import (
    append_idempotent,
    flush_or_translate,
    require_scoped_reference,
)


def _cycle_value(model: PositionCycleModel) -> PositionCycleDraft:
    return PositionCycleDraft(
        id=model.id,
        user_broker_id=model.user_broker_id,
        automation_id=model.automation_id,
        instrument_id=model.instrument_id,
        state=PositionCycleState(model.state),
        quantity_lots=model.quantity_lots,
        average_entry_price=model.average_entry_price,
        invested_amount=model.invested_amount,
        realized_pnl=model.realized_pnl,
        unrealized_pnl=model.unrealized_pnl,
        net_pnl=model.net_pnl,
        accumulated_commissions=model.accumulated_commissions,
        opened_at=model.opened_at,
        closed_at=model.closed_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _lot_value(model: PositionLotModel) -> PositionLotDraft:
    return PositionLotDraft(
        id=model.id,
        user_broker_id=model.user_broker_id,
        automation_id=model.automation_id,
        position_cycle_id=model.position_cycle_id,
        buy_execution_id=model.buy_execution_id,
        source=PositionLotSource(model.source),
        original_lots=model.original_lots,
        remaining_lots=model.remaining_lots,
        entry_price=model.entry_price,
        entry_commission=model.entry_commission,
        opened_at=model.opened_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _allocation_value(model: ExecutionLotAllocationModel) -> ExecutionLotAllocationDraft:
    return ExecutionLotAllocationDraft(
        id=model.id,
        user_broker_id=model.user_broker_id,
        automation_id=model.automation_id,
        position_cycle_id=model.position_cycle_id,
        sell_execution_id=model.sell_execution_id,
        position_lot_id=model.position_lot_id,
        allocated_lots=model.allocated_lots,
        entry_value=model.entry_value,
        exit_value=model.exit_value,
        entry_commission=model.entry_commission,
        exit_commission=model.exit_commission,
        realized_pnl=model.realized_pnl,
        allocated_at=model.allocated_at,
        created_at=model.created_at,
    )


def _require_scope(user_broker_id: str, value_scope: str, entity_type: str) -> None:
    if user_broker_id != value_scope:
        raise TradingFactPersistenceError(TradingFactErrorCode.CROSS_SCOPE, entity_type=entity_type)


class PositionLedgerRepository:
    """Persist rebuildable position aggregates and immutable attribution."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def open_cycle(self, user_broker_id: str, value: PositionCycleDraft) -> PositionCycleDraft:
        _require_scope(user_broker_id, value.user_broker_id, "position_cycle")
        require_scoped_reference(
            self._session,
            scope_column=TradingAutomationModel.user_broker_id,
            id_column=TradingAutomationModel.id,
            user_broker_id=user_broker_id,
            reference_id=value.automation_id,
            entity_type="position_cycle",
        )
        require_scoped_reference(
            self._session,
            scope_column=BrokerInstrumentModel.user_broker_id,
            id_column=BrokerInstrumentModel.id,
            user_broker_id=user_broker_id,
            reference_id=value.instrument_id,
            entity_type="position_cycle",
        )
        matching_automation = self._session.scalar(
            select(TradingAutomationModel.id).where(
                TradingAutomationModel.user_broker_id == user_broker_id,
                TradingAutomationModel.id == value.automation_id,
                TradingAutomationModel.instrument_id == value.instrument_id,
            )
        )
        if matching_automation is None:
            raise TradingFactPersistenceError(TradingFactErrorCode.INVALID_STATE, entity_type="position_cycle")
        model = PositionCycleModel(**value.model_dump(mode="python"))
        return append_idempotent(
            self._session,
            candidate=model,
            identity=and_(
                PositionCycleModel.user_broker_id == user_broker_id,
                PositionCycleModel.id == value.id,
            ),
            to_value=_cycle_value,
            conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
            entity_type="position_cycle",
        )

    def replace_cycle_aggregate(self, user_broker_id: str, value: PositionCycleDraft) -> PositionCycleDraft:
        _require_scope(user_broker_id, value.user_broker_id, "position_cycle")
        result = self._session.execute(
            update(PositionCycleModel)
            .where(
                PositionCycleModel.user_broker_id == user_broker_id,
                PositionCycleModel.id == value.id,
                PositionCycleModel.automation_id == value.automation_id,
                PositionCycleModel.instrument_id == value.instrument_id,
                PositionCycleModel.updated_at <= value.updated_at,
            )
            .values(
                state=value.state.value,
                quantity_lots=value.quantity_lots,
                average_entry_price=value.average_entry_price,
                invested_amount=value.invested_amount,
                realized_pnl=value.realized_pnl,
                unrealized_pnl=value.unrealized_pnl,
                net_pnl=value.net_pnl,
                accumulated_commissions=value.accumulated_commissions,
                opened_at=value.opened_at,
                closed_at=value.closed_at,
                updated_at=value.updated_at,
            )
            .returning(PositionCycleModel.id)
        )
        if result.scalar_one_or_none() is None:
            current = self.get_cycle(user_broker_id, value.id)
            if current.automation_id != value.automation_id or current.instrument_id != value.instrument_id:
                raise TradingFactPersistenceError(TradingFactErrorCode.NOT_FOUND, entity_type="position_cycle")
            return current
        flush_or_translate(self._session, entity_type="position_cycle")
        return self.get_cycle(user_broker_id, value.id)

    def append_lot(self, user_broker_id: str, value: PositionLotDraft) -> PositionLotDraft:
        _require_scope(user_broker_id, value.user_broker_id, "position_lot")
        cycle = self._session.scalar(
            select(PositionCycleModel).where(
                PositionCycleModel.user_broker_id == user_broker_id,
                PositionCycleModel.id == value.position_cycle_id,
                PositionCycleModel.automation_id == value.automation_id,
            )
        )
        if cycle is None:
            raise TradingFactPersistenceError(TradingFactErrorCode.CROSS_SCOPE, entity_type="position_lot")
        if value.source is PositionLotSource.BROKER_EXECUTION:
            execution = self._session.scalar(
                select(TradeExecutionModel).where(
                    TradeExecutionModel.user_broker_id == user_broker_id,
                    TradeExecutionModel.id == value.buy_execution_id,
                    TradeExecutionModel.automation_id == value.automation_id,
                    TradeExecutionModel.position_cycle_id == value.position_cycle_id,
                )
            )
            if execution is None:
                raise TradingFactPersistenceError(TradingFactErrorCode.CROSS_SCOPE, entity_type="position_lot")
            if execution.side != "BUY":
                raise TradingFactPersistenceError(TradingFactErrorCode.INVALID_STATE, entity_type="position_lot")
        elif value.buy_execution_id is not None:
            raise TradingFactPersistenceError(TradingFactErrorCode.INVALID_STATE, entity_type="position_lot")
        model = PositionLotModel(**value.model_dump(mode="python"))
        return append_idempotent(
            self._session,
            candidate=model,
            identity=and_(PositionLotModel.user_broker_id == user_broker_id, PositionLotModel.id == value.id),
            to_value=_lot_value,
            conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
            entity_type="position_lot",
        )

    def append_allocation(
        self,
        user_broker_id: str,
        value: ExecutionLotAllocationDraft,
    ) -> ExecutionLotAllocationDraft:
        _require_scope(user_broker_id, value.user_broker_id, "execution_lot_allocation")
        execution = self._session.scalar(
            select(TradeExecutionModel).where(
                TradeExecutionModel.user_broker_id == user_broker_id,
                TradeExecutionModel.id == value.sell_execution_id,
                TradeExecutionModel.automation_id == value.automation_id,
                TradeExecutionModel.position_cycle_id == value.position_cycle_id,
            )
        )
        lot_exists = self._session.scalar(
            select(PositionLotModel.id).where(
                PositionLotModel.user_broker_id == user_broker_id,
                PositionLotModel.id == value.position_lot_id,
                PositionLotModel.automation_id == value.automation_id,
                PositionLotModel.position_cycle_id == value.position_cycle_id,
            )
        )
        if execution is None or lot_exists is None:
            raise TradingFactPersistenceError(
                TradingFactErrorCode.CROSS_SCOPE,
                entity_type="execution_lot_allocation",
            )
        if execution.side != "SELL":
            raise TradingFactPersistenceError(
                TradingFactErrorCode.INVALID_STATE,
                entity_type="execution_lot_allocation",
            )
        model = ExecutionLotAllocationModel(**value.model_dump(mode="python"))
        return append_idempotent(
            self._session,
            candidate=model,
            identity=and_(
                ExecutionLotAllocationModel.sell_execution_id == value.sell_execution_id,
                ExecutionLotAllocationModel.position_lot_id == value.position_lot_id,
            ),
            to_value=_allocation_value,
            conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
            entity_type="execution_lot_allocation",
        )

    def append_allocation_and_decrement(
        self,
        user_broker_id: str,
        value: ExecutionLotAllocationDraft,
        *,
        expected_remaining_lots: int,
        remaining_lots_after: int,
    ) -> ExecutionLotAllocationDraft:
        _require_scope(user_broker_id, value.user_broker_id, "execution_lot_allocation")
        if remaining_lots_after != expected_remaining_lots - value.allocated_lots:
            raise TradingFactPersistenceError(
                TradingFactErrorCode.INVALID_STATE,
                entity_type="execution_lot_allocation",
            )
        execution = self._session.scalar(
            select(TradeExecutionModel).where(
                TradeExecutionModel.user_broker_id == user_broker_id,
                TradeExecutionModel.id == value.sell_execution_id,
                TradeExecutionModel.automation_id == value.automation_id,
                TradeExecutionModel.position_cycle_id == value.position_cycle_id,
                TradeExecutionModel.side == "SELL",
            )
        )
        if execution is None:
            raise TradingFactPersistenceError(
                TradingFactErrorCode.CROSS_SCOPE,
                entity_type="execution_lot_allocation",
            )
        result = self._session.execute(
            update(PositionLotModel)
            .where(
                PositionLotModel.user_broker_id == user_broker_id,
                PositionLotModel.id == value.position_lot_id,
                PositionLotModel.automation_id == value.automation_id,
                PositionLotModel.position_cycle_id == value.position_cycle_id,
                PositionLotModel.remaining_lots == expected_remaining_lots,
            )
            .values(remaining_lots=remaining_lots_after)
            .returning(PositionLotModel.id)
        )
        if result.scalar_one_or_none() is None:
            raise TradingFactPersistenceError(
                TradingFactErrorCode.INVALID_STATE,
                entity_type="execution_lot_allocation",
            )
        flush_or_translate(self._session, entity_type="position_lot")
        return self.append_allocation(user_broker_id, value)

    def get_cycle(self, user_broker_id: str, cycle_id: str) -> PositionCycleDraft:
        model = self._session.scalar(
            select(PositionCycleModel).where(
                PositionCycleModel.user_broker_id == user_broker_id,
                PositionCycleModel.id == cycle_id,
            )
        )
        if model is None:
            raise TradingFactPersistenceError(TradingFactErrorCode.NOT_FOUND, entity_type="position_cycle")
        return _cycle_value(model)

    def list_open_lots(self, user_broker_id: str, cycle_id: str) -> tuple[PositionLotDraft, ...]:
        statement = (
            select(PositionLotModel)
            .where(
                PositionLotModel.user_broker_id == user_broker_id,
                PositionLotModel.position_cycle_id == cycle_id,
                PositionLotModel.remaining_lots > 0,
            )
            .order_by(PositionLotModel.opened_at.desc(), PositionLotModel.id.desc())
        )
        return tuple(_lot_value(model) for model in self._session.scalars(statement))

    def list_allocations(
        self,
        user_broker_id: str,
        cycle_id: str,
    ) -> tuple[ExecutionLotAllocationDraft, ...]:
        statement = (
            select(ExecutionLotAllocationModel)
            .where(
                ExecutionLotAllocationModel.user_broker_id == user_broker_id,
                ExecutionLotAllocationModel.position_cycle_id == cycle_id,
            )
            .order_by(
                ExecutionLotAllocationModel.allocated_at,
                ExecutionLotAllocationModel.id,
            )
        )
        return tuple(_allocation_value(model) for model in self._session.scalars(statement))
