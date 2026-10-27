"""Session-scoped persistence for worker ownership and admission state."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from hmac import compare_digest
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.trading_admission import (
    AdmissionConflictError,
    AdmissionInstrument,
    AdmissionOrderState,
    broker_configuration_fingerprint,
)
from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
from moex_sentinel.storage.models.order_facts import BrokerOrderEventModel, BrokerOrderModel
from moex_sentinel.storage.models.position_facts import PositionLotModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.models.trading_admission import (
    TradingAccountAdmissionModel,
    TradingAdmissionBaselineModel,
    TradingAdmissionEventModel,
    TradingReconciliationModel,
    TradingSubmitReservationModel,
    WorkerRuntimeInstanceModel,
)
from moex_sentinel.storage.models.trading_observability import AutomationEventModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from sentinel_contracts.time import is_historical, require_utc_millisecond
from sentinel_contracts.trading_admission import (
    AdmissionBaseline,
    AdmissionLimits,
    AdmissionStatus,
    AdmissionView,
    ArmRequest,
    OwnerStoppedRequest,
    PendingRuntimeCandidate,
    ReconciliationPositionAnchor,
    ReconciliationResult,
    ReconciliationStatus,
    RevokeRequest,
    RuntimeRegistrationRequest,
    RuntimeRegistrationResult,
    RuntimeRegistrationStatus,
    SubmitAbsenceBasis,
    SubmitAttemptView,
    SubmitAuthorizationRequest,
    SubmitAuthorizationResult,
    SubmitDenialReason,
    SubmitDenied,
    SubmitPermit,
    SubmitReservationState,
    UpdateLimitsRequest,
    submit_request_fingerprint,
)

WORKER_UNCERTAIN_HOLD_REASON = "Uncertain broker intent could not be reconciled unambiguously"


@dataclass(frozen=True)
class WorkerRuntimeCredentialRecord:
    runtime_instance_id: str
    user_broker_id: str
    worker_id: str
    environment: str
    access_mode: str
    strategy_enabled: bool
    secret_digest: str
    registration_status: str
    generation: int | None
    lease_expires_at: datetime | None
    recovery_only: bool


@dataclass(frozen=True)
class AdmissionReservationRecord:
    idempotency_key: str
    state: str
    side: str
    instrument_uid: str
    requested_amount: Decimal
    reserved_cash: Decimal
    reserved_position_units: Decimal
    quantity_lots: int
    runtime_instance_id: str
    generation: int
    anchor_reconciliation_id: str | None
    anchor_executed_lots: int
    executed_lots: int
    executed_amount: Decimal
    executed_commission: Decimal


@dataclass(frozen=True)
class AdmissionSubmitContext:
    account_id: str
    environment: str
    revision: int
    owner_runtime_instance_id: str | None
    owner_generation: int | None
    lease_expires_at: datetime | None
    arm_revision: int | None
    arm_expires_at: datetime | None
    admission_status: str
    recovery_only: bool
    limits: AdmissionLimits | None
    configuration_fingerprint: str | None
    baseline: AdmissionBaseline | None
    reconciliation: ReconciliationResult | None
    automation_id: str
    automation_state: str
    instrument_uid: str
    instrument_type: str
    currency: str
    lot_size: int
    min_price_increment: Decimal
    instrument_active: bool
    api_trade_available: bool
    owner_access_mode: str | None
    owner_strategy_enabled: bool
    reservations: tuple[AdmissionReservationRecord, ...]
    reservation_digest: str


class TradingAdmissionRepository:
    def __init__(self, factory: sessionmaker[Session], *, application_environment: str | None = None) -> None:
        if application_environment is not None and application_environment not in {"TEST", "PROD"}:
            raise ValueError("Unsupported application environment.")
        self._factory = factory
        self._application_environment = application_environment

    def _environment_matches(self, broker: UserBrokerModel | None) -> bool:
        return broker is not None and (
            self._application_environment is None or broker.environment == self._application_environment
        )

    @staticmethod
    def _current_configuration_fingerprint(broker: UserBrokerModel, access_mode: str) -> str:
        return broker_configuration_fingerprint(
            account_id=broker.external_account_id or "",
            access_mode=access_mode,
            adapter_code=broker.api_slug,
            broker_id=broker.id,
            environment=broker.environment,
            target=broker.fqdn,
            token=str(broker.settings.get("token", "")),
        )

    def register_runtime(
        self,
        request: RuntimeRegistrationRequest,
        *,
        secret_digest: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> RuntimeRegistrationResult:
        with self._factory.begin() as session:
            broker = session.scalar(
                select(UserBrokerModel).where(UserBrokerModel.id == str(request.broker_id)).with_for_update()
            )
            if (
                broker is None
                or broker.archived_at is not None
                or broker.state != "ACTIVE"
                or broker.external_account_id is None
                or broker.environment != request.application_environment
                or not self._environment_matches(broker)
            ):
                raise ValueError("Configured worker broker scope is unavailable.")

            runtime_id = str(request.runtime_instance_id)
            runtime = session.get(WorkerRuntimeInstanceModel, runtime_id)
            admission = session.get(TradingAccountAdmissionModel, str(request.broker_id), with_for_update=True)
            if runtime is not None:
                if (
                    runtime.user_broker_id != str(request.broker_id)
                    or runtime.worker_id != request.worker_id
                    or runtime.environment != request.application_environment
                    or runtime.access_mode != request.access_mode
                    or runtime.strategy_enabled != request.strategy_enabled
                    or not compare_digest(runtime.secret_digest, secret_digest)
                ):
                    raise ValueError("Runtime instance identity is already bound to different credentials or scope.")
                return self._registration_result(runtime, admission)

            first_owner = admission is None or admission.owner_runtime_instance_id is None
            is_owner = first_owner and request.access_mode == "READ_ONLY"
            status = RuntimeRegistrationStatus.OWNER if is_owner else RuntimeRegistrationStatus.PENDING
            generation = 1 if is_owner else None
            runtime = WorkerRuntimeInstanceModel(
                runtime_instance_id=runtime_id,
                user_broker_id=str(request.broker_id),
                worker_id=request.worker_id,
                environment=request.application_environment,
                access_mode=request.access_mode,
                strategy_enabled=request.strategy_enabled,
                secret_digest=secret_digest,
                registration_status=status.value,
                generation=generation,
                registered_at=now,
                last_seen_at=now,
                lease_expires_at=lease_expires_at if is_owner else None,
            )
            session.add(runtime)
            session.flush()

            if admission is None:
                admission = TradingAccountAdmissionModel(
                    user_broker_id=str(request.broker_id),
                    revision=1,
                    owner_runtime_instance_id=runtime_id if is_owner else None,
                    owner_generation=generation,
                    admission_status=AdmissionStatus.DISARMED.value,
                    recovery_only=False,
                    lease_expires_at=lease_expires_at if is_owner else None,
                )
                session.add(admission)
            elif is_owner:
                admission.owner_runtime_instance_id = runtime_id
                admission.owner_generation = generation
                admission.lease_expires_at = lease_expires_at

            session.add(
                TradingAdmissionEventModel(
                    event_id=str(uuid4()),
                    user_broker_id=str(request.broker_id),
                    runtime_instance_id=runtime_id,
                    generation=generation,
                    event_type="RUNTIME_REGISTERED",
                    occurred_at=now,
                    payload={"registration_status": status.value},
                )
            )
            session.flush()
            return self._registration_result(runtime, admission)

    def pending_runtime_candidate(
        self, broker_id: str, worker_id: str, registered_after: datetime
    ) -> PendingRuntimeCandidate | None:
        with self._factory() as session:
            broker = session.get(UserBrokerModel, broker_id)
            if (
                broker is None
                or broker.archived_at is not None
                or broker.state != "ACTIVE"
                or broker.external_account_id is None
                or not self._environment_matches(broker)
            ):
                raise ValueError("Pending runtime scope is unavailable.")
            candidates = session.scalars(
                select(WorkerRuntimeInstanceModel)
                .where(
                    WorkerRuntimeInstanceModel.user_broker_id == broker_id,
                    WorkerRuntimeInstanceModel.worker_id == worker_id,
                    WorkerRuntimeInstanceModel.registration_status == RuntimeRegistrationStatus.PENDING.value,
                    WorkerRuntimeInstanceModel.registered_at > registered_after,
                )
                .order_by(WorkerRuntimeInstanceModel.registered_at, WorkerRuntimeInstanceModel.runtime_instance_id)
                .limit(2)
            ).all()
        if len(candidates) > 1:
            raise AdmissionConflictError("Multiple pending runtime candidates match the requested scope.")
        if not candidates:
            return None
        runtime = candidates[0]
        return PendingRuntimeCandidate(
            broker_id=UUID(runtime.user_broker_id),
            worker_id=runtime.worker_id,
            runtime_instance_id=UUID(runtime.runtime_instance_id),
            environment=runtime.environment,
            access_mode=runtime.access_mode,
            strategy_enabled=runtime.strategy_enabled,
            registration_status="PENDING",
            registered_at=runtime.registered_at,
        )

    def expects_trading(self, broker_id: str) -> bool:
        with self._factory() as session:
            broker = session.get(UserBrokerModel, broker_id)
            admission = session.get(TradingAccountAdmissionModel, broker_id)
            if (
                broker is None
                or not self._environment_matches(broker)
                or broker.archived_at is not None
                or broker.state != "ACTIVE"
                or broker.external_account_id is None
                or admission is None
                or admission.owner_runtime_instance_id is None
                or admission.owner_generation is None
            ):
                return False
            owner = session.get(WorkerRuntimeInstanceModel, admission.owner_runtime_instance_id)
            if (
                owner is None
                or owner.user_broker_id != broker_id
                or owner.environment != broker.environment
                or owner.registration_status != RuntimeRegistrationStatus.OWNER.value
                or owner.generation != admission.owner_generation
                or owner.access_mode != "TRADE"
                or not owner.strategy_enabled
            ):
                return False
            return bool(
                session.scalar(
                    select(func.count())
                    .select_from(TradingAutomationModel)
                    .where(
                        TradingAutomationModel.user_broker_id == broker_id,
                        TradingAutomationModel.state == "IN_WORK",
                        TradingAutomationModel.closed_at.is_(None),
                    )
                )
            )

    @staticmethod
    def _has_age_policy_candidate(session: Session, broker_id: str) -> bool:
        reservations = session.scalars(
            select(TradingSubmitReservationModel).where(
                TradingSubmitReservationModel.user_broker_id == broker_id,
                TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN")),
            )
        )
        return any(
            isinstance((policy := reservation.payload.get("_age_policy")), dict)
            and type(policy.get("version")) is int
            and policy["version"] == 1
            and isinstance(policy.get("expired_at"), str)
            for reservation in reservations
        )

    @staticmethod
    def _has_worker_technical_hold(session: Session, broker_id: str) -> bool:
        automations = session.scalars(
            select(TradingAutomationModel).where(
                TradingAutomationModel.user_broker_id == broker_id,
                TradingAutomationModel.state == "HOLD",
                TradingAutomationModel.suspended_from_state == "IN_WORK",
                TradingAutomationModel.hold_reason == WORKER_UNCERTAIN_HOLD_REASON,
                TradingAutomationModel.closed_at.is_(None),
            )
        )
        for automation in automations:
            event = session.scalar(
                select(AutomationEventModel)
                .where(
                    AutomationEventModel.user_broker_id == broker_id,
                    AutomationEventModel.automation_id == automation.id,
                    AutomationEventModel.fact_kind == "AUTOMATION_STATE_CHANGED",
                )
                .order_by(AutomationEventModel.sequence_number.desc())
                .limit(1)
            )
            if event is not None and (
                event.payload.get("state") == "HOLD"
                and event.payload.get("suspended_from_state") == "IN_WORK"
                and event.payload.get("hold_reason") == automation.hold_reason
                and event.expected_revision + 1 == automation.revision
            ):
                return True
        return False

    def age_policy_candidate_ids(self, broker_id: str) -> frozenset[str]:
        with self._factory() as session:
            return frozenset(
                reservation.idempotency_key
                for reservation in session.scalars(
                    select(TradingSubmitReservationModel).where(
                        TradingSubmitReservationModel.user_broker_id == broker_id,
                        TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN")),
                    )
                )
                if isinstance((policy := reservation.payload.get("_age_policy")), dict)
                and type(policy.get("version")) is int
                and policy["version"] == 1
                and isinstance(policy.get("expired_at"), str)
            )

    def age_policy_candidate_anchor_lots(self, broker_id: str) -> tuple[tuple[str, int], ...] | None:
        """Return Core-known lots for unresolved age-policy UIDs; None means untrusted/missing metadata."""
        with self._factory() as session:
            reservations = session.scalars(
                select(TradingSubmitReservationModel).where(
                    TradingSubmitReservationModel.user_broker_id == broker_id,
                    TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN")),
                )
            ).all()
            candidates = [
                item
                for item in reservations
                if isinstance((policy := item.payload.get("_age_policy")), dict)
                and type(policy.get("version")) is int
                and policy["version"] == 1
                and isinstance(policy.get("expired_at"), str)
            ]
            if not candidates:
                return ()
            result: dict[str, int] = {}
            for reservation in candidates:
                instrument = session.scalar(
                    select(BrokerInstrumentModel).where(
                        BrokerInstrumentModel.user_broker_id == broker_id,
                        BrokerInstrumentModel.external_instrument_id == reservation.instrument_uid,
                    )
                )
                if instrument is None or type(instrument.lot_size) is not int or instrument.lot_size <= 0:
                    return None
                prior = result.setdefault(reservation.instrument_uid, instrument.lot_size)
                if prior != instrument.lot_size:
                    return None
            return tuple(sorted(result.items()))

    def needs_age_policy_recovery(self, broker_id: str) -> bool:
        with self._factory() as session:
            return self._has_age_policy_candidate(session, broker_id) or self._has_worker_technical_hold(
                session, broker_id
            )

    @staticmethod
    def _age_policy_order_is_valid(
        session: Session, reservation: TradingSubmitReservationModel, *, captured_at: datetime
    ) -> bool:
        policy = reservation.payload.get("_age_policy")
        if (
            not isinstance(policy, dict)
            or type(policy.get("version")) is not int
            or policy.get("version") != 1
            or not isinstance(policy.get("expired_at"), str)
        ):
            return False
        try:
            expired_at = require_utc_millisecond(datetime.fromisoformat(policy["expired_at"].replace("Z", "+00:00")))
        except (TypeError, ValueError):
            return False
        order = session.scalar(
            select(BrokerOrderModel).where(
                BrokerOrderModel.user_broker_id == reservation.user_broker_id,
                BrokerOrderModel.idempotency_key == reservation.idempotency_key,
            )
        )
        if (
            order is None
            or order.automation_id != reservation.automation_id
            or not (
                (order.state == "FAILED" and order.terminal_at is None)
                or (order.state in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"} and order.terminal_at is not None)
            )
            or order.idempotency_key != reservation.idempotency_key
            or order.side != reservation.side
            or order.quantity_lots != reservation.quantity_lots
        ):
            return False
        try:
            require_utc_millisecond(order.created_at)
            if not is_historical(order.created_at, expired_at) or expired_at > captured_at:
                return False
        except ValueError:
            return False
        event = session.scalar(
            select(BrokerOrderEventModel)
            .where(
                BrokerOrderEventModel.user_broker_id == reservation.user_broker_id,
                BrokerOrderEventModel.broker_order_id == order.id,
                BrokerOrderEventModel.safe_reason == "ORDER_AGE_LIMIT_EXCEEDED",
            )
            .order_by(BrokerOrderEventModel.created_at, BrokerOrderEventModel.id)
            .limit(1)
        )
        return (
            event is not None
            and event.occurred_at == expired_at
            and event.from_state
            in {"CREATED", "DISPATCH_PENDING", "SUBMITTING", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "UNCERTAIN"}
            and event.to_state == "FAILED"
            and event.safe_reason == "ORDER_AGE_LIMIT_EXCEEDED"
            and event.safe_message == "Order exceeded 24-hour processing limit"
        )

    @staticmethod
    def _account_positions_match_ledger(
        session: Session, broker_id: str, anchors: tuple[ReconciliationPositionAnchor, ...]
    ) -> bool:
        broker_units = {item.instrument_uid: item.quantity_units for item in anchors}
        ledger_units: dict[str, Decimal] = {}
        rows = session.execute(
            select(
                BrokerInstrumentModel.external_instrument_id,
                BrokerInstrumentModel.lot_size,
                PositionLotModel.remaining_lots,
            )
            .join(
                TradingAutomationModel,
                and_(
                    TradingAutomationModel.user_broker_id == PositionLotModel.user_broker_id,
                    TradingAutomationModel.id == PositionLotModel.automation_id,
                ),
            )
            .join(
                BrokerInstrumentModel,
                and_(
                    BrokerInstrumentModel.user_broker_id == TradingAutomationModel.user_broker_id,
                    BrokerInstrumentModel.id == TradingAutomationModel.instrument_id,
                ),
            )
            .where(PositionLotModel.user_broker_id == broker_id)
        )
        for uid, lot_size, remaining_lots in rows:
            ledger_units[uid] = ledger_units.get(uid, Decimal(0)) + Decimal(remaining_lots * lot_size)
        return all(
            broker_units.get(uid, Decimal(0)) == ledger_units.get(uid, Decimal(0))
            for uid in set(broker_units) | set(ledger_units)
        )

    @staticmethod
    def _request_worker_technical_resume(session: Session, broker_id: str, *, now: datetime) -> None:
        holds = list(
            session.scalars(
                select(TradingAutomationModel).where(
                    TradingAutomationModel.user_broker_id == broker_id,
                    TradingAutomationModel.state == "HOLD",
                    TradingAutomationModel.suspended_from_state == "IN_WORK",
                    TradingAutomationModel.hold_reason == WORKER_UNCERTAIN_HOLD_REASON,
                    TradingAutomationModel.closed_at.is_(None),
                )
            )
        )
        for automation in holds:
            event = session.scalar(
                select(AutomationEventModel)
                .where(
                    AutomationEventModel.user_broker_id == broker_id,
                    AutomationEventModel.automation_id == automation.id,
                    AutomationEventModel.fact_kind == "AUTOMATION_STATE_CHANGED",
                )
                .order_by(AutomationEventModel.sequence_number.desc())
                .limit(1)
            )
            if event is None or not (
                event.payload.get("state") == "HOLD"
                and event.payload.get("suspended_from_state") == "IN_WORK"
                and event.payload.get("hold_reason") == automation.hold_reason
                and event.expected_revision + 1 == automation.revision
            ):
                continue
            session.execute(
                update(TradingAutomationModel)
                .where(
                    TradingAutomationModel.user_broker_id == broker_id,
                    TradingAutomationModel.id == automation.id,
                    TradingAutomationModel.state == "HOLD",
                    TradingAutomationModel.suspended_from_state == "IN_WORK",
                    TradingAutomationModel.revision == automation.revision,
                    TradingAutomationModel.hold_reason == automation.hold_reason,
                    TradingAutomationModel.resume_requested.is_(False),
                    TradingAutomationModel.closed_at.is_(None),
                )
                .values(
                    resume_requested=True,
                    updated_at=now,
                )
            )

    def get_runtime_credential(self, runtime_instance_id: str) -> WorkerRuntimeCredentialRecord | None:
        with self._factory() as session:
            runtime = session.get(WorkerRuntimeInstanceModel, runtime_instance_id)
            if runtime is None:
                return None
            admission = session.get(TradingAccountAdmissionModel, runtime.user_broker_id)
            return WorkerRuntimeCredentialRecord(
                runtime_instance_id=runtime.runtime_instance_id,
                user_broker_id=runtime.user_broker_id,
                worker_id=runtime.worker_id,
                environment=runtime.environment,
                access_mode=runtime.access_mode,
                strategy_enabled=runtime.strategy_enabled,
                secret_digest=runtime.secret_digest,
                registration_status=runtime.registration_status,
                generation=runtime.generation,
                lease_expires_at=runtime.lease_expires_at,
                recovery_only=(admission is not None and admission.recovery_runtime_instance_id == runtime_instance_id),
            )

    def get_scope(self, broker_id: str) -> tuple[str, str]:
        with self._factory() as session:
            broker = session.get(UserBrokerModel, broker_id)
            if (
                broker is None
                or broker.archived_at is not None
                or broker.state != "ACTIVE"
                or not broker.external_account_id
            ):
                raise ValueError("Configured broker account is unavailable.")
            return broker.external_account_id, broker.environment

    def renew_owner(
        self,
        runtime_instance_id: str,
        generation: int,
        *,
        final_clock: Callable[[], datetime],
        lease_duration: timedelta,
    ) -> RuntimeRegistrationResult:
        expired = False
        result = None
        with self._factory.begin() as session:
            runtime = session.get(WorkerRuntimeInstanceModel, runtime_instance_id)
            if runtime is None:
                raise AdmissionConflictError("Runtime owner is unavailable.")
            broker = session.get(UserBrokerModel, runtime.user_broker_id, with_for_update=True)
            runtime = session.get(WorkerRuntimeInstanceModel, runtime_instance_id, with_for_update=True)
            admission = session.get(TradingAccountAdmissionModel, runtime.user_broker_id, with_for_update=True)
            now = final_clock()
            lease_expires_at = now + lease_duration
            if (
                broker is None
                or admission is None
                or runtime.registration_status != RuntimeRegistrationStatus.OWNER.value
                or runtime.generation != generation
                or admission.owner_runtime_instance_id != runtime_instance_id
                or admission.owner_generation != generation
            ):
                raise AdmissionConflictError("Runtime is no longer the current account owner.")
            if admission.lease_expires_at is None or admission.lease_expires_at <= now:
                admission.revision += 1
                admission.admission_status = AdmissionStatus.DISARMED.value
                admission.arm_revision = None
                admission.arm_expires_at = None
                self._hold_account_automations(session, runtime.user_broker_id, "OWNER_LEASE_EXPIRED")
                self._event(session, runtime.user_broker_id, "OWNER_LEASE_EXPIRED", now, {"generation": generation})
                expired = True
            else:
                runtime.last_seen_at = now
                runtime.lease_expires_at = lease_expires_at
                admission.lease_expires_at = lease_expires_at
                checkpoint_at = session.scalar(
                    select(TradingAdmissionEventModel.occurred_at)
                    .where(
                        TradingAdmissionEventModel.user_broker_id == runtime.user_broker_id,
                        TradingAdmissionEventModel.event_type == "OWNER_LEASE_RENEWED",
                        or_(
                            TradingAdmissionEventModel.generation == generation,
                            and_(
                                TradingAdmissionEventModel.generation.is_(None),
                                TradingAdmissionEventModel.payload["generation"].as_integer() == generation,
                            ),
                        ),
                    )
                    .order_by(TradingAdmissionEventModel.occurred_at.desc())
                    .limit(1)
                )
                if checkpoint_at is None or now - checkpoint_at >= timedelta(seconds=300):
                    self._event(
                        session,
                        runtime.user_broker_id,
                        "OWNER_LEASE_RENEWED",
                        now,
                        {"generation": generation},
                        runtime_instance_id=runtime_instance_id,
                        generation=generation,
                    )
                result = self._registration_result(runtime, admission)
        if expired:
            raise AdmissionConflictError("Expired owner lease cannot be renewed.")
        if result is None:
            raise RuntimeError("Owner lease renewal returned no result.")
        return result

    def unresolved_request_ids(self, broker_id: str) -> tuple[str, ...]:
        with self._factory() as session:
            return tuple(
                session.scalars(
                    select(TradingSubmitReservationModel.idempotency_key)
                    .where(
                        TradingSubmitReservationModel.user_broker_id == broker_id,
                        TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN")),
                    )
                    .order_by(TradingSubmitReservationModel.created_at)
                )
            )

    def unresolved_reservation_count(self, broker_id: str) -> int:
        with self._factory() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(TradingSubmitReservationModel)
                    .where(
                        TradingSubmitReservationModel.user_broker_id == broker_id,
                        TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN")),
                    )
                )
                or 0
            )

    def submit_attempt(
        self,
        broker_id: str,
        idempotency_key: str,
        *,
        original_runtime_instance_id: str | None,
        original_generation: int | None,
    ) -> SubmitAttemptView:
        with self._factory.begin() as session:
            broker = session.get(UserBrokerModel, broker_id, with_for_update=True)
            if broker is None or broker.external_account_id is None:
                raise ValueError("Submit account scope is unavailable.")
            reservation = session.scalar(
                select(TradingSubmitReservationModel).where(
                    TradingSubmitReservationModel.user_broker_id == broker_id,
                    TradingSubmitReservationModel.idempotency_key == idempotency_key,
                )
            )
            if reservation is not None:
                original = session.get(WorkerRuntimeInstanceModel, reservation.runtime_instance_id)
                return SubmitAttemptView(
                    broker_id=UUID(broker_id),
                    account_id=broker.external_account_id,
                    idempotency_key=UUID(idempotency_key),
                    consumed_state=SubmitReservationState(reservation.state),
                    proven_not_granted=False,
                    original_runtime_instance_id=UUID(reservation.runtime_instance_id),
                    original_generation=reservation.generation,
                    original_generation_retired=(original is not None and original.registration_status == "RETIRED"),
                    absence_basis=None,
                )
            origin = None
            if original_runtime_instance_id is not None and original_generation is not None:
                origin = session.get(WorkerRuntimeInstanceModel, original_runtime_instance_id)
            proven_absence = (
                origin is not None
                and origin.user_broker_id == broker_id
                and origin.generation == original_generation
                and origin.registration_status == "RETIRED"
            )
            return SubmitAttemptView(
                broker_id=UUID(broker_id),
                account_id=broker.external_account_id,
                idempotency_key=UUID(idempotency_key),
                consumed_state=None,
                proven_not_granted=proven_absence,
                original_runtime_instance_id=(None if not proven_absence else UUID(original_runtime_instance_id)),
                original_generation=original_generation if proven_absence else None,
                original_generation_retired=proven_absence,
                absence_basis=SubmitAbsenceBasis.RETIRED_GENERATION if proven_absence else None,
            )

    def grant_submit(
        self,
        request: SubmitAuthorizationRequest,
        *,
        runtime_instance_id: UUID,
        generation: int,
        final_clock: Callable[[], datetime],
        risk_snapshot_captured_at: datetime,
        risk_snapshot_valid_until: datetime,
        risk_instrument: AdmissionInstrument,
        risk_position_anchors: tuple[ReconciliationPositionAnchor, ...],
        order_states: tuple[AdmissionOrderState, ...],
        risk_snapshot_id: UUID,
        reserved_cash: Decimal,
        reserved_position_units: Decimal,
        requested_amount: Decimal,
        expected_reservation_digest: str,
    ) -> SubmitAuthorizationResult:
        broker_id = str(request.broker_id)
        with self._factory.begin() as session:
            broker = session.get(UserBrokerModel, broker_id, with_for_update=True)
            admission = session.get(TradingAccountAdmissionModel, broker_id, with_for_update=True)
            if broker is None or admission is None or broker.external_account_id is None:
                return SubmitDenied(reason=SubmitDenialReason.NOT_OWNER, outcome_uncertain=False)
            existing = session.scalar(
                select(TradingSubmitReservationModel).where(
                    TradingSubmitReservationModel.user_broker_id == broker_id,
                    TradingSubmitReservationModel.idempotency_key == str(request.idempotency_key),
                )
            )
            if existing is not None:
                original = session.get(WorkerRuntimeInstanceModel, existing.runtime_instance_id)
                attempt = SubmitAttemptView(
                    broker_id=request.broker_id,
                    account_id=broker.external_account_id,
                    idempotency_key=request.idempotency_key,
                    consumed_state=SubmitReservationState(existing.state),
                    proven_not_granted=False,
                    original_runtime_instance_id=UUID(existing.runtime_instance_id),
                    original_generation=existing.generation,
                    original_generation_retired=original is not None and original.registration_status == "RETIRED",
                    absence_basis=None,
                )
                return SubmitDenied(
                    reason=SubmitDenialReason.ALREADY_CONSUMED,
                    outcome_uncertain=True,
                    attempt=attempt,
                )
            if self._reservation_digest(session, broker_id) != expected_reservation_digest:
                return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
            automation = session.scalar(
                select(TradingAutomationModel)
                .where(
                    TradingAutomationModel.user_broker_id == broker_id,
                    TradingAutomationModel.id == str(request.automation_id),
                )
                .with_for_update()
            )
            instrument = session.scalar(
                select(BrokerInstrumentModel)
                .where(
                    BrokerInstrumentModel.user_broker_id == broker_id,
                    BrokerInstrumentModel.external_instrument_id == request.instrument_uid,
                )
                .with_for_update()
            )
            if (
                automation is None
                or instrument is None
                or automation.instrument_id != instrument.id
                or risk_instrument.instrument_uid != request.instrument_uid
            ):
                return SubmitDenied(reason=SubmitDenialReason.UID_NOT_ALLOWED, outcome_uncertain=False)
            if automation.state != "IN_WORK":
                return SubmitDenied(reason=SubmitDenialReason.AUTOMATION_NOT_ACTIVE, outcome_uncertain=False)
            if (
                instrument.instrument_type != risk_instrument.instrument_type
                or instrument.currency != risk_instrument.currency
                or instrument.lot_size != risk_instrument.lot_size
                or instrument.currency != request.currency
                or instrument.lot_size != request.lot_size
                or instrument.min_price_increment != risk_instrument.price_increment
                or instrument.api_trade_available != risk_instrument.api_trade_available
                or not instrument.is_active
            ):
                return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
            now = final_clock()
            owner = session.get(WorkerRuntimeInstanceModel, admission.owner_runtime_instance_id)
            baseline = (
                session.get(TradingAdmissionBaselineModel, admission.baseline_id)
                if admission.baseline_id is not None
                else None
            )
            reconciliation = (
                session.get(TradingReconciliationModel, admission.reconciliation_id)
                if admission.reconciliation_id is not None
                else None
            )
            denial = None
            if (
                broker.environment != "PROD"
                or broker.state != "ACTIVE"
                or broker.archived_at is not None
                or broker.external_account_id != request.account_id
                or owner is None
                or owner.registration_status != "OWNER"
                or owner.environment != "PROD"
                or owner.runtime_instance_id != str(runtime_instance_id)
                or admission.owner_runtime_instance_id != str(runtime_instance_id)
                or owner.generation != generation
                or admission.owner_generation != generation
                or request.generation != generation
            ):
                denial = SubmitDenialReason.NOT_OWNER
            elif admission.revision != request.revision:
                denial = SubmitDenialReason.DISARMED
            elif admission.lease_expires_at is None or admission.lease_expires_at <= now:
                denial = SubmitDenialReason.LEASE_EXPIRED
            elif owner.access_mode != "TRADE" or not owner.strategy_enabled:
                denial = SubmitDenialReason.NOT_OWNER
            elif automation.state != "IN_WORK":
                denial = SubmitDenialReason.AUTOMATION_NOT_ACTIVE
            elif (
                baseline is None
                or admission.baseline_id != str(baseline.baseline_id)
                or baseline.moscow_day != now.astimezone(ZoneInfo("Europe/Moscow")).date()
            ):
                denial = SubmitDenialReason.BASELINE_REQUIRED
            elif admission.limits is None:
                denial = SubmitDenialReason.LIMITS_NOT_CONFIGURED
            elif admission.configuration_fingerprint != request.configuration_fingerprint or (
                self._current_configuration_fingerprint(broker, owner.access_mode) != request.configuration_fingerprint
            ):
                denial = SubmitDenialReason.CONFIGURATION_CHANGED
            elif (
                request.submit_deadline <= now
                or request.market_valid_until <= now
                or risk_snapshot_captured_at > now
                or now - risk_snapshot_captured_at > timedelta(seconds=1)
                or risk_snapshot_valid_until <= now
            ):
                denial = SubmitDenialReason.STALE_MARKET
            if denial is not None:
                return SubmitDenied(reason=denial, outcome_uncertain=False)

            expires_at = min(
                admission.lease_expires_at,
                request.market_valid_until,
                request.submit_deadline,
                now + timedelta(seconds=1),
            )
            if expires_at <= now:
                return SubmitDenied(reason=SubmitDenialReason.STALE_MARKET, outcome_uncertain=False)

            if not self._apply_submit_settlement(
                session,
                broker_id,
                admission,
                risk_position_anchors,
                order_states,
                now=now,
            ):
                return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)

            # Promote the proven fresh snapshot to the account anchor used by the
            # next permit. This also upgrades legacy scalar canary anchors without DDL.
            reconciliation_id = str(uuid4())
            position_anchors = tuple(item.model_dump(mode="json") for item in risk_position_anchors)
            active_order_count = sum(
                state.status.upper() not in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"} for state in order_states
            )
            prior_reservations = tuple(
                session.scalars(
                    select(TradingSubmitReservationModel).where(
                        TradingSubmitReservationModel.user_broker_id == broker_id,
                        TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN", "SETTLED")),
                    )
                )
            )
            unresolved_count = sum(
                item.state in {"IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN"} for item in prior_reservations
            )
            reconciliation = TradingReconciliationModel(
                reconciliation_id=reconciliation_id,
                user_broker_id=broker_id,
                account_id=broker.external_account_id,
                scope="ACCOUNT",
                owner_runtime_instance_id=str(runtime_instance_id),
                owner_generation=generation,
                configuration_fingerprint=request.configuration_fingerprint,
                status=ReconciliationStatus.ACCOUNT_ONLY.value,
                captured_at=risk_snapshot_captured_at,
                valid_until=risk_snapshot_valid_until,
                can_arm=True,
                active_ordinary_order_count=active_order_count,
                active_stop_order_count=0,
                unresolved_reservation_count=unresolved_count + 1,
                reasons=[],
                risk_snapshot_id=str(risk_snapshot_id),
                payload={
                    "reconciliation_id": reconciliation_id,
                    "broker_id": broker_id,
                    "account_id": broker.external_account_id,
                    "scope": "ACCOUNT",
                    "owner_runtime_instance_id": str(runtime_instance_id),
                    "owner_generation": generation,
                    "configuration_fingerprint": request.configuration_fingerprint,
                    "status": ReconciliationStatus.ACCOUNT_ONLY.value,
                    "canary_uid": None,
                    "captured_at": risk_snapshot_captured_at.isoformat(),
                    "valid_until": risk_snapshot_valid_until.isoformat(),
                    "can_arm": True,
                    "position_anchor_units": None,
                    "position_anchors": list(position_anchors),
                    "active_ordinary_order_count": active_order_count,
                    "active_stop_order_count": 0,
                    "unresolved_reservation_count": unresolved_count + 1,
                    "reasons": [],
                    "risk_snapshot_id": str(risk_snapshot_id),
                },
            )
            session.add(reconciliation)
            admission.reconciliation_id = reconciliation_id
            admission.recovery_only = False
            admission.recovery_runtime_instance_id = None
            for prior_reservation in prior_reservations:
                prior_reservation.payload = {
                    **prior_reservation.payload,
                    "anchor_reconciliation_id": reconciliation_id,
                    "anchor_executed_lots": prior_reservation.payload.get("executed_lots", 0),
                }

            permit_id = uuid4()
            request_fingerprint = submit_request_fingerprint(request)
            session.add(
                TradingSubmitReservationModel(
                    reservation_id=str(permit_id),
                    user_broker_id=broker_id,
                    automation_id=str(request.automation_id),
                    idempotency_key=str(request.idempotency_key),
                    request_fingerprint=request_fingerprint,
                    state=SubmitReservationState.IN_FLIGHT.value,
                    runtime_instance_id=str(runtime_instance_id),
                    generation=generation,
                    configuration_fingerprint=request.configuration_fingerprint,
                    permit_id=str(permit_id),
                    side=request.side,
                    instrument_uid=request.instrument_uid,
                    quantity_lots=request.quantity_lots,
                    limit_price=request.limit_price,
                    requested_amount=requested_amount,
                    reserved_cash=reserved_cash,
                    reserved_position_units=reserved_position_units,
                    payload={
                        "risk_snapshot_id": str(risk_snapshot_id),
                        "anchor_reconciliation_id": admission.reconciliation_id,
                        "anchor_executed_lots": 0,
                        "executed_lots": 0,
                        "executed_amount": "0",
                        "executed_commission": "0",
                        "original_reserved_cash": str(reserved_cash),
                    },
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            self._event(
                session,
                broker_id,
                "SUBMIT_PERMIT_GRANTED",
                now,
                {
                    "permit_id": str(permit_id),
                    "runtime_instance_id": str(runtime_instance_id),
                    "generation": generation,
                    "idempotency_key": str(request.idempotency_key),
                    "request_fingerprint": request_fingerprint,
                    "configuration_fingerprint": request.configuration_fingerprint,
                    "risk_snapshot_id": str(risk_snapshot_id),
                },
            )
            return SubmitPermit(
                permit_id=permit_id,
                runtime_instance_id=runtime_instance_id,
                idempotency_key=request.idempotency_key,
                generation=generation,
                revision=request.revision,
                configuration_fingerprint=request.configuration_fingerprint,
                request_fingerprint=request_fingerprint,
                issued_at=now,
                expires_at=expires_at,
                server_response_at=now,
                risk_snapshot_id=risk_snapshot_id,
            )

    @staticmethod
    def _settled_reservation_execution_is_valid(reservation: TradingSubmitReservationModel) -> bool:
        payload = reservation.payload
        executed_lots = payload.get("executed_lots", 0)
        anchor_executed_lots = payload.get("anchor_executed_lots", 0)
        age_policy = payload.get("_age_policy")
        age_settlement = payload.get("settlement_reason") == "AGE_POLICY"
        broker_unknown = payload.get("broker_outcome_unknown", False)
        try:
            executed_amount = Decimal(str(payload.get("executed_amount", "0")))
            executed_commission = Decimal(str(payload.get("executed_commission", "0")))
        except (ArithmeticError, ValueError):
            return False
        return (
            type(executed_lots) is int
            and 0 <= executed_lots <= reservation.quantity_lots
            and type(anchor_executed_lots) is int
            and 0 <= anchor_executed_lots <= executed_lots
            and executed_amount.is_finite()
            and executed_amount >= 0
            and executed_commission.is_finite()
            and executed_commission >= 0
            and (executed_lots != 0 or executed_amount == 0)
            and (executed_lots == 0 or executed_amount > 0)
            and (
                not age_settlement
                or (
                    isinstance(age_policy, dict)
                    and type(age_policy.get("version")) is int
                    and age_policy["version"] == 1
                )
            )
            and (not broker_unknown or (age_settlement and payload.get("last_reconciled_order_state") == "UNKNOWN"))
        )

    @staticmethod
    def _apply_submit_settlement(
        session: Session,
        broker_id: str,
        admission: TradingAccountAdmissionModel,
        risk_position_anchors: tuple[ReconciliationPositionAnchor, ...],
        order_states: tuple[AdmissionOrderState, ...],
        *,
        now: datetime,
    ) -> bool:
        reservations = list(
            session.scalars(
                select(TradingSubmitReservationModel).where(
                    TradingSubmitReservationModel.user_broker_id == broker_id,
                    TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN", "SETTLED")),
                )
            )
        )
        unresolved = [item for item in reservations if item.state != SubmitReservationState.SETTLED.value]
        states_by_id: dict[str, list[AdmissionOrderState]] = {}
        for state in order_states:
            states_by_id.setdefault(state.request_id, []).append(state)
        expected_keys = {item.idempotency_key for item in unresolved}
        prior = (
            session.get(TradingReconciliationModel, admission.reconciliation_id)
            if admission.reconciliation_id is not None
            else None
        )
        current = {item.instrument_uid: item.quantity_units for item in risk_position_anchors}
        anchor_result = None if prior is None else ReconciliationResult.model_validate(prior.payload)
        anchors = (
            {} if anchor_result is None else {item.instrument_uid: item for item in anchor_result.position_anchors}
        )
        legacy_scalar_anchor = anchor_result is not None and not anchors
        if legacy_scalar_anchor:
            legacy_uid = anchor_result.canary_uid
            legacy_units = anchor_result.position_anchor_units
            current_anchor = next((item for item in risk_position_anchors if item.instrument_uid == legacy_uid), None)
            if (
                anchor_result.scope != "CANARY_UID"
                or anchor_result.status is not ReconciliationStatus.CANARY_READY
                or not legacy_uid
                or legacy_units is None
                or not legacy_units.is_finite()
                or legacy_units < 0
                or current_anchor is None
                or any(item.instrument_uid != legacy_uid for item in reservations)
            ):
                legacy_scalar_anchor = False
            else:
                anchors[legacy_uid] = ReconciliationPositionAnchor(
                    instrument_uid=legacy_uid,
                    quantity_units=legacy_units,
                    lot_size=current_anchor.lot_size,
                )
        valid = not reservations or (
            anchor_result is not None
            and (
                (anchor_result.scope == "ACCOUNT" and anchor_result.status is ReconciliationStatus.ACCOUNT_ONLY)
                or legacy_scalar_anchor
            )
            and set(states_by_id) == expected_keys
            and all(len(items) == 1 for items in states_by_id.values())
        )
        if not reservations:
            return True
        expected_units = {uid: item.quantity_units for uid, item in anchors.items()}
        terminal_states = {"FILLED", "CANCELLED", "REJECTED"}
        active_states = {"NEW", "PARTIALLYFILL", "PARTIALLY_FILLED"}
        updates: list[tuple[TradingSubmitReservationModel, AdmissionOrderState, bool]] = []
        if valid and anchor_result is not None:
            anchor_id = str(anchor_result.reconciliation_id)
            for reservation in reservations:
                anchor_lots = int(reservation.payload.get("anchor_executed_lots", 0))
                prior_lots = int(reservation.payload.get("executed_lots", 0))
                if reservation.state == SubmitReservationState.SETTLED.value:
                    if reservation.payload.get(
                        "anchor_reconciliation_id"
                    ) != anchor_id or not TradingAdmissionRepository._settled_reservation_execution_is_valid(
                        reservation
                    ):
                        valid = False
                        break
                    if prior_lots < anchor_lots:
                        valid = False
                        break
                    if prior_lots > anchor_lots:
                        uid_anchor = anchors.get(reservation.instrument_uid)
                        if uid_anchor is None:
                            valid = False
                            break
                        sign = Decimal(1) if reservation.side == "BUY" else Decimal(-1)
                        expected_units[reservation.instrument_uid] += sign * Decimal(
                            (prior_lots - anchor_lots) * uid_anchor.lot_size
                        )
                    continue
                if reservation.payload.get("anchor_reconciliation_id") != anchor_id:
                    valid = False
                    break
                uid_anchor = anchors.get(reservation.instrument_uid)
                if uid_anchor is None:
                    valid = False
                    break
                state = states_by_id[reservation.idempotency_key][0]
                status = state.status.upper()
                lots = state.executed_lots if state.executed_lots is not None else -1
                gross = state.executed_amount
                fee = state.executed_commission
                prior_gross = Decimal(str(reservation.payload.get("executed_amount", "0")))
                prior_fee = Decimal(str(reservation.payload.get("executed_commission", "0")))
                if (
                    not state.attributable
                    or state.unknown_reason is not None
                    or state.broker_order_id is None
                    or state.instrument_uid != reservation.instrument_uid
                    or state.side is None
                    or state.side.value != reservation.side
                    or state.requested_lots != reservation.quantity_lots
                    or lots < prior_lots
                    or lots < anchor_lots
                    or lots > reservation.quantity_lots
                    or gross is None
                    or not gross.is_finite()
                    or gross < prior_gross
                    or fee is None
                    or not fee.is_finite()
                    or fee < prior_fee
                    or state.currency != "RUB"
                    or (status not in terminal_states and status not in active_states)
                    or (status == "REJECTED" and lots != 0)
                    or (lots == 0 and gross != 0)
                    or (lots > 0 and gross <= 0)
                ):
                    valid = False
                    break
                updates.append((reservation, state, status in terminal_states))
                if lots < anchor_lots or gross is None or fee is None:
                    valid = False
                    break
                if not gross.is_finite() or not fee.is_finite() or gross < 0 or fee < 0:
                    valid = False
                    break
                sign = Decimal(1) if reservation.side == "BUY" else Decimal(-1)
                expected_units[reservation.instrument_uid] += sign * Decimal((lots - anchor_lots) * uid_anchor.lot_size)

        current_keys = set(expected_units) if legacy_scalar_anchor else set(current) | set(expected_units)
        if valid and all(current.get(uid, Decimal(0)) == expected_units.get(uid, Decimal(0)) for uid in current_keys):
            for reservation, state, is_terminal in updates:
                if state is None:
                    return False
                reservation.state = (
                    SubmitReservationState.SETTLED.value if is_terminal else SubmitReservationState.BROKER_ACTIVE.value
                )
                if is_terminal:
                    reservation.reserved_cash = Decimal(0)
                    reservation.reserved_position_units = Decimal(0)
                else:
                    original_reserved_cash = Decimal(str(reservation.payload.get("original_reserved_cash", "0")))
                    reservation.reserved_cash = (
                        max(Decimal(0), original_reserved_cash - state.executed_amount - state.executed_commission)
                        if reservation.side == "BUY"
                        else Decimal(0)
                    )
                    reservation.reserved_position_units = (
                        Decimal(
                            (reservation.quantity_lots - state.executed_lots)
                            * anchors[reservation.instrument_uid].lot_size
                        )
                        if reservation.side == "SELL"
                        else Decimal(0)
                    )
                reservation.updated_at = now
                reservation.payload = {
                    **reservation.payload,
                    "last_reconciled_order_state": state.status,
                    "executed_lots": state.executed_lots,
                    "executed_amount": str(state.executed_amount),
                    "executed_commission": str(state.executed_commission),
                }
            return True

        for reservation in unresolved:
            reservation.state = SubmitReservationState.UNKNOWN.value
            reservation.updated_at = now
        return False

    def save_baseline(
        self,
        baseline: AdmissionBaseline,
        *,
        configuration_fingerprint: str,
    ) -> AdmissionBaseline:
        broker_id = str(baseline.broker_id)
        with self._factory.begin() as session:
            broker = session.get(UserBrokerModel, broker_id, with_for_update=True)
            admission = session.get(TradingAccountAdmissionModel, broker_id, with_for_update=True)
            if (
                broker is None
                or broker.environment != "PROD"
                or not self._environment_matches(broker)
                or broker.state != "ACTIVE"
                or broker.archived_at is not None
                or broker.external_account_id != baseline.account_id
                or admission is None
            ):
                raise AdmissionConflictError("The account scope changed before baseline acceptance.")
            owner = session.get(WorkerRuntimeInstanceModel, admission.owner_runtime_instance_id)
            if (
                owner is None
                or self._current_configuration_fingerprint(broker, owner.access_mode) != configuration_fingerprint
            ):
                raise AdmissionConflictError("Broker configuration changed before baseline acceptance.")
            existing_for_day = session.scalar(
                select(TradingAdmissionBaselineModel).where(
                    TradingAdmissionBaselineModel.user_broker_id == broker_id,
                    TradingAdmissionBaselineModel.moscow_day == baseline.moscow_day,
                )
            )
            if existing_for_day is not None:
                raise AdmissionConflictError("A baseline is already accepted for this account and day.")
            model = TradingAdmissionBaselineModel(
                baseline_id=str(baseline.baseline_id),
                user_broker_id=broker_id,
                account_id=baseline.account_id,
                moscow_day=baseline.moscow_day,
                currency=baseline.currency,
                equity=baseline.equity,
                net_external_cash_flow=baseline.net_external_cash_flow,
                capture_started_at=baseline.capture_started_at,
                capture_finished_at=baseline.capture_finished_at,
                operations_through=baseline.operations_through,
                accepted_by=baseline.accepted_by,
                accepted_at=baseline.accepted_at,
                payload=baseline.model_dump(mode="json"),
            )
            session.add(model)
            admission.baseline_id = model.baseline_id
            admission.revision += 1
            admission.admission_status = AdmissionStatus.DISARMED.value
            admission.arm_revision = None
            admission.arm_expires_at = None
            self._event(session, broker_id, "BASELINE_ACCEPTED", baseline.accepted_at, baseline.model_dump(mode="json"))
        return baseline

    def ensure_daily_baseline(
        self,
        baseline: AdmissionBaseline,
        *,
        configuration_fingerprint: str,
        expected_owner_runtime_instance_id: str,
        expected_generation: int,
        snapshot_valid_until: datetime,
        final_clock: Callable[[], datetime],
    ) -> AdmissionBaseline:
        """Accept one current-day baseline or return the immutable winner unchanged."""
        broker_id = str(baseline.broker_id)
        with self._factory.begin() as session:
            broker = session.get(UserBrokerModel, broker_id, with_for_update=True)
            admission = session.get(TradingAccountAdmissionModel, broker_id, with_for_update=True)
            if admission is None or admission.owner_runtime_instance_id is None:
                raise AdmissionConflictError("Daily baseline scope has no current owner.")
            owner = session.get(
                WorkerRuntimeInstanceModel,
                admission.owner_runtime_instance_id,
                with_for_update=True,
            )
            now = final_clock()
            if (
                broker is None
                or broker.environment != "PROD"
                or broker.state != "ACTIVE"
                or broker.archived_at is not None
                or broker.external_account_id != baseline.account_id
                or admission.owner_runtime_instance_id != expected_owner_runtime_instance_id
                or admission.owner_generation != expected_generation
                or admission.lease_expires_at is None
                or admission.lease_expires_at <= now
                or owner is None
                or owner.registration_status != RuntimeRegistrationStatus.OWNER.value
                or owner.retired_at is not None
                or owner.environment != "PROD"
                or owner.access_mode != "TRADE"
                or owner.generation != expected_generation
                or owner.lease_expires_at is None
                or owner.lease_expires_at <= now
                or self._current_configuration_fingerprint(broker, owner.access_mode) != configuration_fingerprint
                or baseline.moscow_day != now.astimezone(ZoneInfo("Europe/Moscow")).date()
            ):
                raise AdmissionConflictError("Daily baseline owner, scope, configuration, or Moscow day changed.")

            existing = session.scalar(
                select(TradingAdmissionBaselineModel).where(
                    TradingAdmissionBaselineModel.user_broker_id == broker_id,
                    TradingAdmissionBaselineModel.moscow_day == baseline.moscow_day,
                )
            )
            if existing is not None:
                return AdmissionBaseline.model_validate(existing.payload)
            if snapshot_valid_until <= now or baseline.capture_finished_at > now or baseline.accepted_at > now:
                raise AdmissionConflictError("Daily baseline snapshot expired before acceptance.")

            model = TradingAdmissionBaselineModel(
                baseline_id=str(baseline.baseline_id),
                user_broker_id=broker_id,
                account_id=baseline.account_id,
                moscow_day=baseline.moscow_day,
                currency=baseline.currency,
                equity=baseline.equity,
                net_external_cash_flow=baseline.net_external_cash_flow,
                capture_started_at=baseline.capture_started_at,
                capture_finished_at=baseline.capture_finished_at,
                operations_through=baseline.operations_through,
                accepted_by=baseline.accepted_by,
                accepted_at=baseline.accepted_at,
                payload=baseline.model_dump(mode="json"),
            )
            session.add(model)
            admission.baseline_id = model.baseline_id
            admission.revision += 1
            # Legacy STOP_REQUIRED is no longer a manual authorization state. Any
            # active automations were moved to HOLD by the stop/revoke transition.
            admission.admission_status = AdmissionStatus.DISARMED.value
            admission.arm_revision = None
            admission.arm_expires_at = None
            self._event(session, broker_id, "BASELINE_ACCEPTED", baseline.accepted_at, baseline.model_dump(mode="json"))
        return baseline

    def save_reconciliation(
        self,
        result: ReconciliationResult,
        *,
        expected_revision: int,
        now: datetime,
        order_states: tuple[AdmissionOrderState, ...] = (),
        require_unarmed: bool = False,
        final_clock: Callable[[], datetime] | None = None,
        risk_snapshot_valid_until: datetime | None = None,
    ) -> ReconciliationResult:
        broker_id = str(result.broker_id)
        with self._factory.begin() as session:
            broker = session.get(UserBrokerModel, broker_id, with_for_update=True)
            admission = session.get(TradingAccountAdmissionModel, broker_id, with_for_update=True)
            owner = (
                None
                if admission is None or admission.owner_runtime_instance_id is None
                else session.get(
                    WorkerRuntimeInstanceModel,
                    admission.owner_runtime_instance_id,
                    with_for_update=require_unarmed,
                )
            )
            if require_unarmed and final_clock is None:
                raise AdmissionConflictError("Automatic reconciliation requires a post-lock final clock.")
            checked_at = final_clock() if require_unarmed else now
            if require_unarmed and (risk_snapshot_valid_until is None or risk_snapshot_valid_until <= checked_at):
                raise AdmissionConflictError("Automatic reconciliation risk snapshot expired before save.")
            unresolved_reservations = list(
                session.scalars(
                    select(TradingSubmitReservationModel).where(
                        TradingSubmitReservationModel.user_broker_id == broker_id,
                        TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN")),
                    )
                )
            )
            age_policy_recovery = bool(unresolved_reservations) and all(
                isinstance(item.payload.get("_age_policy"), dict)
                and item.payload["_age_policy"].get("version") == 1
                for item in unresolved_reservations
            )
            technical_hold_recovery = (
                result.scope == "ACCOUNT"
                and not unresolved_reservations
                and self._has_worker_technical_hold(session, broker_id)
            )
            if (
                broker is None
                or broker.environment != "PROD"
                or broker.state != "ACTIVE"
                or broker.archived_at is not None
                or broker.external_account_id != result.account_id
                or admission is None
                or admission.revision != expected_revision
                or admission.owner_runtime_instance_id != str(result.owner_runtime_instance_id)
                or admission.owner_generation != result.owner_generation
                or admission.lease_expires_at is None
                or admission.lease_expires_at <= checked_at
                or (
                    require_unarmed
                    and (
                        admission.admission_status != AdmissionStatus.DISARMED.value
                        or (
                            admission.recovery_only
                            and not (
                                result.scope == "ACCOUNT"
                                and (age_policy_recovery or technical_hold_recovery)
                                and admission.recovery_runtime_instance_id == admission.owner_runtime_instance_id
                            )
                        )
                        or admission.baseline_id is None
                        or result.captured_at.astimezone(ZoneInfo("Europe/Moscow")).date()
                        != checked_at.astimezone(ZoneInfo("Europe/Moscow")).date()
                        or result.valid_until <= checked_at
                        or owner is None
                        or owner.registration_status != RuntimeRegistrationStatus.OWNER.value
                        or owner.retired_at is not None
                        or owner.environment != "PROD"
                        or owner.access_mode != "TRADE"
                        or owner.generation != result.owner_generation
                        or owner.lease_expires_at is None
                        or owner.lease_expires_at <= checked_at
                    )
                )
            ):
                raise AdmissionConflictError("Admission revision or owner changed during reconciliation.")
            if (
                owner is None
                or self._current_configuration_fingerprint(broker, owner.access_mode)
                != result.configuration_fingerprint
            ):
                raise AdmissionConflictError("Broker configuration changed before reconciliation was saved.")
            if (
                result.scope == "ACCOUNT"
                and unresolved_reservations
                and not (
                    require_unarmed
                    and all(
                        isinstance(item.payload.get("_age_policy"), dict)
                        and item.payload["_age_policy"].get("version") == 1
                        for item in unresolved_reservations
                    )
                )
            ):
                raise AdmissionConflictError("Account reconciliation cannot rewrite unresolved account reservations.")
            if (
                require_unarmed
                and result.scope == "ACCOUNT"
                and not self._account_positions_match_ledger(session, broker_id, result.position_anchors)
            ):
                raise AdmissionConflictError("Fresh account positions do not match the Core lot ledger.")
            result = self._reconcile_submit_reservations(
                session,
                admission,
                result,
                order_states=order_states,
            )
            if require_unarmed and not result.can_arm:
                raise AdmissionConflictError("Automatic reconciliation failed reservation settlement validation.")
            model = TradingReconciliationModel(
                reconciliation_id=str(result.reconciliation_id),
                user_broker_id=broker_id,
                account_id=result.account_id,
                scope=result.scope,
                owner_runtime_instance_id=str(result.owner_runtime_instance_id),
                owner_generation=result.owner_generation,
                configuration_fingerprint=result.configuration_fingerprint,
                status=result.status.value,
                canary_uid=result.canary_uid,
                captured_at=result.captured_at,
                valid_until=result.valid_until,
                can_arm=result.can_arm,
                position_anchor_units=result.position_anchor_units,
                active_ordinary_order_count=result.active_ordinary_order_count,
                active_stop_order_count=result.active_stop_order_count,
                unresolved_reservation_count=result.unresolved_reservation_count,
                reasons=[reason.value for reason in result.reasons],
                risk_snapshot_id=None if result.risk_snapshot_id is None else str(result.risk_snapshot_id),
                payload=result.model_dump(mode="json"),
            )
            session.add(model)
            trusted_anchor = (
                result.status is ReconciliationStatus.ACCOUNT_ONLY and result.scope == "ACCOUNT" and not result.reasons
            )
            if trusted_anchor:
                if (
                    result.unresolved_reservation_count == 0
                    and result.active_ordinary_order_count == 0
                    and result.active_stop_order_count == 0
                ):
                    settled_reservations = session.scalars(
                        select(TradingSubmitReservationModel).where(
                            TradingSubmitReservationModel.user_broker_id == broker_id,
                            TradingSubmitReservationModel.state == SubmitReservationState.SETTLED.value,
                        )
                    )
                    for reservation in settled_reservations:
                        if not self._settled_reservation_execution_is_valid(reservation):
                            continue
                        reservation.payload = {
                            **reservation.payload,
                            "anchor_reconciliation_id": model.reconciliation_id,
                            "anchor_executed_lots": reservation.payload.get("executed_lots", 0),
                        }
                admission.reconciliation_id = model.reconciliation_id
                if (
                    admission.recovery_only
                    and result.unresolved_reservation_count == 0
                    and result.active_ordinary_order_count == 0
                    and result.active_stop_order_count == 0
                ):
                    admission.recovery_only = False
                    admission.recovery_runtime_instance_id = None
                if (
                    require_unarmed
                    and self._account_positions_match_ledger(session, broker_id, result.position_anchors)
                    and result.unresolved_reservation_count == 0
                    and result.active_ordinary_order_count == 0
                    and result.active_stop_order_count == 0
                ):
                    self._request_worker_technical_resume(session, broker_id, now=result.captured_at)
            admission.revision += 1
            admission.admission_status = AdmissionStatus.DISARMED.value
            admission.arm_revision = None
            admission.arm_expires_at = None
            admission.configuration_fingerprint = result.configuration_fingerprint
            self._event(session, broker_id, "RECONCILED", result.captured_at, result.model_dump(mode="json"))
        return result

    @staticmethod
    def _reconcile_submit_reservations(
        session: Session,
        admission: TradingAccountAdmissionModel,
        result: ReconciliationResult,
        *,
        order_states: tuple[AdmissionOrderState, ...],
    ) -> ReconciliationResult:
        unresolved = list(
            session.scalars(
                select(TradingSubmitReservationModel)
                .where(
                    TradingSubmitReservationModel.user_broker_id == str(result.broker_id),
                    TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN")),
                )
                .order_by(TradingSubmitReservationModel.idempotency_key)
            )
        )
        if not unresolved:
            return result.model_copy(update={"unresolved_reservation_count": 0})
        if result.scope == "ACCOUNT" and all(
            isinstance(item.payload.get("_age_policy"), dict) and item.payload["_age_policy"].get("version") == 1
            for item in unresolved
        ):
            return TradingAdmissionRepository._settle_age_policy_candidates(
                session, result, unresolved, order_states=order_states
            )
        reservations = list(
            session.scalars(
                select(TradingSubmitReservationModel).where(
                    TradingSubmitReservationModel.user_broker_id == str(result.broker_id),
                    TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN", "SETTLED")),
                )
            )
        )

        prior = (
            session.get(TradingReconciliationModel, admission.reconciliation_id)
            if admission.reconciliation_id is not None
            else None
        )
        instrument = session.scalar(
            select(BrokerInstrumentModel).where(
                BrokerInstrumentModel.user_broker_id == str(result.broker_id),
                BrokerInstrumentModel.external_instrument_id == result.canary_uid,
            )
        )
        states_by_id: dict[str, list[AdmissionOrderState]] = {}
        for order_state in order_states:
            states_by_id.setdefault(order_state.request_id, []).append(order_state)

        reason_set = set(result.reasons)
        safe_to_attribute = (
            result.scope == "CANARY_UID"
            and result.canary_uid is not None
            and result.position_anchor_units is not None
            and prior is not None
            and prior.scope == "CANARY_UID"
            and prior.canary_uid == result.canary_uid
            and prior.position_anchor_units is not None
            and instrument is not None
            and result.active_stop_order_count == 0
            and SubmitDenialReason.DATA_UNKNOWN not in reason_set
            and SubmitDenialReason.FOREIGN_ACTIVE_ORDER not in reason_set
            and set(states_by_id) == {item.idempotency_key for item in unresolved}
        )
        expected_units = None if not safe_to_attribute else prior.position_anchor_units
        terminal_states = {"FILLED", "CANCELLED", "REJECTED"}
        active_states = {"NEW", "PARTIALLYFILL", "PARTIALLY_FILLED"}
        validations: list[tuple[TradingSubmitReservationModel, AdmissionOrderState, bool]] = []
        if safe_to_attribute:
            for reservation in reservations:
                anchor_id = reservation.payload.get("anchor_reconciliation_id")
                if anchor_id != prior.reconciliation_id:
                    if reservation.state != SubmitReservationState.SETTLED.value:
                        safe_to_attribute = False
                        break
                    continue
                state = None
                if reservation.state == SubmitReservationState.SETTLED.value:
                    status = str(reservation.payload.get("last_reconciled_order_state", "SETTLED")).upper()
                    lots = int(reservation.payload.get("executed_lots", 0))
                    executed_amount = Decimal(str(reservation.payload.get("executed_amount", "0")))
                    executed_commission = Decimal(str(reservation.payload.get("executed_commission", "0")))
                    is_terminal = True
                else:
                    matches = states_by_id.get(reservation.idempotency_key, [])
                    if len(matches) != 1:
                        safe_to_attribute = False
                        break
                    state = matches[0]
                    status = state.status.upper()
                    lots = state.executed_lots if state.executed_lots is not None else -1
                    executed_amount = state.executed_amount
                    executed_commission = state.executed_commission
                    is_terminal = status in terminal_states
                previous_lots = int(reservation.payload.get("executed_lots", 0))
                previous_amount = Decimal(str(reservation.payload.get("executed_amount", "0")))
                previous_commission = Decimal(str(reservation.payload.get("executed_commission", "0")))
                if lots < previous_lots or lots > reservation.quantity_lots:
                    safe_to_attribute = False
                    break
                if state is not None and (
                    not state.attributable
                    or state.unknown_reason is not None
                    or state.broker_order_id is None
                    or state.instrument_uid != reservation.instrument_uid
                    or state.side is None
                    or state.side.value != reservation.side
                    or state.requested_lots != reservation.quantity_lots
                    or state.currency != "RUB"
                    or (status not in terminal_states and status not in active_states)
                    or (status == "REJECTED" and lots != 0)
                ):
                    safe_to_attribute = False
                    break
                if (
                    executed_amount is None
                    or not executed_amount.is_finite()
                    or executed_amount < previous_amount
                    or executed_amount < 0
                    or executed_commission is None
                    or not executed_commission.is_finite()
                    or executed_commission < previous_commission
                    or executed_commission < 0
                    or (lots == 0 and executed_amount != 0)
                    or (lots > 0 and executed_amount <= 0)
                ):
                    safe_to_attribute = False
                    break
                sign = Decimal(1) if reservation.side == "BUY" else Decimal(-1)
                anchor_lots = int(reservation.payload.get("anchor_executed_lots", 0))
                if lots < anchor_lots:
                    safe_to_attribute = False
                    break
                expected_units += sign * Decimal((lots - anchor_lots) * instrument.lot_size)
                if state is not None:
                    validations.append((reservation, state, is_terminal))

        active_state_count = sum(not is_terminal for _, _, is_terminal in validations)
        consistent = (
            safe_to_attribute
            and active_state_count == result.active_ordinary_order_count
            and expected_units == result.position_anchor_units
        )
        if consistent:
            for reservation, state, is_terminal in validations:
                reservation.updated_at = result.captured_at
                reservation.state = (
                    SubmitReservationState.SETTLED.value if is_terminal else SubmitReservationState.BROKER_ACTIVE.value
                )
                if is_terminal:
                    reservation.reserved_cash = Decimal(0)
                    reservation.reserved_position_units = Decimal(0)
                else:
                    original_reserved_cash = Decimal(str(reservation.payload.get("original_reserved_cash", "0")))
                    reservation.reserved_cash = (
                        max(Decimal(0), original_reserved_cash - state.executed_amount - state.executed_commission)
                        if reservation.side == "BUY"
                        else Decimal(0)
                    )
                    reservation.reserved_position_units = (
                        Decimal((reservation.quantity_lots - state.executed_lots) * instrument.lot_size)
                        if reservation.side == "SELL"
                        else Decimal(0)
                    )
                reservation.payload = {
                    **reservation.payload,
                    "last_reconciled_order_state": state.status,
                    "anchor_reconciliation_id": str(result.reconciliation_id),
                    "anchor_executed_lots": state.executed_lots,
                    "executed_lots": state.executed_lots,
                    "executed_amount": str(state.executed_amount),
                    "executed_commission": str(state.executed_commission),
                }
            for reservation in reservations:
                if reservation.payload.get("anchor_reconciliation_id") == prior.reconciliation_id:
                    reservation.payload = {
                        **reservation.payload,
                        "anchor_reconciliation_id": str(result.reconciliation_id),
                        "anchor_executed_lots": int(reservation.payload.get("executed_lots", 0)),
                    }
        else:
            for reservation in unresolved:
                reservation.state = SubmitReservationState.UNKNOWN.value
                reservation.updated_at = result.captured_at

        remaining = int(
            session.scalar(
                select(func.count())
                .select_from(TradingSubmitReservationModel)
                .where(
                    TradingSubmitReservationModel.user_broker_id == str(result.broker_id),
                    TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN")),
                )
            )
            or 0
        )
        reasons = list(result.reasons)
        status = result.status
        if not consistent:
            if SubmitDenialReason.DATA_UNKNOWN not in reasons:
                reasons.append(SubmitDenialReason.DATA_UNKNOWN)
            status = ReconciliationStatus.DATA_UNKNOWN
        return result.model_copy(
            update={
                "status": status,
                "can_arm": result.can_arm and remaining == 0 and result.active_ordinary_order_count == 0,
                "unresolved_reservation_count": remaining,
                "reasons": tuple(reasons),
            }
        )

    @staticmethod
    def _settle_age_policy_candidates(
        session: Session,
        result: ReconciliationResult,
        reservations: list[TradingSubmitReservationModel],
        *,
        order_states: tuple[AdmissionOrderState, ...],
    ) -> ReconciliationResult:
        states_by_id: dict[str, list[AdmissionOrderState]] = {}
        for state in order_states:
            states_by_id.setdefault(state.request_id, []).append(state)
        expected_ids = {item.idempotency_key for item in reservations}
        valid = (
            result.scope == "ACCOUNT"
            and result.status is ReconciliationStatus.ACCOUNT_ONLY
            and not result.reasons
            and result.active_ordinary_order_count == 0
            and result.active_stop_order_count == 0
            and set(states_by_id) == expected_ids
            and all(len(items) == 1 for items in states_by_id.values())
            and TradingAdmissionRepository._account_positions_match_ledger(
                session, str(result.broker_id), result.position_anchors
            )
        )
        updates: list[tuple[TradingSubmitReservationModel, AdmissionOrderState, bool]] = []
        if valid:
            for reservation in reservations:
                state = states_by_id[reservation.idempotency_key][0]
                if not TradingAdmissionRepository._age_policy_order_is_valid(
                    session, reservation, captured_at=result.captured_at
                ):
                    valid = False
                    break
                durable_order = session.scalar(
                    select(BrokerOrderModel).where(
                        BrokerOrderModel.user_broker_id == reservation.user_broker_id,
                        BrokerOrderModel.idempotency_key == reservation.idempotency_key,
                    )
                )
                if durable_order is None:
                    valid = False
                    break
                if state.unknown_reason == "REQUEST_NOT_FOUND" and not state.attributable:
                    if durable_order.state != "FAILED" or durable_order.terminal_at is not None:
                        valid = False
                        break
                    updates.append((reservation, state, True))
                    continue
                status = state.status.upper()
                lots = state.executed_lots
                amount = state.executed_amount
                commission = state.executed_commission
                prior_lots = int(reservation.payload.get("executed_lots", 0))
                prior_amount = Decimal(str(reservation.payload.get("executed_amount", "0")))
                prior_commission = Decimal(str(reservation.payload.get("executed_commission", "0")))
                durable_status = durable_order.state
                if (
                    not state.attributable
                    or state.unknown_reason is not None
                    or state.broker_order_id is None
                    or state.instrument_uid != reservation.instrument_uid
                    or state.side is None
                    or state.side.value != reservation.side
                    or state.requested_lots != reservation.quantity_lots
                    or lots is None
                    or lots < prior_lots
                    or lots > reservation.quantity_lots
                    or amount is None
                    or not amount.is_finite()
                    or amount < prior_amount
                    or commission is None
                    or not commission.is_finite()
                    or commission < prior_commission
                    or state.currency != "RUB"
                    or status not in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "EXECUTED"}
                    or (status != durable_status and not (status == "EXECUTED" and durable_status == "FILLED"))
                    or durable_order.terminal_at is None
                    or (lots == 0 and amount != 0)
                    or (lots > 0 and amount <= 0)
                ):
                    valid = False
                    break
                updates.append((reservation, state, False))
        if valid:
            for reservation, state, broker_unknown in updates:
                lots = int(reservation.payload.get("executed_lots", 0)) if broker_unknown else state.executed_lots
                amount = (
                    Decimal(str(reservation.payload.get("executed_amount", "0")))
                    if broker_unknown
                    else state.executed_amount
                )
                commission = (
                    Decimal(str(reservation.payload.get("executed_commission", "0")))
                    if broker_unknown
                    else state.executed_commission
                )
                reservation.state = SubmitReservationState.SETTLED.value
                reservation.reserved_cash = Decimal(0)
                reservation.reserved_position_units = Decimal(0)
                reservation.updated_at = result.captured_at
                reservation.payload = {
                    **reservation.payload,
                    "settlement_reason": "AGE_POLICY",
                    "broker_outcome_unknown": broker_unknown,
                    "last_reconciled_order_state": "UNKNOWN" if broker_unknown else state.status,
                    "anchor_reconciliation_id": str(result.reconciliation_id),
                    "anchor_executed_lots": lots,
                    "executed_lots": lots,
                    "executed_amount": str(amount),
                    "executed_commission": str(commission),
                }
            return result.model_copy(update={"can_arm": True, "unresolved_reservation_count": 0})
        reasons = list(result.reasons)
        if SubmitDenialReason.DATA_UNKNOWN not in reasons:
            reasons.append(SubmitDenialReason.DATA_UNKNOWN)
        return result.model_copy(
            update={
                "status": ReconciliationStatus.DATA_UNKNOWN,
                "can_arm": False,
                "unresolved_reservation_count": len(reservations),
                "reasons": tuple(reasons),
            }
        )

    def get_submit_context(self, broker_id: str, automation_id: str, instrument_uid: str) -> AdmissionSubmitContext:
        with self._factory() as session:
            broker = session.get(UserBrokerModel, broker_id)
            admission = session.get(TradingAccountAdmissionModel, broker_id)
            automation = session.scalar(
                select(TradingAutomationModel).where(
                    TradingAutomationModel.user_broker_id == broker_id,
                    TradingAutomationModel.id == automation_id,
                )
            )
            instrument = session.scalar(
                select(BrokerInstrumentModel).where(
                    BrokerInstrumentModel.user_broker_id == broker_id,
                    BrokerInstrumentModel.external_instrument_id == instrument_uid,
                )
            )
            if broker is None or broker.external_account_id is None or admission is None:
                raise ValueError("Trading admission account is unavailable.")
            if automation is None or instrument is None:
                raise ValueError("Submit automation or instrument is outside the account scope.")
            if automation.instrument_id != instrument.id:
                raise ValueError("Submit automation instrument does not match the requested UID.")
            owner = (
                session.get(WorkerRuntimeInstanceModel, admission.owner_runtime_instance_id)
                if admission.owner_runtime_instance_id is not None
                else None
            )
            baseline_model = (
                session.get(TradingAdmissionBaselineModel, admission.baseline_id)
                if admission.baseline_id is not None
                else None
            )
            reconciliation_model = (
                session.get(TradingReconciliationModel, admission.reconciliation_id)
                if admission.reconciliation_id is not None
                else None
            )
            reservations = tuple(
                AdmissionReservationRecord(
                    idempotency_key=row.idempotency_key,
                    state=row.state,
                    side=row.side,
                    instrument_uid=row.instrument_uid,
                    requested_amount=row.requested_amount,
                    reserved_cash=row.reserved_cash,
                    reserved_position_units=row.reserved_position_units,
                    quantity_lots=row.quantity_lots,
                    runtime_instance_id=row.runtime_instance_id,
                    generation=row.generation,
                    anchor_reconciliation_id=row.payload.get("anchor_reconciliation_id"),
                    anchor_executed_lots=int(row.payload.get("anchor_executed_lots", 0)),
                    executed_lots=int(row.payload.get("executed_lots", 0)),
                    executed_amount=Decimal(str(row.payload.get("executed_amount", "0"))),
                    executed_commission=Decimal(str(row.payload.get("executed_commission", "0"))),
                )
                for row in session.scalars(
                    select(TradingSubmitReservationModel).where(
                        TradingSubmitReservationModel.user_broker_id == broker_id,
                        TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN", "SETTLED")),
                    )
                )
            )
            return AdmissionSubmitContext(
                account_id=broker.external_account_id,
                environment=broker.environment,
                revision=admission.revision,
                owner_runtime_instance_id=admission.owner_runtime_instance_id,
                owner_generation=admission.owner_generation,
                lease_expires_at=admission.lease_expires_at,
                arm_revision=admission.arm_revision,
                arm_expires_at=admission.arm_expires_at,
                admission_status=admission.admission_status,
                recovery_only=admission.recovery_only,
                limits=None if admission.limits is None else AdmissionLimits.model_validate(admission.limits),
                configuration_fingerprint=admission.configuration_fingerprint,
                baseline=None if baseline_model is None else AdmissionBaseline.model_validate(baseline_model.payload),
                reconciliation=(
                    None
                    if reconciliation_model is None
                    else ReconciliationResult.model_validate(reconciliation_model.payload)
                ),
                automation_id=automation.id,
                automation_state=automation.state,
                instrument_uid=instrument.external_instrument_id,
                instrument_type=instrument.instrument_type,
                currency=instrument.currency,
                lot_size=instrument.lot_size,
                min_price_increment=instrument.min_price_increment,
                instrument_active=instrument.is_active,
                api_trade_available=instrument.api_trade_available,
                owner_access_mode=None if owner is None else owner.access_mode,
                owner_strategy_enabled=False if owner is None else owner.strategy_enabled,
                reservations=reservations,
                reservation_digest=self._reservation_digest(session, broker_id),
            )

    @staticmethod
    def _reservation_digest(session: Session, broker_id: str) -> str:
        rows = session.scalars(
            select(TradingSubmitReservationModel)
            .where(
                TradingSubmitReservationModel.user_broker_id == broker_id,
                TradingSubmitReservationModel.state.in_(
                    ("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN", "SETTLED", "CONFIRMED_NOT_SENT")
                ),
            )
            .order_by(TradingSubmitReservationModel.idempotency_key)
        )
        contents = [
            {
                "key": row.idempotency_key,
                "state": row.state,
                "cash": str(row.reserved_cash),
                "position_units": str(row.reserved_position_units),
                "requested_amount": str(row.requested_amount),
                "quantity_lots": row.quantity_lots,
                "limit_price": str(row.limit_price),
                "request_fingerprint": row.request_fingerprint,
                "runtime_instance_id": row.runtime_instance_id,
                "generation": row.generation,
                "configuration_fingerprint": row.configuration_fingerprint,
                "permit_id": row.permit_id,
                "anchor_reconciliation_id": row.payload.get("anchor_reconciliation_id"),
                "anchor_executed_lots": row.payload.get("anchor_executed_lots", 0),
                "executed_lots": row.payload.get("executed_lots", 0),
                "executed_amount": row.payload.get("executed_amount", "0"),
                "executed_commission": row.payload.get("executed_commission", "0"),
                "side": row.side,
                "instrument_uid": row.instrument_uid,
            }
            for row in rows
        ]
        encoded = json.dumps(contents, sort_keys=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    def get_view(self, broker_id: str, *, now: datetime) -> AdmissionView:
        with self._factory() as session:
            broker = session.get(UserBrokerModel, broker_id)
            admission = session.get(TradingAccountAdmissionModel, broker_id)
            if (
                broker is None
                or not self._environment_matches(broker)
                or broker.external_account_id is None
                or admission is None
            ):
                raise ValueError("Trading admission has not been registered for this account.")
            baseline_model = (
                session.get(TradingAdmissionBaselineModel, admission.baseline_id)
                if admission.baseline_id is not None
                else None
            )
            reconciliation_model = (
                session.get(TradingReconciliationModel, admission.reconciliation_id)
                if admission.reconciliation_id is not None
                else None
            )
            baseline = None if baseline_model is None else AdmissionBaseline.model_validate(baseline_model.payload)
            reconciliation = (
                None
                if reconciliation_model is None
                else ReconciliationResult.model_validate(reconciliation_model.payload)
            )
            unresolved = int(
                session.scalar(
                    select(func.count())
                    .select_from(TradingSubmitReservationModel)
                    .where(
                        TradingSubmitReservationModel.user_broker_id == broker_id,
                        TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN")),
                    )
                )
                or 0
            )
            owner = (
                session.get(WorkerRuntimeInstanceModel, admission.owner_runtime_instance_id)
                if admission.owner_runtime_instance_id is not None
                else None
            )
            limits = None if admission.limits is None else AdmissionLimits.model_validate(admission.limits)
            reasons = self._effective_reasons(admission, owner, baseline, reconciliation, now, unresolved)
            return AdmissionView(
                broker_id=broker_id,
                account_id=broker.external_account_id,
                revision=admission.revision,
                owner_runtime_instance_id=admission.owner_runtime_instance_id,
                owner_generation=admission.owner_generation,
                recovery_runtime_instance_id=admission.recovery_runtime_instance_id,
                admission_status=AdmissionStatus(admission.admission_status),
                arm_revision=admission.arm_revision,
                lease_expires_at=admission.lease_expires_at,
                arm_expires_at=admission.arm_expires_at,
                limits=limits,
                configuration_fingerprint=admission.configuration_fingerprint,
                baseline=baseline,
                reconciliation=reconciliation,
                effective_can_submit=not reasons,
                effective_denial_reasons=tuple(reasons),
                unresolved_reservation_count=unresolved,
                outstanding_order_count=(
                    0
                    if reconciliation is None
                    else reconciliation.active_ordinary_order_count + reconciliation.active_stop_order_count
                ),
                recovery_only=admission.recovery_only,
            )

    def update_limits(
        self, broker_id: str, request: UpdateLimitsRequest, *, actor: str, now: datetime
    ) -> AdmissionView:
        with self._factory.begin() as session:
            broker = session.get(UserBrokerModel, broker_id, with_for_update=True)
            admission = session.get(TradingAccountAdmissionModel, broker_id, with_for_update=True)
            if (
                broker is None
                or broker.environment != "PROD"
                or not self._environment_matches(broker)
                or broker.state != "ACTIVE"
                or broker.archived_at is not None
                or broker.external_account_id is None
                or admission is None
                or admission.revision != request.expected_revision
            ):
                raise AdmissionConflictError("Admission account or revision changed before limits update.")
            admission.limits = request.limits.model_dump(mode="json")
            admission.revision += 1
            self._event(
                session,
                broker_id,
                "LIMITS_UPDATED",
                now,
                {"revision": admission.revision, "limits": request.limits.model_dump(mode="json"), "actor": actor},
            )
        return self.get_view(broker_id, now=now)

    def arm(self, request: ArmRequest, *, broker_id: str, now: datetime, actor: str) -> AdmissionView:
        with self._factory.begin() as session:
            broker = session.get(UserBrokerModel, broker_id, with_for_update=True)
            admission = session.get(TradingAccountAdmissionModel, broker_id, with_for_update=True)
            if (
                broker is None
                or not self._environment_matches(broker)
                or admission is None
                or admission.revision != request.expected_revision
            ):
                raise AdmissionConflictError("Admission revision changed before arm.")
            owner = session.get(WorkerRuntimeInstanceModel, admission.owner_runtime_instance_id)
            baseline = session.get(TradingAdmissionBaselineModel, str(request.baseline_id))
            reconciliation = session.get(TradingReconciliationModel, str(request.reconciliation_id))
            moscow_day_end = datetime.combine(
                now.astimezone(ZoneInfo("Europe/Moscow")).date() + timedelta(days=1),
                time.min,
                ZoneInfo("Europe/Moscow"),
            ).astimezone(now.tzinfo)
            if (
                broker.environment != "PROD"
                or owner is None
                or owner.access_mode != "TRADE"
                or not owner.strategy_enabled
                or owner.runtime_instance_id != str(request.runtime_instance_id)
                or owner.registration_status != RuntimeRegistrationStatus.OWNER.value
                or owner.generation != admission.owner_generation
                or admission.baseline_id != str(request.baseline_id)
                or admission.reconciliation_id != str(request.reconciliation_id)
                or admission.lease_expires_at is None
                or admission.lease_expires_at <= now
                or baseline is None
                or baseline.user_broker_id != broker_id
                or admission.baseline_id != baseline.baseline_id
                or baseline.moscow_day != now.astimezone(ZoneInfo("Europe/Moscow")).date()
                or reconciliation is None
                or reconciliation.user_broker_id != broker_id
                or reconciliation.owner_runtime_instance_id != owner.runtime_instance_id
                or reconciliation.owner_generation != owner.generation
                or not reconciliation.can_arm
                or reconciliation.valid_until <= now
                or reconciliation.configuration_fingerprint != request.configuration_fingerprint
                or request.expires_at <= now
                or request.expires_at > moscow_day_end
            ):
                raise AdmissionConflictError("Admission prerequisites are not satisfied.")
            if reconciliation.active_ordinary_order_count or reconciliation.active_stop_order_count:
                raise AdmissionConflictError("Active broker orders prevent admission arm.")
            unresolved = (
                session.scalar(
                    select(func.count())
                    .select_from(TradingSubmitReservationModel)
                    .where(
                        TradingSubmitReservationModel.user_broker_id == broker_id,
                        TradingSubmitReservationModel.state.in_(("IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN")),
                    )
                )
                or 0
            )
            if unresolved or reconciliation.unresolved_reservation_count:
                raise AdmissionConflictError("Unresolved submit reservations prevent admission arm.")
            admission.revision += 1
            admission.arm_revision = admission.revision
            admission.admission_status = AdmissionStatus.ARMED.value
            admission.arm_expires_at = request.expires_at
            admission.limits = request.limits.model_dump(mode="json")
            admission.configuration_fingerprint = request.configuration_fingerprint
            admission.baseline_id = str(request.baseline_id)
            admission.reconciliation_id = str(request.reconciliation_id)
            self._event(
                session,
                broker_id,
                "ADMISSION_ARMED",
                now,
                {
                    "actor": actor,
                    "arm_revision": admission.revision,
                    "configuration_fingerprint": request.configuration_fingerprint,
                },
            )
        return self.get_view(broker_id, now=now)

    def owner_stopped(
        self,
        request: OwnerStoppedRequest,
        *,
        broker_id: str,
        now: datetime,
        actor: str,
        lease_expires_at: datetime,
    ) -> AdmissionView:
        with self._factory.begin() as session:
            broker = session.get(UserBrokerModel, broker_id, with_for_update=True)
            admission = session.get(TradingAccountAdmissionModel, broker_id, with_for_update=True)
            if broker is None or admission is None or admission.revision != request.expected_revision:
                raise AdmissionConflictError("Admission revision changed before owner transfer.")
            old_owner = session.get(WorkerRuntimeInstanceModel, str(request.old_runtime_instance_id))
            candidate = session.get(WorkerRuntimeInstanceModel, str(request.candidate_runtime_instance_id))
            if (
                not self._environment_matches(broker)
                or old_owner is None
                or candidate is None
                or admission.owner_runtime_instance_id != old_owner.runtime_instance_id
                or admission.owner_generation != request.old_generation
                or old_owner.generation != request.old_generation
                or old_owner.registration_status != RuntimeRegistrationStatus.OWNER.value
                or candidate.user_broker_id != broker_id
                or candidate.registration_status != RuntimeRegistrationStatus.PENDING.value
                or candidate.runtime_instance_id == old_owner.runtime_instance_id
                or request.stopped_at > now
            ):
                raise AdmissionConflictError("Owner stop attestation does not match the current account owner.")
            old_owner.registration_status = "RETIRED"
            old_owner.retired_at = now
            old_owner.lease_expires_at = None
            candidate.registration_status = RuntimeRegistrationStatus.OWNER.value
            candidate.generation = request.old_generation + 1
            candidate.lease_expires_at = lease_expires_at
            candidate.last_seen_at = now
            admission.owner_runtime_instance_id = candidate.runtime_instance_id
            admission.owner_generation = candidate.generation
            admission.recovery_runtime_instance_id = candidate.runtime_instance_id
            admission.recovery_only = True
            admission.lease_expires_at = lease_expires_at
            admission.revision += 1
            admission.admission_status = AdmissionStatus.DISARMED.value
            admission.arm_revision = None
            admission.arm_expires_at = None
            if candidate.access_mode == "TRADE" and candidate.strategy_enabled:
                # A controlled owner transfer changes generation, not the user's
                # IN_WORK intent. Bind the new owner to the currently stored broker scope.
                admission.configuration_fingerprint = self._current_configuration_fingerprint(
                    broker, candidate.access_mode
                )
            else:
                self._hold_account_automations(session, broker_id, "TRADING_OWNER_CHANGED")
                admission.configuration_fingerprint = None
            self._event(
                session,
                broker_id,
                "OWNER_STOPPED_AND_RECOVERY_ASSIGNED",
                now,
                {
                    "actor": actor,
                    "old_runtime_instance_id": old_owner.runtime_instance_id,
                    "old_generation": request.old_generation,
                    "candidate_runtime_instance_id": candidate.runtime_instance_id,
                    "new_generation": candidate.generation,
                    "stopped_at": request.stopped_at.isoformat(),
                    "evidence_ref": request.evidence_ref,
                },
            )
        return self.get_view(broker_id, now=now)

    def revoke(self, request: RevokeRequest, *, broker_id: str, now: datetime, actor: str) -> AdmissionView:
        with self._factory.begin() as session:
            broker = session.get(UserBrokerModel, broker_id, with_for_update=True)
            admission = session.get(TradingAccountAdmissionModel, broker_id, with_for_update=True)
            if (
                not self._environment_matches(broker)
                or admission is None
                or admission.revision != request.expected_revision
            ):
                raise AdmissionConflictError("Admission revision changed before revoke.")
            admission.revision += 1
            admission.admission_status = AdmissionStatus.DISARMED.value
            admission.arm_revision = None
            admission.arm_expires_at = None
            self._hold_account_automations(session, broker_id, "TRADING_ADMISSION_REVOKED")
            self._event(session, broker_id, "ADMISSION_REVOKED", now, {"actor": actor, "reason": request.reason})
        return self.get_view(broker_id, now=now)

    @staticmethod
    def _hold_account_automations(session: Session, broker_id: str, reason: str) -> None:
        automations = session.scalars(
            select(TradingAutomationModel)
            .where(
                TradingAutomationModel.user_broker_id == broker_id,
                TradingAutomationModel.state.in_(("IN_QUEUE", "OPENING", "IN_WORK")),
                TradingAutomationModel.closed_at.is_(None),
            )
            .with_for_update()
        )
        for automation in automations:
            automation.suspended_from_state = automation.state
            automation.state = "HOLD"
            automation.hold_reason = reason
            automation.resume_requested = False
            automation.revision += 1

    @staticmethod
    def _effective_reasons(admission, owner, baseline, reconciliation, now, unresolved: int):
        reasons: list[SubmitDenialReason] = []
        if (
            owner is None
            or owner.registration_status != "OWNER"
            or admission.owner_generation != owner.generation
            or owner.access_mode != "TRADE"
            or not owner.strategy_enabled
        ):
            reasons.append(SubmitDenialReason.NOT_OWNER)
        if admission.lease_expires_at is None or admission.lease_expires_at <= now:
            reasons.append(SubmitDenialReason.LEASE_EXPIRED)
        if baseline is None or baseline.moscow_day != now.astimezone(ZoneInfo("Europe/Moscow")).date():
            reasons.append(SubmitDenialReason.BASELINE_REQUIRED)
        if admission.limits is None:
            reasons.append(SubmitDenialReason.LIMITS_NOT_CONFIGURED)
        return tuple(dict.fromkeys(reasons))

    @staticmethod
    def _event(
        session,
        broker_id: str,
        event_type: str,
        now: datetime,
        payload: dict[str, object],
        *,
        runtime_instance_id: str | None = None,
        generation: int | None = None,
    ) -> None:
        session.add(
            TradingAdmissionEventModel(
                event_id=str(uuid4()),
                user_broker_id=broker_id,
                runtime_instance_id=runtime_instance_id,
                generation=generation,
                event_type=event_type,
                occurred_at=now,
                payload=payload,
            )
        )

    @staticmethod
    def _registration_result(
        runtime: WorkerRuntimeInstanceModel,
        admission: TradingAccountAdmissionModel | None,
    ) -> RuntimeRegistrationResult:
        return RuntimeRegistrationResult(
            runtime_instance_id=runtime.runtime_instance_id,
            generation=runtime.generation,
            registration_status=RuntimeRegistrationStatus(runtime.registration_status),
            admission_status=(
                AdmissionStatus(admission.admission_status) if admission is not None else AdmissionStatus.DISARMED
            ),
            lease_expires_at=runtime.lease_expires_at,
        )
