"""Transactional persistence for exact broker fee evidence and bounded scan cursors."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, case, func, select, update
from sqlalchemy.orm import Session, aliased, sessionmaker

from moex_sentinel.domain.execution_commissions import BrokerFeeObservation, BrokerFeeScanClaim
from moex_sentinel.storage.models.execution_commissions import (
    BrokerFeeScanStateModel,
    BrokerOrderFeeObservationModel,
)
from moex_sentinel.storage.models.order_facts import BrokerOrderModel, TradeExecutionModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from sentinel_contracts.broker_order_policy import BROKER_TERMINAL_STATES
from sentinel_contracts.time import floor_utc_millisecond

PAGE_LIMIT = 100
_TERMINAL = tuple(status.value for status in BROKER_TERMINAL_STATES)


class ExecutionCommissionRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def claim_next_page(self, now: datetime) -> BrokerFeeScanClaim | None:
        """Claim the least recently attempted active PROD account before its SDK call."""
        now = floor_utc_millisecond(now)
        with self._factory.begin() as session:
            rows = session.execute(
                select(UserBrokerModel, BrokerFeeScanStateModel)
                .outerjoin(BrokerFeeScanStateModel, BrokerFeeScanStateModel.user_broker_id == UserBrokerModel.id)
                .where(
                    UserBrokerModel.environment == "PROD",
                    UserBrokerModel.state == "ACTIVE",
                    UserBrokerModel.external_account_id.is_not(None),
                )
                .order_by(BrokerFeeScanStateModel.last_attempted_at.asc().nullsfirst(), UserBrokerModel.id)
                .with_for_update(of=UserBrokerModel, skip_locked=True)
            ).all()
            for broker, state in rows:
                if state is None:
                    state = BrokerFeeScanStateModel(user_broker_id=broker.id, status="SCANNING", seen_cursors=[])
                    session.add(state)
                    session.flush()
                if state.scan_pass_id is None or state.status == "COMPLETE":
                    earliest = session.scalar(
                        select(func.min(TradeExecutionModel.executed_at)).where(
                            TradeExecutionModel.user_broker_id == broker.id
                        )
                    )
                    if earliest is None:
                        continue
                    lower = floor_utc_millisecond(earliest.astimezone(UTC)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    state.scan_pass_id = str(uuid4())
                    state.lower_bound = lower
                    state.upper_bound = now
                    state.cursor = None
                    state.seen_cursors = []
                    state.completed_at = None
                state.claim_id = str(uuid4())
                state.status = "SCANNING"
                state.last_attempted_at = now
                state.last_error_code = None
                return BrokerFeeScanClaim(
                    user_broker_id=broker.id,
                    account_id=broker.external_account_id,
                    scan_pass_id=state.scan_pass_id,
                    claim_id=state.claim_id,
                    lower_bound=state.lower_bound,
                    upper_bound=state.upper_bound,
                    cursor=state.cursor,
                )
        return None

    def apply_page(
        self,
        claim: BrokerFeeScanClaim,
        observations: tuple[BrokerFeeObservation, ...],
        next_cursor: str | None,
        now: datetime,
    ) -> bool:
        """Atomically append fee candidates and advance/finalize only the current claimed cursor."""
        now = floor_utc_millisecond(now)
        with self._factory.begin() as session:
            state = session.scalar(
                select(BrokerFeeScanStateModel)
                .where(BrokerFeeScanStateModel.user_broker_id == claim.user_broker_id)
                .with_for_update()
            )
            if not self._claim_matches(state, claim):
                return False
            for observation in observations:
                if observation.user_broker_id != claim.user_broker_id or not (
                    claim.lower_bound <= observation.occurred_at <= claim.upper_bound
                ):
                    continue
                self._append_candidate(session, claim, observation, now)
            if next_cursor is not None:
                seen = list(state.seen_cursors or [])
                cursor_invalid = not next_cursor.strip() or len(next_cursor) > 1024
                cursor_repeated = next_cursor == claim.cursor or next_cursor in seen
                if cursor_invalid or cursor_repeated:
                    state.status = "ERROR"
                    state.last_error_code = "CURSOR_CYCLE"
                    state.claim_id = None
                    return True
                if claim.cursor is not None:
                    seen.append(claim.cursor)
                state.seen_cursors = seen
                state.cursor = next_cursor
            else:
                self._finalize_pass(session, claim, now)
                state.status = "COMPLETE"
                state.completed_at = now
                state.completed_scan_pass_id = claim.scan_pass_id
                state.cursor = None
                state.seen_cursors = []
            state.claim_id = None
            return True

    def fail_page(self, claim: BrokerFeeScanClaim, error_code: str) -> bool:
        """Leave the cursor retryable while releasing only this matching claim."""
        with self._factory.begin() as session:
            state = session.scalar(
                select(BrokerFeeScanStateModel)
                .where(BrokerFeeScanStateModel.user_broker_id == claim.user_broker_id)
                .with_for_update()
            )
            if not self._claim_matches(state, claim):
                return False
            state.status = "ERROR"
            state.last_error_code = error_code[:64]
            state.claim_id = None
            return True

    @staticmethod
    def _claim_matches(state: BrokerFeeScanStateModel | None, claim: BrokerFeeScanClaim) -> bool:
        return bool(
            state is not None
            and state.scan_pass_id == claim.scan_pass_id
            and state.claim_id == claim.claim_id
            and state.cursor == claim.cursor
        )

    @staticmethod
    def _append_candidate(
        session: Session,
        claim: BrokerFeeScanClaim,
        fee: BrokerFeeObservation,
        observed_at: datetime,
    ) -> None:
        if fee.account_id != claim.account_id:
            return
        order = session.scalar(
            select(BrokerOrderModel)
            .join(UserBrokerModel, UserBrokerModel.id == BrokerOrderModel.user_broker_id)
            .where(
                BrokerOrderModel.user_broker_id == claim.user_broker_id,
                BrokerOrderModel.external_order_id == fee.parent_operation_id,
                UserBrokerModel.environment == "PROD",
                UserBrokerModel.external_account_id == claim.account_id,
            )
        )
        if order is None:
            return
        instrument = session.get(BrokerInstrumentModel, order.instrument_id)
        execution_identity = (
            TradeExecutionModel.user_broker_id == order.user_broker_id,
            TradeExecutionModel.broker_order_id == order.id,
            TradeExecutionModel.automation_id == order.automation_id,
            TradeExecutionModel.instrument_id == order.instrument_id,
            TradeExecutionModel.side == order.side,
            TradeExecutionModel.currency == fee.currency,
            TradeExecutionModel.executed_lots > 0,
        )
        execution_totals = session.execute(
            select(
                func.count(TradeExecutionModel.id),
                func.sum(case((and_(*execution_identity), 1), else_=0)),
                func.sum(TradeExecutionModel.executed_lots),
                func.sum(TradeExecutionModel.value),
                func.sum(TradeExecutionModel.broker_commission),
            ).where(
                TradeExecutionModel.user_broker_id == order.user_broker_id,
                TradeExecutionModel.broker_order_id == order.id,
            )
        ).one()
        execution_count, matching_count, executed_lots, executed_value, execution_commission = execution_totals
        valid_parent = bool(
            order.state in _TERMINAL
            and order.terminal_at is not None
            and order.executed_amount > 0
            and execution_count > 0
            and matching_count == execution_count
            and executed_lots > 0
            and executed_value == order.executed_amount
            and execution_commission == order.executed_commission
            and instrument is not None
            and instrument.user_broker_id == order.user_broker_id
            and instrument.external_instrument_id == fee.instrument_id
            and instrument.currency.upper() == fee.currency
            and fee.amount >= order.executed_commission
        )
        existing = session.scalar(
            select(BrokerOrderFeeObservationModel).where(
                BrokerOrderFeeObservationModel.user_broker_id == fee.user_broker_id,
                BrokerOrderFeeObservationModel.parent_operation_id == fee.parent_operation_id,
                BrokerOrderFeeObservationModel.operation_type == "BROKER_FEE",
                BrokerOrderFeeObservationModel.currency == fee.currency,
                BrokerOrderFeeObservationModel.scan_pass_id == claim.scan_pass_id,
                BrokerOrderFeeObservationModel.amount == fee.amount,
            )
        )
        if existing is not None:
            return
        peers = list(
            session.scalars(
                select(BrokerOrderFeeObservationModel).where(
                    BrokerOrderFeeObservationModel.user_broker_id == fee.user_broker_id,
                    BrokerOrderFeeObservationModel.parent_operation_id == fee.parent_operation_id,
                    BrokerOrderFeeObservationModel.operation_type == "BROKER_FEE",
                    BrokerOrderFeeObservationModel.currency == fee.currency,
                    BrokerOrderFeeObservationModel.scan_pass_id == claim.scan_pass_id,
                )
            ).all()
        )
        status = "CANDIDATE" if valid_parent and not peers else "UNKNOWN"
        if peers:
            for peer in peers:
                peer.status = "UNKNOWN"
        session.add(
            BrokerOrderFeeObservationModel(
                user_broker_id=fee.user_broker_id,
                broker_order_id=order.id,
                parent_operation_id=fee.parent_operation_id,
                operation_type="BROKER_FEE",
                currency=fee.currency,
                scan_pass_id=claim.scan_pass_id,
                operation_id=fee.operation_id,
                amount=fee.amount,
                status=status,
                occurred_at=fee.occurred_at,
                observed_at=observed_at,
            )
        )

    @staticmethod
    def _finalize_pass(session: Session, claim: BrokerFeeScanClaim, now: datetime) -> None:
        observation = BrokerOrderFeeObservationModel
        peer = aliased(BrokerOrderFeeObservationModel)
        prior = aliased(BrokerOrderFeeObservationModel)
        peer_identity = (
            peer.user_broker_id == observation.user_broker_id,
            peer.parent_operation_id == observation.parent_operation_id,
            peer.operation_type == observation.operation_type,
            peer.currency == observation.currency,
            peer.scan_pass_id == claim.scan_pass_id,
        )
        distinct_amounts = (
            select(func.count(func.distinct(peer.amount))).where(*peer_identity).correlate(observation).scalar_subquery()
        )
        unknown_peers = (
            select(func.count(peer.id))
            .where(*peer_identity, peer.status == "UNKNOWN")
            .correlate(observation)
            .scalar_subquery()
        )
        previous_known = (
            select(func.max(prior.amount))
            .where(
                prior.user_broker_id == observation.user_broker_id,
                prior.parent_operation_id == observation.parent_operation_id,
                prior.operation_type == observation.operation_type,
                prior.currency == observation.currency,
                prior.scan_pass_id != claim.scan_pass_id,
                prior.status == "KNOWN",
            )
            .correlate(observation)
            .scalar_subquery()
        )
        order_commission = (
            select(BrokerOrderModel.executed_commission)
            .where(
                BrokerOrderModel.user_broker_id == observation.user_broker_id,
                BrokerOrderModel.id == observation.broker_order_id,
            )
            .correlate(observation)
            .scalar_subquery()
        )
        floor = case(
            (previous_known.is_(None), order_commission),
            (previous_known > order_commission, previous_known),
            else_=order_commission,
        )
        eligible = and_(
            distinct_amounts == 1,
            unknown_peers == 0,
            floor.is_not(None),
            observation.amount >= floor,
        )
        session.execute(
            update(observation)
            .where(
                observation.user_broker_id == claim.user_broker_id,
                observation.scan_pass_id == claim.scan_pass_id,
            )
            .values(status=case((eligible, "KNOWN"), else_="UNKNOWN"))
        )
