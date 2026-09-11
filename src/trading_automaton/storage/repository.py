"""Short worker-local transactions for cache, outbox and recovery markers."""

from datetime import datetime, timedelta
from decimal import Decimal
from itertools import groupby
from threading import Lock
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from sentinel_contracts.automation_lifecycle import (
    InvalidAutomationTransition,
    TransitionOrigin,
    validate_automation_transition,
)
from sentinel_contracts.broker_execution import OrderSide
from sentinel_contracts.business_audit import BusinessAuditEvent
from sentinel_contracts.strategy import STRATEGY_CODE, STRATEGY_VERSION
from sentinel_contracts.time import utc_now_ms
from sentinel_contracts.trading import AutomationState, DecisionKind
from sentinel_contracts.trading_facts import (
    AutomationCommand as TypedAutomationCommand,
)
from sentinel_contracts.trading_facts import (
    AutomationStateChangedPayload,
    BrokerOrderRecordedPayload,
    BrokerOrderStateChangedPayload,
    BrokerPositionBootstrap,
    ExecutionLotAllocatedPayload,
    FactBrokerOrderStatus,
    FactBrokerOrderType,
    FactExecutionSource,
    FactKind,
    FactOrderIntentKind,
    FactPositionCycleState,
    FactTradeAuditLevel,
    PositionCycleUpdatedPayload,
    PositionLotOpenedPayload,
    PositionLotSource,
    TradeAuditRecordedPayload,
    TradeDecisionRecordedPayload,
    TradeExecutionRecordedPayload,
)
from trading_automaton.domain.position_valuation import lot_position_snapshot
from trading_automaton.domain.storage_dtos import (
    AccountCommissionProfile,
    AccountCommissionProfileKey,
    ActiveBuyIntentReservation,
    BatchPersistResult,
    DecisionBatchItem,
    DecisionRecord,
    ExecutionFinalization,
    ExecutionFinalizationResult,
    FactOutboxRecord,
    IntentBatchItem,
    IntentHistory,
    LocalAutomationRecord,
    LocalIntentRecord,
    TradeLotRecord,
    TradingCycleState,
)
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import (
    AccountCommissionProfileModel,
    BusinessAuditEventModel,
    CachedAutomationModel,
    FactOutboxModel,
    LocalIntentModel,
    LotAllocationModel,
    TradeDecisionModel,
    TradeLotModel,
    TradingCycleStateModel,
    WorkerRunModel,
)

__all__ = [
    "AccountCommissionProfile",
    "AccountCommissionProfileKey",
    "ActiveBuyIntentReservation",
    "BatchPersistResult",
    "DecisionBatchItem",
    "DecisionRecord",
    "ExecutionFinalization",
    "ExecutionFinalizationResult",
    "FactOutboxRecord",
    "IntentBatchItem",
    "IntentHistory",
    "LocalAutomationRecord",
    "LocalAutomationRepository",
    "LocalIntentRecord",
    "TradeLotRecord",
    "TradingCycleState",
]


class LocalAutomationRepository:
    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        fact_writer: FactOutboxWriter | None = None,
    ) -> None:
        self._factory = factory
        self._fact_writer = fact_writer or FactOutboxWriter()
        # Serializes finalizations inside the supported single worker process.
        # Multiple worker processes sharing one SQLite file remain unsupported.
        self._finalize_lock = Lock()

    @staticmethod
    def _active_intent_predicate() -> ColumnElement[bool]:
        return or_(
            LocalIntentModel.state.in_(
                (
                    "CREATED",
                    "DISPATCH_PENDING",
                    "SUBMITTING",
                    "SUBMITTED",
                    "ACCEPTED",
                    "PARTIALLY_FILLED",
                    "UNCERTAIN",
                )
            ),
            and_(
                LocalIntentModel.state == "FILLED",
                LocalIntentModel.execution_facts_emitted_at.is_(None),
            ),
        )

    @staticmethod
    def _validate_state_metadata(target: AutomationState, safe_message: str) -> None:
        if target is AutomationState.HOLD and not safe_message.strip():
            raise ValueError("Worker HOLD transition requires a nonblank hold reason.")

    @staticmethod
    def _compare_and_set_state(
        session: Session,
        cached: CachedAutomationModel,
        target: AutomationState,
    ) -> None:
        result = session.execute(
            update(CachedAutomationModel)
            .where(
                CachedAutomationModel.automation_id == cached.automation_id,
                CachedAutomationModel.state == cached.state,
                CachedAutomationModel.revision == cached.revision,
                CachedAutomationModel.last_sequence_number == cached.last_sequence_number,
            )
            .values(state=target.value)
            .returning(CachedAutomationModel.automation_id)
            .execution_options(synchronize_session=False)
        )
        if result.scalar_one_or_none() is None:
            raise InvalidAutomationTransition("Worker automation state changed concurrently.")
        session.refresh(cached)

    def get_commission_profile(
        self,
        key: AccountCommissionProfileKey,
    ) -> AccountCommissionProfile | None:
        with self._factory() as session:
            model = session.get(
                AccountCommissionProfileModel,
                (key.broker_id, key.account_id, key.instrument_type, key.currency),
            )
            if model is None:
                return None
            return self._commission_profile(model)

    def upsert_commission_profile(self, profile: AccountCommissionProfile) -> None:
        key = profile.key
        identity = (key.broker_id, key.account_id, key.instrument_type, key.currency)
        with self._factory.begin() as session:
            model = session.get(AccountCommissionProfileModel, identity)
            if model is None:
                model = AccountCommissionProfileModel(
                    broker_id=key.broker_id,
                    account_id=key.account_id,
                    instrument_type=key.instrument_type,
                    currency=key.currency,
                    buy_rate=profile.buy_rate,
                    sell_rate=profile.sell_rate,
                    service_rate=profile.service_rate,
                    deal_rate=profile.deal_rate,
                    source=profile.source,
                    calculated_at=profile.calculated_at,
                    valid_until=profile.valid_until,
                )
                session.add(model)
                return
            model.buy_rate = profile.buy_rate
            model.sell_rate = profile.sell_rate
            model.service_rate = profile.service_rate
            model.deal_rate = profile.deal_rate
            model.source = profile.source
            model.calculated_at = profile.calculated_at
            model.valid_until = profile.valid_until

    def append_audit_events(self, events: list[BusinessAuditEvent]) -> None:
        with self._factory.begin() as session:
            existing_ids = set(
                session.scalars(
                    select(BusinessAuditEventModel.event_id).where(
                        BusinessAuditEventModel.event_id.in_([event.event_id for event in events])
                    )
                ).all()
            )
            for event in events:
                if event.event_id in existing_ids:
                    continue
                existing_ids.add(event.event_id)
                session.add(
                    BusinessAuditEventModel(
                        event_id=event.event_id,
                        process_id=event.process_id,
                        parent_process_id=event.parent_process_id,
                        automation_id=event.automation_id,
                        broker_id=event.broker_id,
                        account_id=event.account_id,
                        instrument_id=event.instrument_id,
                        level=event.level.value,
                        stage=event.stage.value,
                        message=event.message,
                        event_data=event.data,
                        occurred_at=event.occurred_at,
                        critical=event.critical,
                    )
                )
                cached = session.get(CachedAutomationModel, event.automation_id)
                if cached is None or cached.fact_instrument_id is None:
                    raise KeyError(event.automation_id)
                self._fact_writer.append(
                    session,
                    cached,
                    payload=TradeAuditRecordedPayload(
                        audit_event_id=UUID(event.event_id),
                        process_id=UUID(event.process_id),
                        parent_process_id=UUID(event.parent_process_id) if event.parent_process_id else None,
                        decision_id=None,
                        broker_order_id=None,
                        execution_id=None,
                        instrument_id=UUID(cached.fact_instrument_id),
                        level=FactTradeAuditLevel(event.level.value),
                        stage=event.stage.value,
                        safe_message=event.message,
                        data=event.data,
                        occurred_at=event.occurred_at,
                        created_at=event.occurred_at,
                        critical=event.critical,
                    ),
                    safe_message=event.message,
                    occurred_at=event.occurred_at,
                )

    def transition_state(
        self,
        *,
        automation_id: str,
        state: str,
        safe_message: str,
        occurred_at: datetime,
    ) -> bool:
        with self._factory.begin() as session:
            cached = session.get(CachedAutomationModel, automation_id)
            if cached is None:
                raise KeyError(automation_id)
            previous_state = AutomationState(cached.state)
            target_state = AutomationState(state)
            self._validate_state_metadata(target_state, safe_message)
            if not validate_automation_transition(
                previous_state,
                target_state,
                origin=TransitionOrigin.WORKER_FACT,
            ):
                return False
            self._compare_and_set_state(session, cached, target_state)
            self._fact_writer.append(
                session,
                cached,
                payload=AutomationStateChangedPayload(
                    state=target_state,
                    suspended_from_state=(previous_state if target_state is AutomationState.HOLD else None),
                    hold_reason=safe_message if target_state is AutomationState.HOLD else None,
                    closed_at=occurred_at if target_state is AutomationState.CLOSED else None,
                ),
                safe_message=safe_message,
                occurred_at=occurred_at,
                changes_revision=True,
            )
            return True

    def finalize_execution(
        self,
        finalization: ExecutionFinalization,
        *,
        ledger_already_applied: bool = False,
    ) -> ExecutionFinalizationResult:
        """Persist a terminal intent, lot ledger, trading cycle and execution outbox atomically."""
        with self._finalize_lock, self._factory.begin() as session:
            intent = session.get(LocalIntentModel, finalization.intent_id)
            if intent is None:
                raise KeyError(finalization.intent_id)
            cached = session.get(CachedAutomationModel, finalization.automation_id)
            if cached is None:
                raise KeyError(finalization.automation_id)
            self._validate_finalization_source(intent, cached, finalization)
            if intent.terminal_at is not None:
                self._validate_execution_replay(intent, finalization)
                return ExecutionFinalizationResult(
                    self._intent_record(intent),
                    False,
                    None,
                )

            from_state = intent.state
            opens_position_cycle = (
                finalization.executed_lots > 0 and intent.side == "BUY" and cached.position_cycle_id is None
            )
            if finalization.executed_lots > 0:
                if intent.fact_execution_id is None:
                    intent.fact_execution_id = str(uuid4())
                if intent.side == "BUY" and cached.position_cycle_id is None:
                    cached.position_cycle_id = str(uuid4())
                if cached.position_cycle_id is None:
                    raise ValueError("Execution finalization has no active position cycle.")
                intent.position_cycle_id = cached.position_cycle_id
            intent.state = finalization.state
            intent.updated_at = finalization.occurred_at
            intent.broker_order_id = finalization.broker_order_id
            intent.requested_amount = finalization.requested_amount
            intent.executed_amount = finalization.executed_amount
            intent.estimated_commission = finalization.estimated_commission
            intent.executed_commission = finalization.executed_commission
            intent.executed_lots = finalization.executed_lots
            intent.executed_price = finalization.executed_price
            intent.execution_currency = finalization.currency if finalization.executed_lots > 0 else None
            intent.executed_at = finalization.executed_at
            intent.terminal_at = finalization.terminal_at
            session.flush()
            self._append_order_state_fact(
                session,
                intent,
                from_state=from_state,
                occurred_at=finalization.occurred_at,
                include_position_cycle=not opens_position_cycle,
            )

            if finalization.executed_lots > 0:
                if ledger_already_applied:
                    self._validate_preapplied_execution(session, intent)
                elif intent.side == "BUY":
                    self._create_trade_lot_in_session(session, intent, finalization)
                elif intent.side == "SELL":
                    self._allocate_sell_lifo_in_session(session, intent, finalization)
                self._finalize_trading_cycle(session, intent, finalization)
                snapshot = self._authoritative_position_snapshot(session, intent.automation_id, finalization)
                self._append_execution_graph(session, intent, cached, finalization, snapshot)
            else:
                snapshot = None
            return ExecutionFinalizationResult(self._intent_record(intent), True, snapshot)

    def _finalize_trading_cycle(
        self, session: Session, intent: LocalIntentModel, finalization: ExecutionFinalization
    ) -> None:
        # Imported here because the service's public state alias lives in this repository.
        from trading_automaton.services.trading_cycle import TradingCycleService  # noqa: PLC0415

        model = session.get(TradingCycleStateModel, intent.automation_id)
        state = (
            self._cycle_record(model)
            if model is not None
            else TradingCycleState(intent.automation_id, None, None, True, None, finalization.occurred_at)
        )
        service = TradingCycleService()
        now = max(state.updated_at, finalization.occurred_at)
        if intent.side == "BUY":
            candle_at = (finalization.executed_at or finalization.occurred_at).replace(second=0, microsecond=0)
            stored_candle = intent.strategy_snapshot.get("decision_candle_at")
            if isinstance(stored_candle, str):
                candle_at = datetime.fromisoformat(stored_candle)
            if state.last_buy_candle_at is not None:
                candle_at = max(candle_at, state.last_buy_candle_at)
            state = service.mark_buy(state, candle_at, now=now)
        else:
            state = service.mark_sell(state, finalization.executed_price, now=now)
        if model is None:
            model = TradingCycleStateModel(automation_id=intent.automation_id)
            session.add(model)
        model.pending_low = state.pending_low
        model.last_buy_candle_at = state.last_buy_candle_at
        model.sell_armed = state.sell_armed
        model.last_sell_price = state.last_sell_price
        model.updated_at = state.updated_at

    @staticmethod
    def _validate_preapplied_execution(session: Session, intent: LocalIntentModel) -> None:
        if intent.side == "BUY":
            evidence = session.scalar(
                select(TradeLotModel.id).where(TradeLotModel.source_intent_id == intent.idempotency_key).limit(1)
            )
        else:
            evidence = session.scalar(
                select(LotAllocationModel.id)
                .where(LotAllocationModel.sell_intent_id == intent.idempotency_key)
                .limit(1)
            )
        if evidence is None:
            raise ValueError("Reconciled execution has no pre-applied ledger evidence.")

    def _append_execution_graph(
        self,
        session: Session,
        intent: LocalIntentModel,
        cached: CachedAutomationModel,
        finalization: ExecutionFinalization,
        snapshot: dict[str, int | str],
    ) -> None:
        if intent.fact_execution_id is None or intent.position_cycle_id is None or cached.fact_instrument_id is None:
            raise ValueError("Execution fact identity is incomplete.")
        occurred_at = finalization.executed_at or finalization.occurred_at
        lot = session.scalar(select(TradeLotModel).where(TradeLotModel.source_intent_id == intent.idempotency_key))
        first_buy = intent.side == "BUY" and lot is not None and lot.original_lots == int(snapshot["quantity_lots"])

        if first_buy:
            self._append_cycle_fact(session, cached, finalization, snapshot, occurred_at)
        self._fact_writer.append(
            session,
            cached,
            payload=TradeExecutionRecordedPayload(
                execution_id=UUID(intent.fact_execution_id),
                broker_order_id=UUID(intent.idempotency_key),
                position_cycle_id=UUID(intent.position_cycle_id),
                instrument_id=UUID(cached.fact_instrument_id),
                external_execution_id=finalization.broker_order_id,
                side=OrderSide(intent.side),
                executed_lots=finalization.executed_lots,
                price=finalization.executed_price,
                value=finalization.executed_amount,
                broker_commission=finalization.executed_commission,
                other_fees=Decimal(),
                currency=finalization.currency,
                source=FactExecutionSource.BROKER_FILL,
                executed_at=occurred_at,
                created_at=occurred_at,
            ),
            safe_message="Trade execution recorded",
            occurred_at=occurred_at,
        )
        if intent.side == "BUY":
            if lot is None:
                raise ValueError("Buy execution has no persisted lot.")
            self._fact_writer.append(
                session,
                cached,
                payload=PositionLotOpenedPayload(
                    position_lot_id=UUID(lot.id),
                    position_cycle_id=UUID(intent.position_cycle_id),
                    buy_execution_id=UUID(intent.fact_execution_id),
                    source=PositionLotSource.BROKER_EXECUTION,
                    original_lots=lot.original_lots,
                    remaining_lots=lot.remaining_lots,
                    entry_price=lot.entry_price,
                    entry_commission=lot.entry_commission,
                    opened_at=lot.opened_at,
                    created_at=lot.opened_at,
                    updated_at=occurred_at,
                ),
                safe_message="Position lot opened",
                occurred_at=occurred_at,
            )
            if not first_buy:
                self._append_cycle_fact(session, cached, finalization, snapshot, occurred_at)
        else:
            allocations = session.scalars(
                select(LotAllocationModel)
                .where(LotAllocationModel.sell_intent_id == intent.idempotency_key)
                .order_by(LotAllocationModel.closed_at, LotAllocationModel.id)
            ).all()
            for allocation in allocations:
                lot = session.get(TradeLotModel, allocation.lot_id)
                if lot is None:
                    raise ValueError("Sell allocation has no persisted lot.")
                allocated_lots = allocation.quantity_lots
                self._fact_writer.append(
                    session,
                    cached,
                    payload=ExecutionLotAllocatedPayload(
                        allocation_id=UUID(allocation.id),
                        position_cycle_id=UUID(intent.position_cycle_id),
                        sell_execution_id=UUID(intent.fact_execution_id),
                        position_lot_id=UUID(lot.id),
                        allocated_lots=allocated_lots,
                        remaining_lots_after=lot.remaining_lots,
                        entry_value=lot.entry_price * allocated_lots * finalization.lot_size,
                        exit_value=allocation.exit_price * allocated_lots * finalization.lot_size,
                        entry_commission=(lot.entry_commission * Decimal(allocated_lots) / Decimal(lot.original_lots)),
                        exit_commission=allocation.exit_commission,
                        realized_pnl=allocation.realized_pnl,
                        allocated_at=allocation.closed_at,
                        created_at=allocation.closed_at,
                    ),
                    safe_message="Execution allocated to position lot",
                    occurred_at=allocation.closed_at,
                )
            self._append_cycle_fact(session, cached, finalization, snapshot, occurred_at)
            if int(snapshot["quantity_lots"]) == 0:
                cached.position_cycle_id = None
        intent.execution_facts_emitted_at = occurred_at

    def _append_cycle_fact(
        self,
        session: Session,
        cached: CachedAutomationModel,
        finalization: ExecutionFinalization,
        snapshot: dict[str, int | str],
        occurred_at: datetime,
    ) -> None:
        if cached.position_cycle_id is None or cached.fact_instrument_id is None:
            raise ValueError("Position cycle identity is incomplete.")
        first_lot_at = session.scalar(
            select(func.min(TradeLotModel.opened_at))
            .join(
                LocalIntentModel,
                LocalIntentModel.idempotency_key == TradeLotModel.source_intent_id,
            )
            .where(
                TradeLotModel.automation_id == cached.automation_id,
                LocalIntentModel.position_cycle_id == cached.position_cycle_id,
            )
        )
        opened_at = first_lot_at or occurred_at
        quantity_lots = int(snapshot["quantity_lots"])
        self._fact_writer.append(
            session,
            cached,
            payload=PositionCycleUpdatedPayload(
                position_cycle_id=UUID(cached.position_cycle_id),
                instrument_id=UUID(cached.fact_instrument_id),
                state=FactPositionCycleState.OPEN if quantity_lots > 0 else FactPositionCycleState.CLOSED,
                quantity_lots=quantity_lots,
                average_entry_price=Decimal(str(snapshot["average_price"])),
                invested_amount=Decimal(str(snapshot["invested_amount"])),
                realized_pnl=Decimal(str(snapshot["realized_pnl"])),
                unrealized_pnl=Decimal(str(snapshot["unrealized_pnl"])),
                net_pnl=Decimal(str(snapshot["net_pnl"])),
                accumulated_commissions=Decimal(str(snapshot["actual_commissions"])),
                opened_at=opened_at,
                closed_at=occurred_at if quantity_lots == 0 else None,
                created_at=opened_at,
                updated_at=occurred_at,
            ),
            safe_message="Position cycle updated",
            occurred_at=occurred_at,
        )

    @staticmethod
    def _validate_finalization_source(
        intent: LocalIntentModel,
        cached: CachedAutomationModel,
        finalization: ExecutionFinalization,
    ) -> None:
        if finalization.state not in {"FILLED", "REJECTED", "CANCELLED"}:
            raise ValueError("Execution finalization requires a terminal state.")
        if finalization.side not in {"BUY", "SELL"} or intent.side not in {"BUY", "SELL"}:
            raise ValueError("Execution finalization side is invalid.")
        expected = (
            (intent.automation_id, finalization.automation_id, "automation_id"),
            (intent.side, finalization.side, "side"),
            (intent.quantity_lots, finalization.quantity_lots, "quantity_lots"),
            (intent.limit_price, finalization.requested_price, "requested_price"),
            (cached.broker_id, finalization.broker_id, "broker_id"),
            (cached.account_id, finalization.account_id, "account_id"),
            (cached.instrument_id, finalization.instrument_id, "instrument_id"),
            (cached.lot_size, finalization.lot_size, "lot_size"),
        )
        mismatch = next((name for actual, provided, name in expected if actual != provided), None)
        if mismatch is not None:
            raise ValueError(f"Execution finalization {mismatch} conflicts with its source.")
        if finalization.executed_lots < 0 or finalization.executed_lots > finalization.quantity_lots:
            raise ValueError("Execution finalization executed_lots is invalid.")
        if finalization.state == "FILLED" and finalization.executed_lots <= 0:
            raise ValueError("FILLED execution must contain executed lots.")

    @staticmethod
    def _validate_execution_replay(
        intent: LocalIntentModel,
        finalization: ExecutionFinalization,
    ) -> None:
        facts = (
            (intent.state, finalization.state),
            (intent.broker_order_id, finalization.broker_order_id),
            (intent.requested_amount, finalization.requested_amount),
            (intent.executed_amount, finalization.executed_amount),
            (intent.estimated_commission, finalization.estimated_commission),
            (intent.executed_commission, finalization.executed_commission),
            (intent.executed_lots, finalization.executed_lots),
            (intent.executed_price, finalization.executed_price),
            (intent.executed_at, finalization.executed_at),
        )
        if any(actual != provided for actual, provided in facts):
            raise ValueError("Execution replay conflicts with the persisted terminal result.")
        if intent.executed_lots > 0 and intent.execution_currency != finalization.currency:
            raise ValueError("Execution replay currency conflicts with the persisted terminal result.")

    @staticmethod
    def _authoritative_position_snapshot(
        session: Session,
        automation_id: str,
        finalization: ExecutionFinalization,
    ) -> dict[str, int | str]:
        lots = session.scalars(select(TradeLotModel).where(TradeLotModel.automation_id == automation_id)).all()
        allocations = session.scalars(
            select(LotAllocationModel).where(LotAllocationModel.automation_id == automation_id)
        ).all()
        realized_pnl = sum((allocation.realized_pnl for allocation in allocations), start=Decimal())
        actual_commissions = sum((lot.entry_commission for lot in lots), start=Decimal()) + sum(
            (allocation.exit_commission for allocation in allocations),
            start=Decimal(),
        )
        return lot_position_snapshot(
            lots,
            lot_size=finalization.lot_size,
            mark_price=finalization.executed_price,
            realized_pnl=realized_pnl,
            actual_commissions=actual_commissions,
        ).to_metadata()

    @staticmethod
    def _create_trade_lot_in_session(
        session: Session,
        intent: LocalIntentModel,
        finalization: ExecutionFinalization,
    ) -> None:
        existing = session.scalar(select(TradeLotModel).where(TradeLotModel.source_intent_id == intent.idempotency_key))
        if existing is not None:
            return
        session.add(
            TradeLotModel(
                id=str(uuid4()),
                automation_id=intent.automation_id,
                source_intent_id=intent.idempotency_key,
                source="EXECUTED",
                original_lots=finalization.executed_lots,
                remaining_lots=finalization.executed_lots,
                entry_price=finalization.executed_price,
                entry_commission=finalization.executed_commission,
                opened_at=finalization.executed_at or finalization.occurred_at,
            )
        )

    @staticmethod
    def _allocate_sell_lifo_in_session(
        session: Session,
        intent: LocalIntentModel,
        finalization: ExecutionFinalization,
    ) -> None:
        existing = session.scalar(
            select(LotAllocationModel).where(LotAllocationModel.sell_intent_id == intent.idempotency_key).limit(1)
        )
        if existing is not None:
            return
        lots = session.scalars(
            select(TradeLotModel)
            .where(
                TradeLotModel.automation_id == intent.automation_id,
                TradeLotModel.remaining_lots > 0,
            )
            .order_by(TradeLotModel.opened_at.desc(), TradeLotModel.id.desc())
        ).all()
        if sum(row.remaining_lots for row in lots) < finalization.executed_lots:
            raise ValueError("Sell execution exceeds the worker lot ledger.")
        remaining = finalization.executed_lots
        for lot in lots:
            if remaining == 0:
                break
            allocated = min(remaining, lot.remaining_lots)
            exit_commission = (
                finalization.executed_commission * Decimal(allocated) / Decimal(finalization.executed_lots)
            )
            entry_commission = lot.entry_commission * Decimal(allocated) / Decimal(lot.original_lots)
            realized_pnl = (
                (finalization.executed_price - lot.entry_price) * finalization.lot_size * allocated
                - entry_commission
                - exit_commission
            )
            session.add(
                LotAllocationModel(
                    id=str(uuid4()),
                    automation_id=intent.automation_id,
                    sell_intent_id=intent.idempotency_key,
                    lot_id=lot.id,
                    quantity_lots=allocated,
                    exit_price=finalization.executed_price,
                    exit_commission=exit_commission,
                    realized_pnl=realized_pnl,
                    closed_at=finalization.executed_at or finalization.occurred_at,
                )
            )
            lot.remaining_lots -= allocated
            remaining -= allocated

    @staticmethod
    def _order_lifecycle_metadata(
        intent: LocalIntentModel,
        cached: CachedAutomationModel,
    ) -> dict[str, object]:
        def timestamp(value: datetime | None) -> str | None:
            return None if value is None else value.isoformat()

        return {
            "idempotency_key": intent.idempotency_key,
            "broker_id": cached.broker_id,
            "account_id": cached.account_id,
            "instrument_id": cached.instrument_id,
            "broker_order_id": intent.broker_order_id,
            "intent_kind": intent.kind,
            "side": intent.side,
            "status": intent.state,
            "quantity_lots": intent.quantity_lots,
            "requested_price": str(intent.limit_price),
            "requested_amount": str(intent.requested_amount),
            "executed_amount": str(intent.executed_amount),
            "estimated_commission": str(intent.estimated_commission),
            "executed_commission": str(intent.executed_commission),
            "strategy_snapshot": intent.strategy_snapshot,
            "created_at": timestamp(intent.created_at),
            "dispatch_started_at": timestamp(intent.dispatch_started_at),
            "broker_responded_at": timestamp(intent.broker_responded_at),
            "terminal_at": timestamp(intent.terminal_at),
            "executed_at": timestamp(intent.executed_at),
        }

    def get_state(self, automation_id: str) -> LocalAutomationRecord:
        with self._factory() as session:
            model = session.get(CachedAutomationModel, automation_id)
            if model is None:
                raise KeyError(automation_id)
            return LocalAutomationRecord(
                model.automation_id,
                model.state,
                model.revision,
                model.last_sequence_number,
            )

    def cache_command(self, command: TypedAutomationCommand) -> bool:
        with self._factory.begin() as session:
            automation_id = str(command.automation_id)
            model = session.get(CachedAutomationModel, automation_id)
            previous_sequence = None if model is None else model.last_sequence_number
            pending_fact = (
                None
                if model is None
                else session.scalar(
                    select(FactOutboxModel.event_id)
                    .where(
                        FactOutboxModel.automation_id == automation_id,
                        FactOutboxModel.delivery_state == "PENDING",
                    )
                    .limit(1)
                )
            )
            manual_resume = (
                model is not None
                and model.state == AutomationState.HOLD.value
                and command.state is AutomationState.IN_QUEUE
                and model.last_sequence_number <= command.last_sequence_number
                and model.revision <= command.revision
                and pending_fact is None
            )
            if model is not None and (
                model.last_sequence_number > command.last_sequence_number
                or model.revision > command.revision
                or (model.state != command.state.value and not manual_resume)
            ):
                return False
            if manual_resume:
                session.execute(
                    delete(FactOutboxModel).where(
                        FactOutboxModel.automation_id == automation_id,
                        FactOutboxModel.delivery_state == "FAILED",
                    )
                )
            if model is None:
                model = CachedAutomationModel(automation_id=automation_id)
                session.add(model)
            model.broker_id = str(command.broker_id)
            model.account_id = command.account_id
            model.user_broker_id = str(command.user_broker_id)
            model.instrument_id = command.external_instrument_id
            model.fact_instrument_id = str(command.instrument_id)
            model.currency = command.currency
            model.lot_size = command.lot_size
            model.min_price_increment = command.min_price_increment
            model.state = command.state.value
            model.revision = command.revision
            model.last_sequence_number = max(
                command.last_sequence_number,
                previous_sequence if previous_sequence is not None else 0,
            )
            model.resume_requested = command.resume_requested
            snapshot = command.bootstrap
            model.bootstrap_position_cycle_id = None if snapshot is None else str(snapshot.position_cycle_id)
            model.bootstrap_position_lot_id = None if snapshot is None else str(snapshot.position_lot_id)
            model.bootstrap_quantity_lots = None if snapshot is None else snapshot.quantity_lots
            model.bootstrap_average_price = None if snapshot is None else snapshot.average_price
            model.bootstrap_invested_amount = None if snapshot is None else snapshot.invested_amount
            model.bootstrap_currency = None if snapshot is None else snapshot.currency
            model.bootstrap_observed_at = None if snapshot is None else snapshot.observed_at
            return True

    def ensure_position_bootstrap(self, command: TypedAutomationCommand) -> bool:
        """Create recovery lot and the complete typed activation group in one transaction."""
        snapshot = command.bootstrap
        if snapshot is None:
            return False
        automation_id = str(command.automation_id)
        with self._factory.begin() as session:
            cached = session.get(CachedAutomationModel, automation_id)
            if cached is None:
                raise KeyError(automation_id)
            expected = (
                str(snapshot.position_cycle_id),
                str(snapshot.position_lot_id),
                snapshot.quantity_lots,
                snapshot.average_price,
                snapshot.invested_amount,
                snapshot.currency,
                snapshot.observed_at,
            )
            actual = (
                cached.bootstrap_position_cycle_id,
                cached.bootstrap_position_lot_id,
                cached.bootstrap_quantity_lots,
                cached.bootstrap_average_price,
                cached.bootstrap_invested_amount,
                cached.bootstrap_currency,
                cached.bootstrap_observed_at,
            )
            if actual != expected:
                raise ValueError("Cached broker-position bootstrap snapshot conflicts with the command.")
            existing = session.get(TradeLotModel, str(snapshot.position_lot_id))
            if existing is not None:
                self._validate_bootstrap_lot(existing, command)
                return False
            if (
                session.scalar(select(TradeLotModel.id).where(TradeLotModel.automation_id == automation_id).limit(1))
                is not None
            ):
                raise ValueError("Broker-position bootstrap cannot replace an existing lot ledger.")
            if (
                session.scalar(
                    select(LocalIntentModel.idempotency_key)
                    .where(LocalIntentModel.automation_id == automation_id)
                    .limit(1)
                )
                is not None
            ):
                raise ValueError("Broker-position bootstrap cannot run after a trade intent.")

            current_state = AutomationState(cached.state)
            if current_state is not AutomationState.HOLD:
                raise InvalidAutomationTransition(
                    f"WORKER_FACT bootstrap cannot transition {current_state.value} to IN_WORK."
                )
            validate_automation_transition(
                current_state,
                AutomationState.IN_WORK,
                origin=TransitionOrigin.WORKER_FACT,
                bootstrap=True,
            )
            self._compare_and_set_state(session, cached, AutomationState.IN_WORK)

            cached.position_cycle_id = str(snapshot.position_cycle_id)
            session.add(
                TradeLotModel(
                    id=str(snapshot.position_lot_id),
                    automation_id=automation_id,
                    source_intent_id=None,
                    source=PositionLotSource.BROKER_POSITION_BOOTSTRAP.value,
                    original_lots=snapshot.quantity_lots,
                    remaining_lots=snapshot.quantity_lots,
                    entry_price=snapshot.average_price,
                    entry_commission=Decimal(),
                    opened_at=snapshot.observed_at,
                )
            )
            if session.get(TradingCycleStateModel, automation_id) is None:
                session.add(
                    TradingCycleStateModel(
                        automation_id=automation_id,
                        pending_low=None,
                        last_buy_candle_at=None,
                        sell_armed=True,
                        last_sell_price=None,
                        updated_at=snapshot.observed_at,
                    )
                )
            session.flush()
            self._append_bootstrap_facts(session, cached, command)
            return True

    def _append_bootstrap_facts(
        self,
        session: Session,
        cached: CachedAutomationModel,
        command: TypedAutomationCommand,
    ) -> None:
        snapshot = command.bootstrap
        if snapshot is None or cached.fact_instrument_id is None:
            raise ValueError("Broker-position bootstrap context is incomplete.")
        self._fact_writer.append(
            session,
            cached,
            payload=PositionCycleUpdatedPayload(
                position_cycle_id=snapshot.position_cycle_id,
                instrument_id=UUID(cached.fact_instrument_id),
                state=FactPositionCycleState.OPEN,
                quantity_lots=snapshot.quantity_lots,
                average_entry_price=snapshot.average_price,
                invested_amount=snapshot.invested_amount,
                realized_pnl=Decimal(),
                unrealized_pnl=Decimal(),
                net_pnl=Decimal(),
                accumulated_commissions=Decimal(),
                opened_at=snapshot.observed_at,
                closed_at=None,
                created_at=snapshot.observed_at,
                updated_at=snapshot.observed_at,
            ),
            safe_message="Broker position cycle adopted",
            occurred_at=snapshot.observed_at,
        )
        self._fact_writer.append(
            session,
            cached,
            payload=PositionLotOpenedPayload(
                position_lot_id=snapshot.position_lot_id,
                position_cycle_id=snapshot.position_cycle_id,
                buy_execution_id=None,
                source=PositionLotSource.BROKER_POSITION_BOOTSTRAP,
                original_lots=snapshot.quantity_lots,
                remaining_lots=snapshot.quantity_lots,
                entry_price=snapshot.average_price,
                entry_commission=Decimal(),
                opened_at=snapshot.observed_at,
                created_at=snapshot.observed_at,
                updated_at=snapshot.observed_at,
            ),
            safe_message="Broker position lot adopted",
            occurred_at=snapshot.observed_at,
        )
        process_id = uuid5(NAMESPACE_URL, f"{command.automation_id}:bootstrap-process")
        audit_event_id = uuid5(NAMESPACE_URL, f"{command.automation_id}:bootstrap-audit")
        session.add(
            BusinessAuditEventModel(
                event_id=str(audit_event_id),
                process_id=str(process_id),
                parent_process_id=None,
                automation_id=str(command.automation_id),
                broker_id=str(command.broker_id),
                account_id=command.account_id,
                instrument_id=command.external_instrument_id,
                level=FactTradeAuditLevel.INFO.value,
                stage="BOOTSTRAP_POSITION_ADOPTED",
                message="Broker position adopted as clean runtime state",
                event_data={"strategy_code": STRATEGY_CODE, "strategy_version": STRATEGY_VERSION},
                occurred_at=snapshot.observed_at,
                critical=False,
            )
        )
        self._fact_writer.append(
            session,
            cached,
            payload=TradeAuditRecordedPayload(
                audit_event_id=audit_event_id,
                process_id=process_id,
                parent_process_id=None,
                decision_id=None,
                broker_order_id=None,
                execution_id=None,
                instrument_id=UUID(cached.fact_instrument_id),
                level=FactTradeAuditLevel.INFO,
                stage="BOOTSTRAP_POSITION_ADOPTED",
                safe_message="Broker position adopted as clean runtime state",
                data={"strategy_code": STRATEGY_CODE, "strategy_version": STRATEGY_VERSION},
                occurred_at=snapshot.observed_at,
                created_at=snapshot.observed_at,
                critical=False,
            ),
            safe_message="Broker position adopted as clean runtime state",
            occurred_at=snapshot.observed_at,
        )
        cached.state = AutomationState.IN_WORK.value
        self._fact_writer.append(
            session,
            cached,
            payload=AutomationStateChangedPayload(
                state=AutomationState.IN_WORK,
                suspended_from_state=None,
                hold_reason=None,
                closed_at=None,
            ),
            safe_message="Broker position bootstrap completed",
            occurred_at=snapshot.observed_at,
            changes_revision=True,
        )

    @staticmethod
    def _validate_bootstrap_lot(
        lot: TradeLotModel,
        command: TypedAutomationCommand,
    ) -> None:
        snapshot = command.bootstrap
        if snapshot is None:
            raise ValueError("Broker-position bootstrap snapshot is missing.")
        actual = (
            lot.automation_id,
            lot.source_intent_id,
            lot.source,
            lot.original_lots,
            lot.remaining_lots,
            lot.entry_price,
            lot.entry_commission,
            lot.opened_at,
        )
        expected = (
            str(command.automation_id),
            None,
            PositionLotSource.BROKER_POSITION_BOOTSTRAP.value,
            snapshot.quantity_lots,
            snapshot.quantity_lots,
            snapshot.average_price,
            Decimal(),
            snapshot.observed_at,
        )
        if actual != expected:
            raise ValueError("Existing broker-position bootstrap lot conflicts with the command.")

    def list_active(self) -> list[TypedAutomationCommand]:
        active_intent_exists = exists(
            select(LocalIntentModel.idempotency_key).where(
                LocalIntentModel.automation_id == CachedAutomationModel.automation_id,
                self._active_intent_predicate(),
            )
        )
        with self._factory() as session:
            rows = session.scalars(
                select(CachedAutomationModel)
                .where(
                    CachedAutomationModel.broker_id.is_not(None),
                    CachedAutomationModel.state != "HOLD",
                    or_(CachedAutomationModel.state != "CLOSED", active_intent_exists),
                )
                .order_by(CachedAutomationModel.automation_id)
            ).all()
            return [self._command(row) for row in rows]

    def list_monitored(self) -> list[TypedAutomationCommand]:
        active_intent_exists = exists(
            select(LocalIntentModel.idempotency_key).where(
                LocalIntentModel.automation_id == CachedAutomationModel.automation_id,
                self._active_intent_predicate(),
            )
        )
        with self._factory() as session:
            rows = session.scalars(
                select(CachedAutomationModel)
                .where(
                    CachedAutomationModel.broker_id.is_not(None),
                    or_(CachedAutomationModel.state != "CLOSED", active_intent_exists),
                )
                .order_by(CachedAutomationModel.automation_id)
            ).all()
            return [self._command(row) for row in rows]

    def synchronize_core_state(
        self,
        automation_id: str,
        *,
        state: str,
        revision: int,
        last_sequence_number: int | None,
    ) -> TypedAutomationCommand:
        with self._factory.begin() as session:
            model = session.get(CachedAutomationModel, automation_id)
            if model is None:
                raise KeyError(automation_id)
            pending_fact = session.scalar(
                select(FactOutboxModel.event_id).where(FactOutboxModel.automation_id == automation_id).limit(1)
            )
            if pending_fact is not None:
                return self._command(model)
            model.state = state
            model.revision = revision
            if last_sequence_number is not None:
                model.last_sequence_number = max(model.last_sequence_number, last_sequence_number)
            session.flush()
            return self._command(model)

    def ready_fact_outbox(
        self,
        limit: int,
        *,
        now: datetime,
        deadline_ms: int,
    ) -> list[FactOutboxRecord]:
        if limit <= 0:
            return []
        with self._factory() as session:
            pending = session.scalars(
                select(FactOutboxModel)
                .where(FactOutboxModel.delivery_state == "PENDING")
                .order_by(FactOutboxModel.automation_id, FactOutboxModel.sequence_number)
            ).all()
            eligible: list[list[FactOutboxModel]] = []
            blocked_automations: set[str] = set()
            for unit in self._fact_publication_units(session, pending):
                automation_id = unit[0].automation_id
                if automation_id in blocked_automations:
                    continue
                if any(row.next_retry_at is not None and row.next_retry_at > now for row in unit):
                    blocked_automations.add(automation_id)
                    continue
                eligible.append(unit)
            count = sum(len(unit) for unit in eligible)
            oldest = min((row.created_at for unit in eligible for row in unit), default=None)
            due_by_deadline = oldest is not None and now >= oldest + timedelta(milliseconds=deadline_ms)
            if count < limit and not due_by_deadline:
                return []
            rows: list[FactOutboxModel] = []
            selected_bootstraps: set[str] = set()
            for unit in sorted(eligible, key=lambda unit: (unit[0].occurred_at, unit[0].event_id)):
                automation_id = unit[0].automation_id
                if automation_id in selected_bootstraps:
                    continue
                rows.extend(unit)
                if len(unit) == 4:
                    # Bootstrap activation must remain the final fact in its
                    # first Core transaction. Deliver any later HOLD separately.
                    selected_bootstraps.add(automation_id)
                if len(rows) >= limit:
                    break
            rows.sort(key=lambda row: (row.occurred_at, row.event_id))
            return [self._fact_outbox_record(row) for row in rows]

    @staticmethod
    def _fact_publication_units(session: Session, pending: list[FactOutboxModel]) -> list[list[FactOutboxModel]]:
        """Keep the initial bootstrap quartet together without expanding runtime backlog."""
        units: list[list[FactOutboxModel]] = []
        bootstraps = {
            cached.automation_id: cached
            for cached in session.scalars(
                select(CachedAutomationModel).where(CachedAutomationModel.bootstrap_position_cycle_id.is_not(None))
            )
        }
        for automation_id, group in groupby(pending, key=lambda row: row.automation_id):
            rows = list(group)
            cached = bootstraps.get(automation_id)
            index = 0
            while index < len(rows):
                quartet = rows[index : index + 4]
                if (
                    cached is not None
                    and cached.bootstrap_position_cycle_id is not None
                    and len(quartet) == 4
                    and [row.fact_kind for row in quartet]
                    == [
                        FactKind.POSITION_CYCLE_UPDATED.value,
                        FactKind.POSITION_LOT_OPENED.value,
                        FactKind.TRADE_AUDIT_RECORDED.value,
                        FactKind.AUTOMATION_STATE_CHANGED.value,
                    ]
                    and [row.sequence_number for row in quartet]
                    == list(range(quartet[0].sequence_number, quartet[0].sequence_number + 4))
                    and quartet[0].payload["position_cycle_id"] == cached.bootstrap_position_cycle_id
                    and quartet[1].payload["position_cycle_id"] == cached.bootstrap_position_cycle_id
                    and quartet[1].payload["position_lot_id"] == cached.bootstrap_position_lot_id
                    and quartet[1].payload["source"] == PositionLotSource.BROKER_POSITION_BOOTSTRAP.value
                    and quartet[2].payload["stage"] == "BOOTSTRAP_POSITION_ADOPTED"
                    and quartet[3].payload["state"] == AutomationState.IN_WORK.value
                ):
                    units.append(quartet)
                    index += 4
                else:
                    units.append([rows[index]])
                    index += 1
        return units

    def has_pending_fact_outbox(self, automation_id: str) -> bool:
        with self._factory() as session:
            return (
                session.scalar(
                    select(FactOutboxModel.event_id).where(FactOutboxModel.automation_id == automation_id).limit(1)
                )
                is not None
            )

    def acknowledge_fact_outbox(
        self,
        automation_id: str,
        *,
        accepted_through_sequence: int,
        current_revision: int,
    ) -> None:
        with self._factory.begin() as session:
            session.execute(
                delete(FactOutboxModel).where(
                    FactOutboxModel.automation_id == automation_id,
                    FactOutboxModel.sequence_number <= accepted_through_sequence,
                )
            )
            cached = session.get(CachedAutomationModel, automation_id)
            if cached is not None:
                # Pending state facts already advanced the speculative Worker
                # revision. A prefix ACK must not make later appends reuse it.
                cached.revision = max(cached.revision, current_revision)

    def schedule_fact_retry(
        self,
        event_ids: tuple[str, ...],
        *,
        retry_count: int,
        next_retry_at: datetime,
    ) -> None:
        if not event_ids:
            return
        with self._factory.begin() as session:
            session.execute(
                update(FactOutboxModel)
                .where(FactOutboxModel.event_id.in_(event_ids))
                .values(retry_count=retry_count, next_retry_at=next_retry_at)
            )

    def reject_fact_outbox(self, automation_id: str, event_ids: tuple[str, ...], *, reason: str) -> None:
        with self._factory.begin() as session:
            session.execute(
                update(FactOutboxModel)
                .where(
                    FactOutboxModel.automation_id == automation_id,
                    FactOutboxModel.delivery_state == "PENDING",
                )
                .values(delivery_state="FAILED", next_retry_at=None)
            )
            cached = session.get(CachedAutomationModel, automation_id)
            if cached is not None and cached.state != AutomationState.CLOSED.value:
                cached.state = AutomationState.HOLD.value

    def reconcile_from_core(
        self,
        automation_id: str,
        *,
        state: str,
        revision: int,
        last_sequence_number: int,
    ) -> None:
        with self._factory.begin() as session:
            cached = session.get(CachedAutomationModel, automation_id)
            if cached is None:
                raise KeyError(automation_id)
            cached.state = state
            cached.revision = revision
            cached.last_sequence_number = last_sequence_number

    def begin_run(self, worker_id: str) -> bool:
        with self._factory.begin() as session:
            model = session.get(WorkerRunModel, worker_id)
            unclean = model is not None and not model.clean_shutdown
            if model is None:
                session.add(WorkerRunModel(worker_id=worker_id, clean_shutdown=False))
            else:
                model.clean_shutdown = False
            return unclean

    def finish_run(self, worker_id: str) -> None:
        with self._factory.begin() as session:
            model = session.get(WorkerRunModel, worker_id)
            if model is None:
                raise KeyError(worker_id)
            model.clean_shutdown = True

    def hold_active(self, reason: str, automation_id: str | None = None) -> None:
        self._validate_state_metadata(AutomationState.HOLD, reason)
        with self._factory.begin() as session:
            query = select(CachedAutomationModel).where(CachedAutomationModel.state.not_in(("HOLD", "CLOSED")))
            if automation_id is not None:
                query = query.where(CachedAutomationModel.automation_id == automation_id)
            for cached in session.scalars(query):
                previous_state = AutomationState(cached.state)
                validate_automation_transition(
                    previous_state,
                    AutomationState.HOLD,
                    origin=TransitionOrigin.WORKER_FACT,
                )
                self._compare_and_set_state(session, cached, AutomationState.HOLD)
                self._fact_writer.append(
                    session,
                    cached,
                    payload=AutomationStateChangedPayload(
                        state=AutomationState.HOLD,
                        suspended_from_state=previous_state,
                        hold_reason=reason,
                        closed_at=None,
                    ),
                    safe_message=reason,
                    occurred_at=utc_now_ms(),
                    changes_revision=True,
                )

    def update_intent(
        self,
        idempotency_key: str,
        *,
        state: str,
        occurred_at: datetime,
        broker_order_id: str | None = None,
        requested_amount: Decimal | None = None,
        executed_amount: Decimal | None = None,
        estimated_commission: Decimal | None = None,
        executed_commission: Decimal | None = None,
        executed_lots: int | None = None,
        executed_price: Decimal | None = None,
        executed_at: datetime | None = None,
        dispatch_started_at: datetime | None = None,
        broker_responded_at: datetime | None = None,
        terminal_at: datetime | None = None,
        process_id: str | None = None,
    ) -> LocalIntentRecord:
        with self._factory.begin() as session:
            model = session.get(LocalIntentModel, idempotency_key)
            if model is None:
                raise KeyError(idempotency_key)
            from_state = model.state
            model.state = state
            model.updated_at = occurred_at
            if broker_order_id is not None:
                model.broker_order_id = broker_order_id
            for name, value in (
                ("requested_amount", requested_amount),
                ("executed_amount", executed_amount),
                ("estimated_commission", estimated_commission),
                ("executed_commission", executed_commission),
                ("executed_lots", executed_lots),
                ("executed_price", executed_price),
                ("executed_at", executed_at),
                ("dispatch_started_at", dispatch_started_at),
                ("broker_responded_at", broker_responded_at),
                ("terminal_at", terminal_at),
            ):
                if value is not None:
                    setattr(model, name, value)
            session.flush()
            self._append_order_state_fact(session, model, from_state=from_state, occurred_at=occurred_at)
            return self._intent_record(model)

    def _append_order_state_fact(
        self,
        session: Session,
        intent: LocalIntentModel,
        *,
        from_state: str,
        occurred_at: datetime,
        include_position_cycle: bool = True,
    ) -> None:
        cached = session.get(CachedAutomationModel, intent.automation_id)
        decision = session.scalar(
            select(TradeDecisionModel).where(TradeDecisionModel.intent_id == intent.idempotency_key)
        )
        if cached is None or cached.fact_instrument_id is None or decision is None:
            raise KeyError(intent.automation_id)
        self._fact_writer.append(
            session,
            cached,
            payload=BrokerOrderStateChangedPayload(
                order_id=UUID(intent.idempotency_key),
                order_event_id=uuid4(),
                broker_order_id=UUID(intent.idempotency_key),
                decision_id=UUID(decision.id),
                position_cycle_id=(
                    UUID(intent.position_cycle_id) if include_position_cycle and intent.position_cycle_id else None
                ),
                instrument_id=UUID(cached.fact_instrument_id),
                idempotency_key=intent.idempotency_key,
                external_order_id=intent.broker_order_id,
                intent_kind=FactOrderIntentKind(intent.kind),
                side=OrderSide(intent.side),
                order_type=FactBrokerOrderType.LIMIT,
                state=FactBrokerOrderStatus(intent.state),
                quantity_lots=intent.quantity_lots,
                limit_price=intent.limit_price,
                requested_amount=intent.requested_amount,
                executed_amount=intent.executed_amount,
                estimated_commission=intent.estimated_commission,
                executed_commission=intent.executed_commission,
                # Intent JSON also carries private execution-cycle metadata.
                # Core requires the original immutable snapshot recorded with the order.
                strategy_snapshot=dict(decision.strategy_snapshot),
                dispatch_started_at=intent.dispatch_started_at,
                broker_responded_at=intent.broker_responded_at,
                executed_at=intent.executed_at,
                terminal_at=intent.terminal_at,
                updated_at=intent.updated_at,
                from_state=FactBrokerOrderStatus(from_state),
                to_state=FactBrokerOrderStatus(intent.state),
                safe_reason=intent.state,
                safe_message="Broker order state changed",
                occurred_at=occurred_at,
                created_at=intent.created_at,
            ),
            safe_message="Broker order state changed",
            occurred_at=occurred_at,
        )

    def get_active_intent(self, automation_id: str) -> LocalIntentRecord | None:
        with self._factory() as session:
            model = session.scalar(
                select(LocalIntentModel).where(
                    LocalIntentModel.automation_id == automation_id,
                    self._active_intent_predicate(),
                )
            )
            return None if model is None else self._intent_record(model)

    def get_latest_intent(self, automation_id: str) -> LocalIntentRecord | None:
        with self._factory() as session:
            model = session.scalar(
                select(LocalIntentModel)
                .where(LocalIntentModel.automation_id == automation_id)
                .order_by(LocalIntentModel.created_at.desc())
                .limit(1)
            )
            return None if model is None else self._intent_record(model)

    def list_active_buy_intent_reservations(
        self,
        broker_id: str | None = None,
    ) -> tuple[ActiveBuyIntentReservation, ...]:
        with self._factory() as session:
            query = (
                select(LocalIntentModel, CachedAutomationModel, TradeDecisionModel.estimated_commission)
                .join(
                    CachedAutomationModel,
                    CachedAutomationModel.automation_id == LocalIntentModel.automation_id,
                )
                .outerjoin(TradeDecisionModel, TradeDecisionModel.intent_id == LocalIntentModel.idempotency_key)
                .where(
                    LocalIntentModel.side == "BUY",
                    self._active_intent_predicate(),
                )
                .order_by(LocalIntentModel.created_at, LocalIntentModel.idempotency_key)
            )
            if broker_id is not None:
                query = query.where(CachedAutomationModel.broker_id == broker_id)
            rows = session.execute(query).all()
            reservations = []
            for intent, command, decision_commission in rows:
                if command.account_id is None or command.lot_size is None:
                    continue
                commission = max(intent.estimated_commission, Decimal(decision_commission or 0))
                reservations.append(
                    ActiveBuyIntentReservation(
                        intent.idempotency_key,
                        str(command.broker_id),
                        command.account_id,
                        command.currency,
                        intent.limit_price * command.lot_size * intent.quantity_lots + commission,
                        intent.side,
                        intent.state,
                    )
                )
            return tuple(reservations)

    def save_decision_batch(
        self,
        items: tuple[DecisionBatchItem, ...],
        *,
        occurred_at: datetime,
    ) -> BatchPersistResult:
        with self._factory.begin() as session:
            intent_automation_ids = [item.automation_id for item in items if item.intent is not None]
            if len(intent_automation_ids) != len(set(intent_automation_ids)):
                raise ValueError("Automation already has an active intent in batch.")
            if intent_automation_ids:
                active_rows = session.execute(
                    select(LocalIntentModel.automation_id, LocalIntentModel.idempotency_key).where(
                        LocalIntentModel.automation_id.in_(intent_automation_ids),
                        self._active_intent_predicate(),
                    )
                ).all()
                if active_rows:
                    conflicting = {automation_id for automation_id, _intent_id in active_rows}
                    items = tuple(
                        (
                            item.model_copy(
                                update={
                                    "estimated_commission": Decimal(),
                                    "decision": "WAIT",
                                    "reason_code": "ACTIVE_INTENT",
                                    "decision_quantity_lots": 0,
                                    "limit_price": None,
                                    "intent": None,
                                }
                            )
                            if item.automation_id in conflicting and item.intent is not None
                            else item
                        )
                        for item in items
                    )
            decisions: list[TradeDecisionModel] = []
            intents: list[LocalIntentModel] = []
            for item in items:
                cached = session.get(CachedAutomationModel, item.automation_id)
                if cached is None or cached.fact_instrument_id is None:
                    raise KeyError(item.automation_id)
                if not self._save_decision_cycle(session, item) and item.intent is not None:
                    item = item.model_copy(
                        update={
                            "estimated_commission": Decimal(),
                            "decision": "WAIT",
                            "reason_code": "CYCLE_STATE_CHANGED",
                            "decision_quantity_lots": 0,
                            "limit_price": None,
                            "intent": None,
                        }
                    )
                intent_id = None
                order_fact_at = None
                if item.intent is not None:
                    intent_id = item.intent.idempotency_key
                    order_fact_at = occurred_at + timedelta(milliseconds=1)
                    intent_model = LocalIntentModel(
                        idempotency_key=intent_id,
                        position_cycle_id=cached.position_cycle_id,
                        automation_id=item.automation_id,
                        kind=item.intent.kind,
                        side=item.intent.side,
                        state="DISPATCH_PENDING",
                        quantity_lots=item.intent.quantity_lots,
                        limit_price=item.intent.limit_price,
                        requested_amount=(item.intent.limit_price * item.lot_size * item.intent.quantity_lots),
                        estimated_commission=item.estimated_commission,
                        strategy_snapshot={
                            **item.strategy_snapshot,
                            **(
                                {"decision_candle_at": item.indicators["last_candle_at"]}
                                if item.indicators.get("last_candle_at") is not None
                                else {}
                            ),
                        },
                        created_at=order_fact_at,
                        updated_at=order_fact_at,
                    )
                    session.add(intent_model)
                    session.flush()
                    intents.append(intent_model)
                decision_model = TradeDecisionModel(
                    id=str(uuid4()),
                    process_id=item.process_id,
                    automation_id=item.automation_id,
                    broker_id=item.broker_id,
                    instrument_id=item.instrument_id,
                    occurred_at=occurred_at,
                    quantity_lots=item.quantity_lots,
                    lot_size=item.lot_size,
                    average_price=item.average_price,
                    current_price=item.current_price,
                    best_bid=item.best_bid,
                    best_ask=item.best_ask,
                    invested_amount=item.invested_amount,
                    estimated_commission=item.estimated_commission,
                    decision=item.decision,
                    reason_code=item.reason_code,
                    decision_quantity_lots=item.decision_quantity_lots,
                    limit_price=item.limit_price,
                    strategy_snapshot=item.strategy_snapshot,
                    intent_id=intent_id,
                )
                session.add(decision_model)
                decisions.append(decision_model)
                session.flush()
                self._fact_writer.append(
                    session,
                    cached,
                    payload=TradeDecisionRecordedPayload(
                        decision_id=UUID(decision_model.id),
                        process_id=UUID(item.process_id) if item.process_id else None,
                        position_cycle_id=UUID(cached.position_cycle_id) if cached.position_cycle_id else None,
                        instrument_id=UUID(cached.fact_instrument_id),
                        quantity_lots=item.quantity_lots,
                        lot_size=item.lot_size,
                        average_price=item.average_price,
                        invested_amount=item.invested_amount,
                        current_price=item.current_price,
                        best_bid=item.best_bid,
                        best_ask=item.best_ask,
                        indicators=item.indicators,
                        estimated_commission=item.estimated_commission,
                        decision=DecisionKind(item.decision),
                        reason_code=item.reason_code,
                        requested_quantity_lots=item.decision_quantity_lots,
                        limit_price=item.limit_price,
                        strategy_snapshot=item.strategy_snapshot,
                        decided_at=occurred_at,
                        created_at=occurred_at,
                    ),
                    safe_message="Trading decision recorded",
                    occurred_at=occurred_at,
                )
                if item.intent is not None:
                    if order_fact_at is None:
                        raise RuntimeError("Order fact timestamp was not initialized.")
                    self._fact_writer.append(
                        session,
                        cached,
                        payload=BrokerOrderRecordedPayload(
                            order_id=UUID(item.intent.idempotency_key),
                            decision_id=UUID(decision_model.id),
                            position_cycle_id=(UUID(cached.position_cycle_id) if cached.position_cycle_id else None),
                            instrument_id=UUID(cached.fact_instrument_id),
                            idempotency_key=item.intent.idempotency_key,
                            external_order_id=None,
                            intent_kind=FactOrderIntentKind(item.intent.kind),
                            side=OrderSide(item.intent.side),
                            order_type=FactBrokerOrderType.LIMIT,
                            state=FactBrokerOrderStatus.DISPATCH_PENDING,
                            quantity_lots=item.intent.quantity_lots,
                            limit_price=item.intent.limit_price,
                            requested_amount=(item.intent.limit_price * item.lot_size * item.intent.quantity_lots),
                            executed_amount=Decimal(),
                            estimated_commission=item.estimated_commission,
                            executed_commission=Decimal(),
                            strategy_snapshot=item.strategy_snapshot,
                            created_at=order_fact_at,
                            dispatch_started_at=None,
                            broker_responded_at=None,
                            executed_at=None,
                            terminal_at=None,
                            updated_at=order_fact_at,
                        ),
                        safe_message="Broker order recorded",
                        occurred_at=order_fact_at,
                    )
            session.flush()
            return BatchPersistResult(
                tuple(self._decision_record(item) for item in decisions),
                tuple(self._intent_record(item) for item in intents),
            )

    def _save_decision_cycle(self, session: Session, item: DecisionBatchItem) -> bool:
        """Apply an observation only while its original cycle is still current."""
        if item.cycle_state is None:
            return True
        fields = ("pending_low", "last_buy_candle_at", "sell_armed", "last_sell_price", "updated_at")
        values = {field: item.cycle_state[field] for field in fields}
        cycle = session.get(TradingCycleStateModel, item.automation_id)
        if cycle is None:
            session.add(TradingCycleStateModel(automation_id=item.automation_id, **values))
            return True
        expected = item.cycle_state.get("expected_state") or self._cycle_record(cycle).model_dump()
        marker = values["last_buy_candle_at"]
        marker_predicate = (
            TradingCycleStateModel.last_buy_candle_at.is_(None)
            if marker is None
            else or_(
                TradingCycleStateModel.last_buy_candle_at.is_(None),
                TradingCycleStateModel.last_buy_candle_at <= marker,
            )
        )
        # Column-bound comparisons use UTCDateTime's millisecond normalization.
        # SQL CAS also covers execution committed after the row was loaded above.
        result = session.execute(
            update(TradingCycleStateModel)
            .where(
                TradingCycleStateModel.automation_id == item.automation_id,
                TradingCycleStateModel.updated_at <= values["updated_at"],
                marker_predicate,
                *(getattr(TradingCycleStateModel, field) == expected[field] for field in fields),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def intent_history(self, automation_id: str) -> IntentHistory:
        with self._factory() as session:
            rows = session.scalars(
                select(LocalIntentModel)
                .where(
                    LocalIntentModel.automation_id == automation_id,
                    LocalIntentModel.state == "FILLED",
                )
                .order_by(LocalIntentModel.created_at, LocalIntentModel.idempotency_key)
            ).all()
            cycle_rows: list[LocalIntentModel] = []
            balance_lots = 0
            for row in rows:
                if row.executed_lots <= 0:
                    continue
                executed_lots = row.executed_lots
                if row.side == "BUY":
                    if balance_lots == 0:
                        cycle_rows = []
                    balance_lots += executed_lots
                else:
                    balance_lots = max(0, balance_lots - executed_lots)
                cycle_rows.append(row)
            buys = [row for row in cycle_rows if row.kind in {"OPEN", "BUY_MORE"}]
            return IntentHistory(
                last_buy_price=Decimal() if not buys else buys[-1].limit_price,
                completed_partial_sell_steps=sum(row.kind == "SELL_PART" for row in cycle_rows),
                actual_commissions=sum((row.executed_commission for row in cycle_rows), start=Decimal()),
                total_bought_lots=sum(row.executed_lots for row in buys),
                buy_commissions=sum((row.executed_commission for row in buys), start=Decimal()),
            )

    def create_trade_lot(
        self,
        *,
        automation_id: str,
        source_intent_id: str | None,
        source: str,
        quantity_lots: int,
        entry_price: Decimal,
        entry_commission: Decimal,
        opened_at: datetime,
    ) -> TradeLotRecord:
        with self._factory.begin() as session:
            if source_intent_id is not None:
                existing = session.scalar(
                    select(TradeLotModel).where(TradeLotModel.source_intent_id == source_intent_id)
                )
                if existing is not None:
                    intent = session.get(LocalIntentModel, source_intent_id)
                    return self._lot_record(existing, None if intent is None else intent.kind)
            model = TradeLotModel(
                id=str(uuid4()),
                automation_id=automation_id,
                source_intent_id=source_intent_id,
                source=source,
                original_lots=quantity_lots,
                remaining_lots=quantity_lots,
                entry_price=entry_price,
                entry_commission=entry_commission,
                opened_at=opened_at,
            )
            session.add(model)
            session.flush()
            intent_kind = None
            if source_intent_id is not None:
                intent = session.get(LocalIntentModel, source_intent_id)
                intent_kind = None if intent is None else intent.kind
            return self._lot_record(model, intent_kind)

    def list_open_lots(self, automation_id: str) -> list[TradeLotRecord]:
        with self._factory() as session:
            rows = session.execute(
                select(TradeLotModel, LocalIntentModel.kind)
                .outerjoin(
                    LocalIntentModel,
                    LocalIntentModel.idempotency_key == TradeLotModel.source_intent_id,
                )
                .where(
                    TradeLotModel.automation_id == automation_id,
                    TradeLotModel.remaining_lots > 0,
                )
                .order_by(TradeLotModel.opened_at.desc(), TradeLotModel.id.desc())
            ).all()
            return [self._lot_record(row, intent_kind) for row, intent_kind in rows]

    def realized_pnl(self, automation_id: str) -> Decimal:
        with self._factory() as session:
            value = session.scalar(
                select(func.coalesce(func.sum(LotAllocationModel.realized_pnl), 0)).where(
                    LotAllocationModel.automation_id == automation_id
                )
            )
            return Decimal(value or 0)

    def allocate_sell_lifo(
        self,
        *,
        automation_id: str,
        sell_intent_id: str,
        quantity_lots: int,
        exit_price: Decimal,
        exit_commission: Decimal,
        closed_at: datetime,
        lot_size: int,
    ) -> None:
        with self._factory.begin() as session:
            existing = session.scalar(
                select(LotAllocationModel).where(LotAllocationModel.sell_intent_id == sell_intent_id).limit(1)
            )
            if existing is not None:
                return
            lots = session.scalars(
                select(TradeLotModel)
                .where(
                    TradeLotModel.automation_id == automation_id,
                    TradeLotModel.remaining_lots > 0,
                )
                .order_by(TradeLotModel.opened_at.desc(), TradeLotModel.id.desc())
            ).all()
            if sum(row.remaining_lots for row in lots) < quantity_lots:
                raise ValueError("Sell execution exceeds the worker lot ledger.")
            remaining = quantity_lots
            for lot in lots:
                if remaining == 0:
                    break
                allocated = min(remaining, lot.remaining_lots)
                commission = exit_commission * Decimal(allocated) / Decimal(quantity_lots)
                entry_commission = lot.entry_commission * Decimal(allocated) / Decimal(lot.original_lots)
                pnl = (exit_price - lot.entry_price) * lot_size * allocated - entry_commission - commission
                session.add(
                    LotAllocationModel(
                        id=str(uuid4()),
                        automation_id=automation_id,
                        sell_intent_id=sell_intent_id,
                        lot_id=lot.id,
                        quantity_lots=allocated,
                        exit_price=exit_price,
                        exit_commission=commission,
                        realized_pnl=pnl,
                        closed_at=closed_at,
                    )
                )
                lot.remaining_lots -= allocated
                remaining -= allocated

    def get_cycle_state(self, automation_id: str, *, now: datetime) -> TradingCycleState:
        with self._factory.begin() as session:
            model = session.get(TradingCycleStateModel, automation_id)
            if model is None:
                model = TradingCycleStateModel(
                    automation_id=automation_id,
                    pending_low=None,
                    last_buy_candle_at=None,
                    sell_armed=True,
                    last_sell_price=None,
                    updated_at=now,
                )
                session.add(model)
                session.flush()
            return self._cycle_record(model)

    def save_cycle_state(self, state: TradingCycleState) -> TradingCycleState:
        with self._factory.begin() as session:
            model = session.get(TradingCycleStateModel, state.automation_id)
            if model is None:
                model = TradingCycleStateModel(automation_id=state.automation_id)
                session.add(model)
            model.pending_low = state.pending_low
            model.last_buy_candle_at = state.last_buy_candle_at
            model.sell_armed = state.sell_armed
            model.last_sell_price = state.last_sell_price
            model.updated_at = state.updated_at
            session.flush()
            return self._cycle_record(model)

    @staticmethod
    def _fact_outbox_record(model: FactOutboxModel) -> FactOutboxRecord:
        return FactOutboxRecord(
            event_id=model.event_id,
            user_broker_id=model.user_broker_id,
            automation_id=model.automation_id,
            sequence_number=model.sequence_number,
            expected_revision=model.expected_revision,
            fact_kind=model.fact_kind,
            payload=dict(model.payload),
            safe_message=model.safe_message,
            occurred_at=model.occurred_at,
            delivery_state=model.delivery_state,
            retry_count=model.retry_count,
            next_retry_at=model.next_retry_at,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _command(model: CachedAutomationModel) -> TypedAutomationCommand:
        if (
            model.broker_id is None
            or model.account_id is None
            or model.instrument_id is None
            or model.lot_size is None
            or model.min_price_increment is None
            or model.user_broker_id is None
            or model.fact_instrument_id is None
            or model.currency is None
        ):
            raise RuntimeError("Cached automation command is incomplete.")
        bootstrap = None
        if model.bootstrap_position_cycle_id is not None:
            required = (
                model.bootstrap_position_lot_id,
                model.bootstrap_quantity_lots,
                model.bootstrap_average_price,
                model.bootstrap_invested_amount,
                model.bootstrap_currency,
                model.bootstrap_observed_at,
            )
            if any(value is None for value in required):
                raise RuntimeError("Cached automation bootstrap snapshot is incomplete.")
            bootstrap = BrokerPositionBootstrap(
                position_cycle_id=UUID(model.bootstrap_position_cycle_id),
                position_lot_id=UUID(model.bootstrap_position_lot_id),
                quantity_lots=model.bootstrap_quantity_lots,
                average_price=model.bootstrap_average_price,
                invested_amount=model.bootstrap_invested_amount,
                currency=model.bootstrap_currency,
                observed_at=model.bootstrap_observed_at,
            )
        return TypedAutomationCommand(
            automation_id=UUID(model.automation_id),
            user_broker_id=UUID(model.user_broker_id),
            broker_id=UUID(model.broker_id),
            account_id=model.account_id,
            external_instrument_id=model.instrument_id,
            instrument_id=UUID(model.fact_instrument_id),
            currency=model.currency,
            lot_size=model.lot_size,
            min_price_increment=model.min_price_increment,
            state=AutomationState(model.state),
            revision=model.revision,
            last_sequence_number=model.last_sequence_number,
            resume_requested=model.resume_requested,
            bootstrap=bootstrap,
        )

    @staticmethod
    def _intent_record(model: LocalIntentModel) -> LocalIntentRecord:
        return LocalIntentRecord(
            idempotency_key=model.idempotency_key,
            automation_id=model.automation_id,
            kind=model.kind,
            side=model.side,
            state=model.state,
            quantity_lots=model.quantity_lots,
            limit_price=model.limit_price,
            broker_order_id=model.broker_order_id,
            requested_amount=model.requested_amount,
            executed_amount=model.executed_amount,
            estimated_commission=model.estimated_commission,
            executed_commission=model.executed_commission,
            executed_lots=model.executed_lots,
            executed_price=model.executed_price,
            execution_currency=model.execution_currency,
            executed_at=model.executed_at,
            dispatch_started_at=model.dispatch_started_at,
            broker_responded_at=model.broker_responded_at,
            terminal_at=model.terminal_at,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _commission_profile(
        model: AccountCommissionProfileModel,
    ) -> AccountCommissionProfile:
        return AccountCommissionProfile(
            key=AccountCommissionProfileKey(
                model.broker_id,
                model.account_id,
                model.instrument_type,
                model.currency,
            ),
            buy_rate=model.buy_rate,
            sell_rate=model.sell_rate,
            service_rate=model.service_rate,
            deal_rate=model.deal_rate,
            source=model.source,
            calculated_at=model.calculated_at,
            valid_until=model.valid_until,
        )

    @staticmethod
    def _decision_record(model: TradeDecisionModel) -> DecisionRecord:
        return DecisionRecord(
            id=model.id,
            process_id=model.process_id,
            automation_id=model.automation_id,
            reason_code=model.reason_code,
            decision=model.decision,
            intent_id=model.intent_id,
        )

    @staticmethod
    def _lot_record(model: TradeLotModel, source_intent_kind: str | None = None) -> TradeLotRecord:
        return TradeLotRecord(
            model.id,
            model.automation_id,
            model.source_intent_id,
            model.source,
            model.original_lots,
            model.remaining_lots,
            model.entry_price,
            model.entry_commission,
            model.opened_at,
            source_intent_kind,
        )

    @staticmethod
    def _cycle_record(model: TradingCycleStateModel) -> TradingCycleState:
        return TradingCycleState(
            model.automation_id,
            model.pending_low,
            model.last_buy_candle_at,
            model.sell_armed,
            model.last_sell_price,
            model.updated_at,
        )
