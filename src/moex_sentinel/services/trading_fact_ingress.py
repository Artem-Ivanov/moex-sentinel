"""Transactional per-automation ingress for typed Worker facts."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from moex_sentinel.domain.trading_facts import (
    AutomationEnvelopeDraft,
    BrokerOrderStatus,
    TradingAutomationDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.services.trading_fact_mapping import TradingFactMapper
from moex_sentinel.services.trading_fact_ports import (
    FactRuntimeAuthorization,
    FactSubmitReservation,
    TradingFactsUnitOfWorkPort,
)
from sentinel_contracts.automation_lifecycle import (
    InvalidAutomationTransition,
    TransitionOrigin,
    validate_automation_transition,
)
from sentinel_contracts.time import utc_now_ms
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import (
    AutomationStateChangedEnvelope,
    BrokerOrderStateChangedEnvelope,
    FactBatchResult,
    FactEnvelope,
    FactGroupAcknowledgement,
    FactGroupFailure,
    FactIngressErrorCode,
    FactPositionCycleState,
    PositionCycleUpdatedEnvelope,
    PositionLotOpenedEnvelope,
    PositionLotSource,
    TradeAuditRecordedEnvelope,
)

PERSISTENCE_ERROR_CODES = {
    TradingFactErrorCode.CROSS_SCOPE: FactIngressErrorCode.CROSS_SCOPE_RELATION,
    TradingFactErrorCode.FACT_ID_CONFLICT: FactIngressErrorCode.FACT_ID_CONFLICT,
    TradingFactErrorCode.ORDER_IDEMPOTENCY_CONFLICT: FactIngressErrorCode.FACT_LINEAGE_CONFLICT,
    TradingFactErrorCode.SEQUENCE_CONFLICT: FactIngressErrorCode.AUTOMATION_SEQUENCE_CONFLICT,
    TradingFactErrorCode.REVISION_CONFLICT: FactIngressErrorCode.AUTOMATION_REVISION_CONFLICT,
    TradingFactErrorCode.INVALID_STATE: FactIngressErrorCode.INVALID_FACT_STATE,
}


class _FactGroupRejected(RuntimeError):
    def __init__(self, code: FactIngressErrorCode, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code.value)


class TradingFactIngressService:
    def __init__(
        self,
        uow_factory: Callable[[], TradingFactsUnitOfWorkPort],
        mapper: TradingFactMapper,
        *,
        now: Callable[[], datetime] = utc_now_ms,
        environment: str | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._mapper = mapper
        self._now = now
        self._environment = environment

    def publish(
        self,
        facts: list[FactEnvelope],
        *,
        runtime_instance_id: UUID | None = None,
        generation: int | None = None,
    ) -> FactBatchResult:
        groups: dict[UUID, list[FactEnvelope]] = {}
        for fact in facts:
            groups.setdefault(fact.automation_id, []).append(fact)
        acknowledgements: list[FactGroupAcknowledgement] = []
        failures: list[FactGroupFailure] = []
        for automation_id, group in groups.items():
            ordered = sorted(group, key=lambda item: (item.sequence_number, str(item.event_id)))
            try:
                acknowledgement = self._publish_group(
                    automation_id,
                    ordered,
                    runtime_instance_id=runtime_instance_id,
                    generation=generation,
                )
            except _FactGroupRejected as error:
                failures.append(self._failure(automation_id, ordered, error.code, retryable=error.retryable))
            except TradingFactPersistenceError as error:
                code = PERSISTENCE_ERROR_CODES.get(error.code, FactIngressErrorCode.FACT_LINEAGE_CONFLICT)
                failures.append(self._failure(automation_id, ordered, code, retryable=False))
            except Exception:
                failures.append(
                    self._failure(
                        automation_id,
                        ordered,
                        FactIngressErrorCode.TEMPORARY_CORE_FAILURE,
                        retryable=True,
                    )
                )
            else:
                acknowledgements.append(acknowledgement)
        return FactBatchResult(results=tuple(acknowledgements), failures=tuple(failures))

    def _publish_group(
        self,
        automation_id: UUID,
        facts: list[FactEnvelope],
        *,
        runtime_instance_id: UUID | None,
        generation: int | None,
    ) -> FactGroupAcknowledgement:
        if len({fact.user_broker_id for fact in facts}) != 1:
            raise _FactGroupRejected(FactIngressErrorCode.CROSS_SCOPE_RELATION)
        scope_id = str(facts[0].user_broker_id)
        accepted: list[UUID] = []
        with self._uow_factory() as uow:
            if self._environment is not None and not uow.scope_matches_environment(scope_id, self._environment):
                raise _FactGroupRejected(FactIngressErrorCode.CROSS_SCOPE_RELATION)
            runtime_authorization = None
            if self._environment == "PROD":
                if runtime_instance_id is None or generation is None:
                    raise _FactGroupRejected(FactIngressErrorCode.TEMPORARY_CORE_FAILURE, retryable=True)
                runtime_authorization = uow.authorize_fact_runtime(
                    scope_id,
                    str(runtime_instance_id),
                    generation,
                    environment=self._environment,
                )
                if runtime_authorization is None:
                    raise _FactGroupRejected(FactIngressErrorCode.TEMPORARY_CORE_FAILURE, retryable=True)
            try:
                current = uow.automations.get(scope_id, str(automation_id))
            except TradingFactPersistenceError as error:
                if error.code is TradingFactErrorCode.NOT_FOUND:
                    raise _FactGroupRejected(FactIngressErrorCode.AUTOMATION_NOT_FOUND) from error
                raise
            bootstrap = current.bootstrap_position_cycle_id is not None and current.last_sequence_number == 0
            if bootstrap:
                self._validate_initial_bootstrap(current, facts)
            for fact in facts:
                incoming = self._envelope_draft(fact)
                matches = uow.audit.find_envelope_matches(
                    scope_id,
                    incoming.event_id,
                    str(automation_id),
                    fact.sequence_number,
                )
                by_event = next((value for value in matches if value.event_id == incoming.event_id), None)
                if by_event is not None:
                    if self._same_envelope(by_event, incoming):
                        accepted.append(fact.event_id)
                        continue
                    raise _FactGroupRejected(FactIngressErrorCode.FACT_ID_CONFLICT)
                if any(
                    value.automation_id == incoming.automation_id and value.sequence_number == fact.sequence_number
                    for value in matches
                ):
                    raise _FactGroupRejected(FactIngressErrorCode.AUTOMATION_SEQUENCE_CONFLICT)
                if fact.sequence_number <= current.last_sequence_number:
                    raise _FactGroupRejected(FactIngressErrorCode.AUTOMATION_SEQUENCE_CONFLICT)
                if fact.sequence_number != current.last_sequence_number + 1:
                    raise _FactGroupRejected(FactIngressErrorCode.AUTOMATION_SEQUENCE_GAP)
                if fact.expected_revision != current.revision:
                    raise _FactGroupRejected(FactIngressErrorCode.AUTOMATION_REVISION_CONFLICT)
                if isinstance(fact, AutomationStateChangedEnvelope):
                    try:
                        changed = validate_automation_transition(
                            current.state,
                            fact.payload.state,
                            origin=TransitionOrigin.WORKER_FACT,
                            bootstrap=bootstrap,
                        )
                    except InvalidAutomationTransition as error:
                        raise _FactGroupRejected(FactIngressErrorCode.INVALID_FACT_STATE) from error
                    if not changed:
                        raise _FactGroupRejected(FactIngressErrorCode.INVALID_FACT_STATE)
                    self._validate_state_metadata(current, fact)
                reservation_key = self._validate_pre_rpc_abort(
                    uow,
                    fact,
                    scope_id=scope_id,
                    automation_id=automation_id,
                    runtime_instance_id=runtime_instance_id,
                    generation=generation,
                    runtime_authorization=runtime_authorization,
                    automation_instrument_id=current.instrument_id,
                )
                self._mapper.apply(uow, fact)
                if reservation_key is not None and not uow.confirm_submit_not_sent(
                    scope_id,
                    reservation_key,
                    now=self._now(),
                ):
                    raise _FactGroupRejected(FactIngressErrorCode.INVALID_FACT_STATE)
                uow.audit.append_envelope(scope_id, incoming)
                if isinstance(fact, AutomationStateChangedEnvelope):
                    current = uow.automations.accept_state_fact(
                        scope_id,
                        str(automation_id),
                        expected_revision=current.revision,
                        expected_sequence=current.last_sequence_number,
                        sequence_number=fact.sequence_number,
                        state=fact.payload.state,
                        suspended_from_state=fact.payload.suspended_from_state,
                        hold_reason=fact.payload.hold_reason,
                        closed_at=fact.payload.closed_at,
                    )
                else:
                    current = uow.automations.accept_supporting_fact(
                        scope_id,
                        str(automation_id),
                        expected_revision=current.revision,
                        expected_sequence=current.last_sequence_number,
                        sequence_number=fact.sequence_number,
                    )
                accepted.append(fact.event_id)
        return FactGroupAcknowledgement(
            automation_id=automation_id,
            accepted_through_sequence=current.last_sequence_number,
            current_revision=current.revision,
            accepted_event_ids=tuple(accepted),
        )

    @classmethod
    def _validate_pre_rpc_abort(
        cls,
        uow: TradingFactsUnitOfWorkPort,
        fact: FactEnvelope,
        *,
        scope_id: str,
        automation_id: UUID,
        runtime_instance_id: UUID | None,
        generation: int | None,
        runtime_authorization: FactRuntimeAuthorization | None,
        automation_instrument_id: str,
    ) -> str | None:
        if not isinstance(fact, BrokerOrderStateChangedEnvelope):
            return None
        payload = fact.payload
        proof = payload.pre_rpc_abort_proof
        if proof is None:
            return None
        reservation = uow.get_fact_submit_reservation(scope_id, str(proof.idempotency_key))
        if not cls._proof_matches_reservation(
            proof,
            payload,
            reservation,
            automation_id=str(automation_id),
            runtime_instance_id=runtime_instance_id,
            generation=generation,
            runtime_authorization=runtime_authorization,
        ):
            raise _FactGroupRejected(FactIngressErrorCode.INVALID_FACT_STATE)
        order = uow.orders.get_order(scope_id, str(payload.broker_order_id))
        if (
            order.user_broker_id != scope_id
            or order.automation_id != str(automation_id)
            or order.instrument_id != automation_instrument_id
            or order.instrument_id != str(payload.instrument_id)
            or order.idempotency_key != reservation.idempotency_key
            or order.side.value != reservation.side
            or payload.side.value != reservation.side
            or order.quantity_lots != reservation.quantity_lots
            or payload.quantity_lots != reservation.quantity_lots
            or order.limit_price != reservation.limit_price
            or payload.limit_price != reservation.limit_price
            or order.requested_amount != reservation.requested_amount
            or payload.requested_amount != reservation.requested_amount
            or order.order_type.value != "LIMIT"
            or order.state not in {BrokerOrderStatus.DISPATCH_PENDING, BrokerOrderStatus.SUBMITTING}
            or payload.from_state is None
            or payload.from_state.value != order.state.value
            or order.external_order_id is not None
            or order.broker_responded_at is not None
            or order.executed_at is not None
            or order.terminal_at is not None
            or order.executed_amount != 0
            or order.executed_commission != 0
            or uow.orders.list_executions(scope_id, order.id)
        ):
            raise _FactGroupRejected(FactIngressErrorCode.INVALID_FACT_STATE)
        return reservation.idempotency_key

    @staticmethod
    def _proof_matches_reservation(
        proof,
        payload,
        reservation: FactSubmitReservation | None,
        *,
        automation_id: str,
        runtime_instance_id: UUID | None,
        generation: int | None,
        runtime_authorization: FactRuntimeAuthorization | None,
    ) -> bool:
        if (
            reservation is None
            or runtime_instance_id is None
            or generation is None
            or runtime_authorization is None
            or reservation.automation_id != automation_id
            or reservation.idempotency_key != str(proof.idempotency_key)
            or reservation.permit_id != str(proof.permit_id)
            or reservation.request_fingerprint != proof.request_fingerprint
            or reservation.runtime_instance_id != str(proof.runtime_instance_id)
            or reservation.generation != proof.generation
            or reservation.configuration_fingerprint != proof.configuration_fingerprint
            or reservation.state not in {"IN_FLIGHT", "UNKNOWN"}
            or payload.idempotency_key != reservation.idempotency_key
            or payload.quantity_lots != reservation.quantity_lots
            or payload.requested_amount != reservation.requested_amount
            or payload.state.value != "CANCELLED"
            or payload.to_state.value != "CANCELLED"
            or payload.external_order_id is not None
            or payload.broker_responded_at is not None
            or payload.executed_amount != 0
            or payload.executed_commission != 0
            or payload.executed_at is not None
            or not TradingFactIngressService._reservation_has_no_attributable_fill(reservation)
        ):
            return False
        origin_is_current = (
            reservation.runtime_instance_id == str(runtime_instance_id) and reservation.generation == generation
        )
        origin_is_retired = reservation.origin_registration_status == "RETIRED"
        if origin_is_retired:
            return runtime_authorization.recovery_only
        return origin_is_current and reservation.origin_registration_status == "OWNER"

    @staticmethod
    def _reservation_has_no_attributable_fill(reservation: FactSubmitReservation) -> bool:
        payload = reservation.payload
        executed_lots = payload.get("executed_lots")
        anchor_lots = payload.get("anchor_executed_lots")
        if (
            not isinstance(executed_lots, int)
            or isinstance(executed_lots, bool)
            or executed_lots != 0
            or not isinstance(anchor_lots, int)
            or isinstance(anchor_lots, bool)
            or anchor_lots != 0
            or payload.get("broker_order_id") not in {None, ""}
            or payload.get("last_reconciled_order_state") is not None
        ):
            return False
        for field in ("executed_amount", "executed_commission"):
            try:
                amount = Decimal(str(payload[field]))
            except (KeyError, InvalidOperation):
                return False
            if not amount.is_finite() or amount != 0:
                return False
        return True

    @staticmethod
    def _validate_state_metadata(current: TradingAutomationDraft, fact: AutomationStateChangedEnvelope) -> None:
        payload = fact.payload
        if (payload.state is AutomationState.CLOSED) != (payload.closed_at is not None):
            raise _FactGroupRejected(FactIngressErrorCode.INVALID_FACT_STATE)
        if payload.state is AutomationState.HOLD:
            if (
                payload.suspended_from_state is not current.state
                or not payload.hold_reason
                or not payload.hold_reason.strip()
            ):
                raise _FactGroupRejected(FactIngressErrorCode.INVALID_FACT_STATE)
        elif payload.suspended_from_state is not None or payload.hold_reason is not None:
            raise _FactGroupRejected(FactIngressErrorCode.INVALID_FACT_STATE)

    @staticmethod
    def _validate_initial_bootstrap(current: TradingAutomationDraft, facts: list[FactEnvelope]) -> None:
        """Accept the authoritative opening position and activation in one transaction."""
        if len(facts) != 4:
            raise _FactGroupRejected(FactIngressErrorCode.INVALID_FACT_STATE)
        cycle, lot, audit, activation = facts
        if not (
            isinstance(cycle, PositionCycleUpdatedEnvelope)
            and isinstance(lot, PositionLotOpenedEnvelope)
            and isinstance(audit, TradeAuditRecordedEnvelope)
            and isinstance(activation, AutomationStateChangedEnvelope)
        ):
            raise _FactGroupRejected(FactIngressErrorCode.INVALID_FACT_STATE)
        if not (
            current.state in {AutomationState.HOLD, AutomationState.IN_QUEUE}
            and str(cycle.payload.position_cycle_id) == current.bootstrap_position_cycle_id
            and str(cycle.payload.instrument_id) == current.instrument_id
            and cycle.payload.state is FactPositionCycleState.OPEN
            and cycle.payload.quantity_lots == current.bootstrap_quantity_lots
            and cycle.payload.average_entry_price == current.bootstrap_average_price
            and cycle.payload.invested_amount == current.bootstrap_invested_amount
            and cycle.payload.realized_pnl == cycle.payload.unrealized_pnl == cycle.payload.net_pnl == 0
            and cycle.payload.accumulated_commissions == 0
            and (
                cycle.payload.opened_at
                == cycle.payload.created_at
                == cycle.payload.updated_at
                == current.bootstrap_observed_at
            )
            and cycle.payload.closed_at is None
            and str(lot.payload.position_cycle_id) == current.bootstrap_position_cycle_id
            and str(lot.payload.position_lot_id) == current.bootstrap_position_lot_id
            and lot.payload.source is PositionLotSource.BROKER_POSITION_BOOTSTRAP
            and lot.payload.buy_execution_id is None
            and lot.payload.original_lots == lot.payload.remaining_lots == current.bootstrap_quantity_lots
            and lot.payload.entry_price == current.bootstrap_average_price
            and lot.payload.entry_commission == 0
            and (
                lot.payload.opened_at
                == lot.payload.created_at
                == lot.payload.updated_at
                == current.bootstrap_observed_at
            )
            and audit.payload.stage == "BOOTSTRAP_POSITION_ADOPTED"
            and str(audit.payload.instrument_id) == current.instrument_id
            and audit.payload.decision_id is audit.payload.broker_order_id is audit.payload.execution_id is None
            and activation.payload.state is AutomationState.IN_WORK
            and (
                activation.payload.suspended_from_state
                is activation.payload.hold_reason
                is activation.payload.closed_at
                is None
            )
        ):
            raise _FactGroupRejected(FactIngressErrorCode.INVALID_FACT_STATE)

    def _envelope_draft(self, envelope: FactEnvelope) -> AutomationEnvelopeDraft:
        return AutomationEnvelopeDraft(
            event_id=str(envelope.event_id),
            automation_id=str(envelope.automation_id),
            user_broker_id=str(envelope.user_broker_id),
            sequence_number=envelope.sequence_number,
            expected_revision=envelope.expected_revision,
            fact_kind=envelope.fact_kind.value,
            safe_message=envelope.safe_message,
            payload=envelope.payload.model_dump(mode="json"),
            occurred_at=envelope.occurred_at,
            received_at=self._now(),
        )

    @staticmethod
    def _same_envelope(stored: AutomationEnvelopeDraft, incoming: AutomationEnvelopeDraft) -> bool:
        return stored.model_copy(update={"received_at": incoming.received_at}) == incoming

    @staticmethod
    def _failure(
        automation_id: UUID,
        facts: list[FactEnvelope],
        code: FactIngressErrorCode,
        *,
        retryable: bool,
    ) -> FactGroupFailure:
        return FactGroupFailure(
            automation_id=automation_id,
            code=code,
            event_ids=tuple(fact.event_id for fact in facts),
            sequence_numbers=tuple(fact.sequence_number for fact in facts),
            retryable=retryable,
        )
