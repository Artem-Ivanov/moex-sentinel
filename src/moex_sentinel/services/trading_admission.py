"""Business rules for durable ownership and guarded trade admission."""

import hashlib
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from functools import partial
from time import perf_counter_ns
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from moex_sentinel.domain.daily_admission_preparation import (
    DailyAdmissionPreparationReason,
    DailyAdmissionPreparationStatus,
    DailyAdmissionPreparationView,
)
from moex_sentinel.domain.trading_admission import AdmissionConflictError
from moex_sentinel.domain.trading_summary import (
    EXECUTED_OPERATION_STATES,
    INPUT_OPERATION_TYPES,
    OUTPUT_OPERATION_TYPES,
)
from moex_sentinel.services.sync_execution import run_sync
from moex_sentinel.services.trading_admission_ports import (
    AdmissionOrder,
    AdmissionOrderState,
    AdmissionRiskDataPort,
    AdmissionRiskSnapshot,
)
from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.time import utc_now_ms
from sentinel_contracts.trading_admission import (
    AdmissionBaseline,
    AdmissionView,
    ArmRequest,
    CaptureBaselineRequest,
    OwnerStoppedRequest,
    PendingRuntimeCandidate,
    ReconciliationPositionAnchor,
    ReconciliationRequest,
    ReconciliationResult,
    ReconciliationStatus,
    RevokeRequest,
    RuntimeRegistrationRequest,
    RuntimeRegistrationResult,
    SubmitAuthorizationRequest,
    SubmitAuthorizationResult,
    SubmitDenialReason,
    SubmitDenied,
    SubmitPermit,
    SubmitReservationState,
    UpdateLimitsRequest,
)

OWNER_LEASE_SECONDS = 30


class TradingAdmissionService:
    def __init__(
        self,
        repository,
        *,
        risk: AdmissionRiskDataPort | None = None,
        connection_resolver: Callable[[str], BrokerConnection] | None = None,
        clock: Callable[[], datetime] = utc_now_ms,
        owner_lease_seconds: int = OWNER_LEASE_SECONDS,
    ) -> None:
        if owner_lease_seconds <= 0:
            raise ValueError("Owner lease duration must be positive.")
        self._repository = repository
        self._risk = risk
        self._connection_resolver = connection_resolver
        self._clock = clock
        self._owner_lease_seconds = owner_lease_seconds

    def register_runtime(self, request: RuntimeRegistrationRequest, runtime_secret: str) -> RuntimeRegistrationResult:
        if re.fullmatch(r"[0-9a-f]{64}", runtime_secret) is None:
            raise ValueError("Runtime secret has invalid format.")
        now = self._clock()
        return self._repository.register_runtime(
            request,
            secret_digest=hashlib.sha256(runtime_secret.encode("ascii")).hexdigest(),
            now=now,
            lease_expires_at=now + timedelta(seconds=self._owner_lease_seconds),
        )

    def view(self, broker_id: str) -> AdmissionView:
        return self._repository.get_view(broker_id, now=self._clock())

    def expects_trading(self, broker_id: UUID) -> bool:
        return self._repository.expects_trading(str(broker_id))

    def pending_runtime_candidate(
        self, broker_id: str, worker_id: str, registered_after: datetime
    ) -> PendingRuntimeCandidate | None:
        return self._repository.pending_runtime_candidate(broker_id, worker_id, registered_after)

    def update_limits(self, broker_id: str, request: UpdateLimitsRequest, *, actor: str) -> AdmissionView:
        return self._repository.update_limits(broker_id, request, actor=actor, now=self._clock())

    def renew_owner(self, runtime_instance_id: UUID, generation: int) -> RuntimeRegistrationResult:
        return self._repository.renew_owner(
            str(runtime_instance_id),
            generation,
            final_clock=self._clock,
            lease_duration=timedelta(seconds=self._owner_lease_seconds),
        )

    def submit_attempt(
        self, broker_id: str, idempotency_key: str, *, original_runtime_instance_id: UUID | None, generation: int | None
    ):
        return self._repository.submit_attempt(
            broker_id,
            idempotency_key,
            original_runtime_instance_id=(
                None if original_runtime_instance_id is None else str(original_runtime_instance_id)
            ),
            original_generation=generation,
        )

    async def capture_baseline(
        self, broker_id: str, request: CaptureBaselineRequest, *, accepted_by: str
    ) -> AdmissionBaseline:
        risk = self._require_risk()
        now = self._clock()
        account_id, environment = await run_sync(partial(self._repository.get_scope, broker_id))
        if environment != "PROD" or request.day != now.astimezone(ZoneInfo("Europe/Moscow")).date():
            raise ValueError("Baseline can only be captured for the current Moscow day in PROD.")
        start = datetime.combine(request.day, time.min, ZoneInfo("Europe/Moscow")).astimezone(UTC)
        connection = await run_sync(partial(self._connection, broker_id))
        snapshot = await risk.collect(connection, instrument_uid=None, operations_from=start)
        accepted_at = self._clock()
        if (
            request.day != accepted_at.astimezone(ZoneInfo("Europe/Moscow")).date()
            or snapshot.valid_until <= accepted_at
            or snapshot.account_id != account_id
            or snapshot.equity is None
            or snapshot.equity_currency != "RUB"
            or snapshot.unknown_reasons
            or not snapshot.operations_complete
            or snapshot.operations_through != snapshot.captured_at
            or snapshot.captured_at < snapshot.capture_started_at
            or snapshot.captured_at > accepted_at
        ):
            raise ValueError("Authoritative account snapshot is incomplete or outside the expected scope.")
        cash_flow = Decimal(0)
        for operation in snapshot.operations:
            if operation.operation_type in INPUT_OPERATION_TYPES | OUTPUT_OPERATION_TYPES and (
                operation.occurred_at > snapshot.operations_through
                or snapshot.capture_started_at <= operation.occurred_at <= snapshot.captured_at
            ):
                raise ValueError("External cash flow overlaps the account snapshot window.")
            if operation.account_id != account_id:
                raise ValueError("Operation account scope does not match the configured account.")
            if operation.currency not in (None, "RUB"):
                raise ValueError("External cash flow currency is unknown or unsupported.")
            if operation.operation_type in INPUT_OPERATION_TYPES | {"INPUT"}:
                if operation.payment is None or operation.state not in EXECUTED_OPERATION_STATES:
                    raise ValueError("External cash input is not conclusively executed.")
                cash_flow += abs(operation.payment)
            elif operation.operation_type in OUTPUT_OPERATION_TYPES | {"OUTPUT"}:
                if operation.payment is None or operation.state not in EXECUTED_OPERATION_STATES:
                    raise ValueError("External cash output is not conclusively executed.")
                cash_flow -= abs(operation.payment)
            elif operation.operation_type.startswith(
                ("INPUT", "OUTPUT", "OPERATION_TYPE_INPUT", "OPERATION_TYPE_OUTPUT")
            ):
                raise ValueError("Unrecognized external cash flow prevents baseline acceptance.")
        baseline = AdmissionBaseline(
            baseline_id=uuid4(),
            broker_id=UUID(broker_id),
            account_id=account_id,
            moscow_day=request.day,
            timezone="Europe/Moscow",
            currency="RUB",
            equity=snapshot.equity,
            net_external_cash_flow=cash_flow,
            capture_started_at=snapshot.capture_started_at,
            capture_finished_at=snapshot.captured_at,
            source="GET_PORTFOLIO",
            operations_through=snapshot.operations_through,
            accepted_by=accepted_by,
            accepted_at=max(accepted_at, snapshot.captured_at),
            policy="FIRST_ACCEPTED_ACCOUNT_EQUITY",
        )
        return await run_sync(
            partial(
                self._repository.save_baseline,
                baseline,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        )

    async def prepare_daily(self, broker_id: str) -> DailyAdmissionPreparationView:
        """Accept today's loss baseline idempotently; trade admission remains order-scoped."""
        now = self._clock()
        day = now.astimezone(ZoneInfo("Europe/Moscow")).date()
        try:
            account_id, environment = await run_sync(partial(self._repository.get_scope, broker_id))
            view = await run_sync(partial(self._repository.get_view, broker_id, now=now))
        except ValueError:
            return self._daily_empty(
                DailyAdmissionPreparationStatus.BLOCKED, DailyAdmissionPreparationReason.SCOPE_UNAVAILABLE
            )
        if environment != "PROD":
            return self._daily_empty(
                DailyAdmissionPreparationStatus.BLOCKED, DailyAdmissionPreparationReason.SCOPE_UNAVAILABLE
            )
        if view.owner_runtime_instance_id is None or view.owner_generation is None:
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.OWNER_MISSING,
                moscow_day=day,
            )
        if view.lease_expires_at is None or view.lease_expires_at <= now:
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.LEASE_EXPIRED,
                moscow_day=day,
            )
        risk = self._require_risk()
        connection = await run_sync(partial(self._connection, broker_id))
        if connection.environment != "PROD" or connection.access_mode != "TRADE":
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.SCOPE_UNAVAILABLE,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        if view.configuration_fingerprint and view.configuration_fingerprint != connection.configuration_fingerprint:
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.CONFIGURATION_CHANGED,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )

        if view.baseline is None or view.baseline.moscow_day != day:
            start = datetime.combine(day, time.min, ZoneInfo("Europe/Moscow")).astimezone(UTC)
            snapshot = await risk.collect(connection, instrument_uid=None, operations_from=start)
            accepted_at = self._clock()
            try:
                baseline = self._daily_baseline(
                    broker_id,
                    account_id,
                    day,
                    snapshot,
                    accepted_at=accepted_at,
                )
                baseline = await run_sync(
                    partial(
                        self._repository.ensure_daily_baseline,
                        baseline,
                        configuration_fingerprint=connection.configuration_fingerprint,
                        expected_owner_runtime_instance_id=str(view.owner_runtime_instance_id),
                        expected_generation=view.owner_generation,
                        snapshot_valid_until=snapshot.valid_until,
                        final_clock=self._clock,
                    )
                )
            except (ValueError, AdmissionConflictError):
                return self._daily_with_view(
                    view,
                    DailyAdmissionPreparationStatus.BLOCKED,
                    DailyAdmissionPreparationReason.SNAPSHOT_INVALID,
                    moscow_day=day,
                    configuration_fingerprint=connection.configuration_fingerprint,
                )
            view = await run_sync(partial(self._repository.get_view, broker_id, now=self._clock()))
            if view.baseline is None or view.baseline.baseline_id != baseline.baseline_id:
                return self._daily_with_view(
                    view,
                    DailyAdmissionPreparationStatus.WAITING,
                    DailyAdmissionPreparationReason.MOSCOW_DAY_CHANGED,
                    moscow_day=day,
                    configuration_fingerprint=connection.configuration_fingerprint,
                )

        if view.baseline is None or view.baseline.moscow_day != day:
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.WAITING,
                DailyAdmissionPreparationReason.BASELINE_REQUIRED,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        observation = DailyAdmissionPreparationView(
            moscow_day=day,
            captured_at=self._clock(),
            status=DailyAdmissionPreparationStatus.BASELINE_ACCEPTED,
            reason=None,
            baseline_id=view.baseline.baseline_id,
            owner_runtime_instance_id=view.owner_runtime_instance_id,
            owner_generation=view.owner_generation,
            configuration_fingerprint=connection.configuration_fingerprint,
            reconciliation_id=None,
            reconciliation_valid_until=None,
            suggested_limits=None,
            risk_reasons=(),
        )
        prepared = await self.validate_daily_preparation(broker_id, observation)
        if prepared.status is DailyAdmissionPreparationStatus.BASELINE_ACCEPTED and await run_sync(
            partial(self._repository.needs_age_policy_recovery, broker_id)
        ):
            current = await run_sync(partial(self._repository.get_view, broker_id, now=self._clock()))
            if current.owner_runtime_instance_id is not None and current.owner_generation is not None:
                with suppress(AdmissionConflictError, ValueError):
                    await self.reconcile(
                        broker_id,
                        ReconciliationRequest(expected_revision=current.revision, canary_uid=None),
                        runtime_instance_id=(
                            current.owner_runtime_instance_id
                            if isinstance(current.owner_runtime_instance_id, UUID)
                            else UUID(current.owner_runtime_instance_id)
                        ),
                        generation=current.owner_generation,
                        automatic=True,
                    )
                    # Recovery remains gated; the next daily preparation retries with a fresh snapshot.
        return prepared

    async def validate_daily_preparation(
        self, broker_id: str, observation: DailyAdmissionPreparationView
    ) -> DailyAdmissionPreparationView:
        """Revalidate only the automatic daily loss baseline observation."""
        try:
            account_id, environment = await run_sync(partial(self._repository.get_scope, broker_id))
            connection = await run_sync(partial(self._connection, broker_id))
            view = await run_sync(partial(self._repository.get_view, broker_id, now=self._clock()))
        except ValueError:
            return self._daily_empty(
                DailyAdmissionPreparationStatus.WAITING, DailyAdmissionPreparationReason.CORE_UNAVAILABLE
            )
        now = self._clock()
        day = now.astimezone(ZoneInfo("Europe/Moscow")).date()
        if observation.moscow_day != day:
            return self._daily_empty(
                DailyAdmissionPreparationStatus.WAITING, DailyAdmissionPreparationReason.MOSCOW_DAY_CHANGED
            )
        if environment != "PROD" or connection.environment != "PROD":
            return self._daily_empty(
                DailyAdmissionPreparationStatus.BLOCKED, DailyAdmissionPreparationReason.SCOPE_UNAVAILABLE
            )
        if view.owner_runtime_instance_id is None or view.owner_generation is None:
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.OWNER_MISSING,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        if observation.owner_runtime_instance_id is not None and (
            observation.owner_runtime_instance_id != view.owner_runtime_instance_id
            or observation.owner_generation != view.owner_generation
        ):
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.OWNER_MISMATCH,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        if view.lease_expires_at is None or view.lease_expires_at <= now:
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.LEASE_EXPIRED,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        if observation.configuration_fingerprint != connection.configuration_fingerprint:
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.CONFIGURATION_CHANGED,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        if observation.status is not DailyAdmissionPreparationStatus.BASELINE_ACCEPTED:
            return observation
        if (
            view.baseline is None
            or view.baseline.moscow_day != day
            or observation.baseline_id != view.baseline.baseline_id
        ):
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.WAITING,
                DailyAdmissionPreparationReason.BASELINE_REQUIRED,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        return observation

    def _daily_baseline(self, broker_id, account_id, day, snapshot, *, accepted_at):
        if (
            snapshot.account_id != account_id
            or snapshot.equity is None
            or snapshot.equity_currency != "RUB"
            or snapshot.unknown_reasons
            or not snapshot.operations_complete
            or snapshot.operations_through != snapshot.captured_at
            or snapshot.capture_started_at > snapshot.captured_at
            or snapshot.captured_at > accepted_at
            or snapshot.valid_until <= accepted_at
        ):
            raise ValueError("Daily baseline snapshot is incomplete or expired.")
        cash_flow = Decimal(0)
        for operation in snapshot.operations:
            if operation.account_id != account_id or operation.currency not in (None, "RUB"):
                raise ValueError("Daily baseline operation scope or currency is invalid.")
            if operation.operation_type in INPUT_OPERATION_TYPES | OUTPUT_OPERATION_TYPES and (
                operation.occurred_at > snapshot.operations_through
                or snapshot.capture_started_at <= operation.occurred_at <= snapshot.captured_at
            ):
                raise ValueError("Daily baseline cash flow overlaps the snapshot window.")
            if operation.operation_type in INPUT_OPERATION_TYPES | {"INPUT"}:
                if operation.payment is None or operation.state not in EXECUTED_OPERATION_STATES:
                    raise ValueError("Daily baseline cash input is inconclusive.")
                cash_flow += abs(operation.payment)
            elif operation.operation_type in OUTPUT_OPERATION_TYPES | {"OUTPUT"}:
                if operation.payment is None or operation.state not in EXECUTED_OPERATION_STATES:
                    raise ValueError("Daily baseline cash output is inconclusive.")
                cash_flow -= abs(operation.payment)
            elif operation.operation_type.startswith(
                ("INPUT", "OUTPUT", "OPERATION_TYPE_INPUT", "OPERATION_TYPE_OUTPUT")
            ):
                raise ValueError("Daily baseline has an unknown cash flow type.")
        return AdmissionBaseline(
            baseline_id=uuid4(),
            broker_id=UUID(broker_id),
            account_id=account_id,
            moscow_day=day,
            timezone="Europe/Moscow",
            currency="RUB",
            equity=snapshot.equity,
            net_external_cash_flow=cash_flow,
            capture_started_at=snapshot.capture_started_at,
            capture_finished_at=snapshot.captured_at,
            source="GET_PORTFOLIO",
            operations_through=snapshot.operations_through,
            accepted_by="daily-preparation",
            accepted_at=max(accepted_at, snapshot.captured_at),
            policy="FIRST_ACCEPTED_ACCOUNT_EQUITY",
        )

    async def _ensure_submit_baseline(self, broker_id: str, context) -> None:
        now = self._clock()
        day = now.astimezone(ZoneInfo("Europe/Moscow")).date()
        if context.baseline is not None and context.baseline.moscow_day == day:
            return
        if (
            context.owner_runtime_instance_id is None
            or context.owner_generation is None
            or context.owner_access_mode != "TRADE"
            or not context.owner_strategy_enabled
            or context.lease_expires_at is None
            or context.lease_expires_at <= now
        ):
            raise ValueError("Current trade owner is unavailable for daily baseline capture.")
        account_id, environment = await run_sync(partial(self._repository.get_scope, broker_id))
        connection = await run_sync(partial(self._connection, broker_id))
        if environment != "PROD" or connection.environment != "PROD" or connection.access_mode != "TRADE":
            raise ValueError("Daily baseline requires the configured PROD TRADE account.")
        if (
            context.configuration_fingerprint
            and connection.configuration_fingerprint != context.configuration_fingerprint
        ):
            raise ValueError("Broker configuration changed before daily baseline capture.")
        start = datetime.combine(day, time.min, ZoneInfo("Europe/Moscow")).astimezone(UTC)
        snapshot = await self._require_risk().collect(connection, instrument_uid=None, operations_from=start)
        accepted_at = self._clock()
        baseline = self._daily_baseline(broker_id, account_id, day, snapshot, accepted_at=accepted_at)
        await run_sync(
            partial(
                self._repository.ensure_daily_baseline,
                baseline,
                configuration_fingerprint=connection.configuration_fingerprint,
                expected_owner_runtime_instance_id=context.owner_runtime_instance_id,
                expected_generation=context.owner_generation,
                snapshot_valid_until=snapshot.valid_until,
                final_clock=self._clock,
            )
        )

    @staticmethod
    def _daily_empty(status, reason):
        return DailyAdmissionPreparationView(
            moscow_day=None,
            captured_at=None,
            status=status,
            reason=reason,
            baseline_id=None,
            owner_runtime_instance_id=None,
            owner_generation=None,
            configuration_fingerprint=None,
            reconciliation_id=None,
            reconciliation_valid_until=None,
            suggested_limits=None,
            risk_reasons=(),
        )

    def _daily_with_view(
        self,
        view,
        status,
        reason,
        *,
        risk_reasons=(),
        moscow_day=None,
        configuration_fingerprint=None,
    ):
        baseline = view.baseline
        reconciliation = view.reconciliation
        return DailyAdmissionPreparationView(
            moscow_day=(moscow_day if moscow_day is not None else None if baseline is None else baseline.moscow_day),
            captured_at=self._clock(),
            status=status,
            reason=reason,
            baseline_id=None if baseline is None else baseline.baseline_id,
            owner_runtime_instance_id=view.owner_runtime_instance_id,
            owner_generation=view.owner_generation,
            configuration_fingerprint=(
                configuration_fingerprint if configuration_fingerprint is not None else view.configuration_fingerprint
            ),
            reconciliation_id=None if reconciliation is None else reconciliation.reconciliation_id,
            reconciliation_valid_until=None if reconciliation is None else reconciliation.valid_until,
            suggested_limits=None,
            risk_reasons=tuple(risk_reasons),
        )

    async def reconcile(
        self,
        broker_id: str,
        request: ReconciliationRequest,
        *,
        runtime_instance_id: UUID,
        generation: int,
        automatic: bool = False,
    ) -> ReconciliationResult:
        risk = self._require_risk()
        now = self._clock()
        account_id, environment = await run_sync(partial(self._repository.get_scope, broker_id))
        view = await run_sync(partial(self._repository.get_view, broker_id, now=now))
        if (
            environment != "PROD"
            or view.owner_runtime_instance_id != runtime_instance_id
            or view.owner_generation != generation
        ):
            raise ValueError("Reconciliation requires the current account owner.")
        connection = await run_sync(partial(self._connection, broker_id))
        if not connection.configuration_fingerprint:
            raise ValueError("Broker configuration fingerprint is unavailable.")
        operations_from = (
            datetime.combine(view.baseline.moscow_day, time.min, ZoneInfo("Europe/Moscow")).astimezone(UTC)
            if view.baseline is not None
            else datetime.combine(now.astimezone(ZoneInfo("Europe/Moscow")).date(), time.min, ZoneInfo("Europe/Moscow"))
        )
        request_ids = await run_sync(partial(self._repository.unresolved_request_ids, broker_id))
        age_policy_ids = (
            await run_sync(partial(self._repository.age_policy_candidate_ids, broker_id)) if automatic else frozenset()
        )
        snapshot = await risk.collect(
            connection,
            instrument_uid=request.canary_uid,
            operations_from=operations_from,
            request_ids=request_ids,
        )
        now = self._clock()
        reasons: list[SubmitDenialReason] = []
        unknown_states = [state for state in snapshot.order_states if state.unknown_reason is not None]
        request_not_found_ids = {
            state.request_id for state in unknown_states if state.unknown_reason == "REQUEST_NOT_FOUND"
        }
        age_policy_unknowns_are_accounted = (
            automatic
            and bool(age_policy_ids)
            and set(snapshot.unknown_reasons) <= {"REQUEST_NOT_FOUND"}
            and len(request_not_found_ids) == len(unknown_states)
            and request_not_found_ids <= age_policy_ids
            and all(state.unknown_reason == "REQUEST_NOT_FOUND" for state in unknown_states)
            and len({state.request_id for state in snapshot.order_states}) == len(snapshot.order_states)
        )
        if (
            snapshot.account_id != account_id
            or (snapshot.unknown_reasons and not age_policy_unknowns_are_accounted)
            or snapshot.capture_started_at > snapshot.captured_at
            or snapshot.captured_at > now
            or snapshot.valid_until <= now
            or view.lease_expires_at is None
            or view.lease_expires_at <= now
        ):
            reasons.append(SubmitDenialReason.DATA_UNKNOWN)
        if not snapshot.operations_complete or snapshot.operations_through != snapshot.captured_at:
            reasons.append(SubmitDenialReason.DATA_UNKNOWN)
        terminal_order_states = {"EXECUTED", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
        active_ordinary_orders = tuple(
            order for order in snapshot.ordinary_orders if order.status.upper() not in terminal_order_states
        )
        unresolved_keys = set(request_ids)
        if any(order.request_id not in unresolved_keys for order in active_ordinary_orders):
            reasons.append(SubmitDenialReason.FOREIGN_ACTIVE_ORDER)
        if snapshot.stop_orders:
            reasons.append(SubmitDenialReason.FOREIGN_ACTIVE_ORDER)
        if any(
            (not state.attributable or state.unknown_reason)
            and not (
                age_policy_unknowns_are_accounted
                and state.unknown_reason == "REQUEST_NOT_FOUND"
                and state.request_id in age_policy_ids
            )
            for state in snapshot.order_states
        ):
            reasons.append(SubmitDenialReason.DATA_UNKNOWN)
        unresolved_count = await run_sync(partial(self._repository.unresolved_reservation_count, broker_id))
        scope = "ACCOUNT"
        position_anchors = []
        seen_uids = set()
        for position in snapshot.positions:
            if position.instrument_uid in seen_uids:
                reasons.append(SubmitDenialReason.DATA_UNKNOWN)
                break
            seen_uids.add(position.instrument_uid)
            if (
                position.currency != "RUB"
                or not position.quantity_units.is_finite()
                or not position.blocked_units.is_finite()
                or position.quantity_units < 0
                or position.blocked_units < 0
                or position.blocked_units > position.quantity_units
                or position.lot_size is None
                or position.lot_size <= 0
                or position.mark_price is None
                or not position.mark_price.is_finite()
                or position.mark_price <= 0
                or position.mark_captured_at is None
                or position.mark_captured_at > now
                or now - position.mark_captured_at > timedelta(seconds=1)
            ):
                reasons.append(SubmitDenialReason.DATA_UNKNOWN)
                break
            position_anchors.append(
                ReconciliationPositionAnchor(
                    instrument_uid=position.instrument_uid,
                    quantity_units=position.quantity_units,
                    lot_size=position.lot_size,
                )
            )
        if automatic and age_policy_ids:
            age_lots = await run_sync(
                partial(self._repository.age_policy_candidate_anchor_lots, broker_id)
            )
            if age_lots is None:
                reasons.append(SubmitDenialReason.DATA_UNKNOWN)
            else:
                anchor_lots_by_uid = {item.instrument_uid: item.lot_size for item in position_anchors}
                for uid, lot_size in age_lots:
                    if uid in anchor_lots_by_uid:
                        if anchor_lots_by_uid[uid] != lot_size:
                            reasons.append(SubmitDenialReason.DATA_UNKNOWN)
                        continue
                    # A complete account position snapshot omits zero holdings.
                    # Retain a zero anchor for each unresolved historical order so
                    # a later authenticated fill can be checked against fresh units.
                    position_anchors.append(
                        ReconciliationPositionAnchor(instrument_uid=uid, quantity_units=Decimal(0), lot_size=lot_size)
                    )
        if (
            snapshot.equity is None
            or not snapshot.equity.is_finite()
            or snapshot.equity < 0
            or snapshot.equity_currency != "RUB"
        ):
            reasons.append(SubmitDenialReason.DATA_UNKNOWN)
        valid_until = min(snapshot.captured_at + timedelta(seconds=30), view.lease_expires_at)
        status = (
            ReconciliationStatus.DATA_UNKNOWN
            if SubmitDenialReason.DATA_UNKNOWN in reasons
            else ReconciliationStatus.ACCOUNT_ONLY
        )
        result = ReconciliationResult(
            reconciliation_id=uuid4(),
            broker_id=UUID(broker_id),
            account_id=account_id,
            scope=scope,
            owner_runtime_instance_id=runtime_instance_id,
            owner_generation=generation,
            configuration_fingerprint=connection.configuration_fingerprint,
            status=status,
            canary_uid=None,
            captured_at=snapshot.captured_at,
            valid_until=valid_until,
            can_arm=status is ReconciliationStatus.ACCOUNT_ONLY and not reasons,
            position_anchor_units=None,
            active_ordinary_order_count=len(active_ordinary_orders),
            active_stop_order_count=len(snapshot.stop_orders),
            unresolved_reservation_count=unresolved_count,
            reasons=tuple(dict.fromkeys(reasons)),
            risk_snapshot_id=UUID(snapshot.snapshot_id),
            position_anchors=tuple(position_anchors),
        )
        if automatic and not result.can_arm:
            return result
        return await run_sync(
            partial(
                self._repository.save_reconciliation,
                result,
                expected_revision=request.expected_revision,
                now=now,
                order_states=snapshot.order_states,
                require_unarmed=automatic,
                final_clock=self._clock if automatic else None,
                risk_snapshot_valid_until=snapshot.valid_until if automatic else None,
            )
        )

    def arm(self, broker_id: str, request: ArmRequest, *, actor: str) -> AdmissionView:
        return self._repository.arm(broker_id=broker_id, request=request, now=self._clock(), actor=actor)

    def revoke(self, broker_id: str, request: RevokeRequest, *, actor: str) -> AdmissionView:
        return self._repository.revoke(broker_id=broker_id, request=request, now=self._clock(), actor=actor)

    def owner_stopped(self, broker_id: str, request: OwnerStoppedRequest, *, actor: str) -> AdmissionView:
        now = self._clock()
        return self._repository.owner_stopped(
            request,
            broker_id=broker_id,
            now=now,
            actor=actor,
            lease_expires_at=now + timedelta(seconds=self._owner_lease_seconds),
        )

    async def authorize_submit(
        self,
        request: SubmitAuthorizationRequest,
        *,
        runtime_instance_id: UUID,
        generation: int,
    ) -> SubmitAuthorizationResult:
        processing_started_ns = perf_counter_ns()
        broker_id = str(request.broker_id)
        try:
            prior_attempt = await run_sync(
                partial(
                    self._repository.submit_attempt,
                    broker_id,
                    str(request.idempotency_key),
                    original_runtime_instance_id=None,
                    original_generation=None,
                )
            )
        except Exception:
            return SubmitDenied(reason=SubmitDenialReason.CORE_UNAVAILABLE, outcome_uncertain=True)
        if prior_attempt.consumed_state is not None:
            return SubmitDenied(
                reason=SubmitDenialReason.ALREADY_CONSUMED,
                outcome_uncertain=True,
                attempt=prior_attempt,
            )
        risk = self._require_risk()
        try:
            context = await run_sync(
                partial(
                    self._repository.get_submit_context, broker_id, str(request.automation_id), request.instrument_uid
                )
            )
        except ValueError:
            return SubmitDenied(reason=SubmitDenialReason.NOT_OWNER, outcome_uncertain=False)
        if (
            context.environment != "PROD"
            or context.account_id != request.account_id
            or context.owner_runtime_instance_id != str(runtime_instance_id)
            or context.owner_generation != generation
            or context.owner_access_mode != "TRADE"
            or not context.owner_strategy_enabled
        ):
            return SubmitDenied(reason=SubmitDenialReason.NOT_OWNER, outcome_uncertain=False)
        now = self._clock()
        current_day = now.astimezone(ZoneInfo("Europe/Moscow")).date()
        if context.baseline is None or context.baseline.moscow_day != current_day:
            try:
                await self._ensure_submit_baseline(broker_id, context)
            except (ValueError, AdmissionConflictError):
                return SubmitDenied(reason=SubmitDenialReason.BASELINE_REQUIRED, outcome_uncertain=False)
            return SubmitDenied(reason=SubmitDenialReason.BASELINE_REQUIRED, outcome_uncertain=False)
        if request.lot_size != context.lot_size or request.currency != context.currency:
            return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
        if context.revision != request.revision:
            return SubmitDenied(reason=SubmitDenialReason.DISARMED, outcome_uncertain=False)
        if context.automation_state != "IN_WORK":
            return SubmitDenied(reason=SubmitDenialReason.AUTOMATION_NOT_ACTIVE, outcome_uncertain=False)
        if context.limits is None:
            return SubmitDenied(reason=SubmitDenialReason.LIMITS_NOT_CONFIGURED, outcome_uncertain=False)
        if context.baseline is None:
            return SubmitDenied(reason=SubmitDenialReason.BASELINE_REQUIRED, outcome_uncertain=False)
        connection = await run_sync(partial(self._connection, broker_id))
        if (
            not connection.configuration_fingerprint
            or connection.configuration_fingerprint != request.configuration_fingerprint
            or connection.configuration_fingerprint != context.configuration_fingerprint
        ):
            return SubmitDenied(reason=SubmitDenialReason.CONFIGURATION_CHANGED, outcome_uncertain=False)
        if context.lease_expires_at is None or context.lease_expires_at <= now:
            return SubmitDenied(reason=SubmitDenialReason.LEASE_EXPIRED, outcome_uncertain=False)
        if request.submit_deadline <= now or request.market_valid_until <= now:
            return SubmitDenied(reason=SubmitDenialReason.STALE_MARKET, outcome_uncertain=False)
        if (
            not context.instrument_active
            or not context.api_trade_available
            or context.instrument_type.upper() not in {"SHARE", "FUND"}
            or context.currency != "RUB"
            or context.instrument_uid != request.instrument_uid
        ):
            return SubmitDenied(reason=SubmitDenialReason.UID_NOT_ALLOWED, outcome_uncertain=False)
        if context.limits.max_outstanding_orders <= 0 or context.limits.max_order_notional <= 0:
            return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)

        snapshot = await risk.collect(
            connection,
            instrument_uid=request.instrument_uid,
            operations_from=datetime.combine(
                context.baseline.moscow_day, time.min, ZoneInfo("Europe/Moscow")
            ).astimezone(UTC),
            request_ids=tuple(
                item.idempotency_key
                for item in context.reservations
                if item.state in {"IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN"}
            ),
            instrument_uids=tuple(
                sorted(
                    {
                        item.instrument_uid
                        for item in context.reservations
                        if item.state in {"IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN"}
                    }
                )
            ),
        )
        estimate = await risk.estimate_limit_order(
            connection,
            instrument_uid=request.instrument_uid,
            side=request.side,
            quantity_lots=request.quantity_lots,
            limit_price=request.limit_price,
        )
        checked_at = self._clock()
        safe_snapshot = self._validate_submit_snapshot(snapshot, context, estimate, request, checked_at)
        if isinstance(safe_snapshot, SubmitDenialReason):
            return SubmitDenied(reason=safe_snapshot, outcome_uncertain=False)

        quote_notional, quote_fee, positions_by_uid, instruments_by_uid = safe_snapshot
        limits = context.limits
        reservation_projection = self._project_submit_reservations(
            context, snapshot, positions_by_uid, instruments_by_uid
        )
        if isinstance(reservation_projection, SubmitDenialReason):
            return SubmitDenied(reason=reservation_projection, outcome_uncertain=False)
        effective_reservations = reservation_projection
        selected_position = positions_by_uid.get(request.instrument_uid, (Decimal(0), Decimal(0)))
        position_units, blocked_units = selected_position
        mark_price = instruments_by_uid[request.instrument_uid].mark_price
        units = Decimal(context.lot_size * request.quantity_lots)
        if request.limit_price % context.min_price_increment != 0:
            return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
        if quote_notional <= 0 or quote_notional != units * request.limit_price:
            return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
        if quote_notional > limits.max_order_notional:
            return SubmitDenied(reason=SubmitDenialReason.ORDER_NOTIONAL_EXCEEDED, outcome_uncertain=False)
        if quote_fee < 0 or not quote_fee.is_finite():
            return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)

        unsettled = [item for item in effective_reservations if item.state in {"IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN"}]
        active_orders = self._active_orders(snapshot.ordinary_orders)
        own_keys = {item.idempotency_key for item in unsettled}
        known_keys = {
            item.idempotency_key
            for item in context.reservations
            if item.state in {"IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN"}
        }
        if snapshot.stop_orders or any(order.request_id not in own_keys for order in active_orders):
            return SubmitDenied(reason=SubmitDenialReason.FOREIGN_ACTIVE_ORDER, outcome_uncertain=False)
        if any(
            not state.attributable or state.unknown_reason or state.request_id not in known_keys
            for state in snapshot.order_states
        ):
            return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
        active_keys = {order.request_id for order in active_orders if order.request_id is not None}
        outstanding = len(active_orders) + len(snapshot.stop_orders) + len(own_keys - active_keys)
        if outstanding + 1 > limits.max_outstanding_orders:
            return SubmitDenied(reason=SubmitDenialReason.OUTSTANDING_ORDERS_EXCEEDED, outcome_uncertain=False)

        external_flow = self._net_external_cash_flow(snapshot, context.account_id)
        if isinstance(external_flow, SubmitDenialReason):
            return SubmitDenied(reason=external_flow, outcome_uncertain=False)
        if (
            max(
                Decimal(0),
                context.baseline.equity + external_flow - context.baseline.net_external_cash_flow - snapshot.equity,
            )
            >= limits.max_daily_loss
        ):
            return SubmitDenied(reason=SubmitDenialReason.DAILY_LOSS_EXCEEDED, outcome_uncertain=False)
        if position_units < 0 or blocked_units < 0 or blocked_units > position_units:
            return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
        reserved_cash = sum((item.reserved_cash for item in unsettled if item.side == "BUY"), Decimal(0))
        reserved_sell_units = sum(
            (
                item.reserved_position_units
                for item in unsettled
                if item.side == "SELL" and item.instrument_uid == request.instrument_uid
            ),
            Decimal(0),
        )
        if request.side == "BUY":
            if snapshot.money is None or snapshot.blocked_cash is None or not snapshot.money_loaded:
                return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
            if (
                not snapshot.money.is_finite()
                or not snapshot.blocked_cash.is_finite()
                or snapshot.money < 0
                or snapshot.blocked_cash < 0
                or snapshot.blocked_cash > snapshot.money
            ):
                return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
            if snapshot.money_currency != "RUB" or snapshot.blocked_cash_currency != "RUB":
                return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
            if snapshot.money - snapshot.blocked_cash - reserved_cash < quote_notional + quote_fee:
                return SubmitDenied(reason=SubmitDenialReason.INSUFFICIENT_CASH, outcome_uncertain=False)
        else:
            available_units = position_units - blocked_units - reserved_sell_units
            if available_units < units:
                return SubmitDenied(reason=SubmitDenialReason.INSUFFICIENT_POSITION, outcome_uncertain=False)

        mark_exposure = sum(
            (
                quantity * instruments_by_uid[uid].mark_price
                for uid, (quantity, _) in positions_by_uid.items()
                if quantity > 0
            ),
            Decimal(0),
        )
        existing_buy_exposure = sum(
            (
                max(
                    item.requested_amount
                    * Decimal(max(0, item.quantity_lots - item.executed_lots))
                    / Decimal(item.quantity_lots),
                    Decimal(
                        max(0, item.quantity_lots - item.executed_lots)
                        * instruments_by_uid[item.instrument_uid].lot_size
                    )
                    * instruments_by_uid[item.instrument_uid].mark_price,
                )
                for item in unsettled
                if item.side == "BUY"
            ),
            Decimal(0),
        )
        projected = max(quote_notional, units * mark_price) if request.side == "BUY" else Decimal(0)
        if mark_exposure + existing_buy_exposure + projected > limits.max_exposure:
            return SubmitDenied(reason=SubmitDenialReason.EXPOSURE_EXCEEDED, outcome_uncertain=False)

        expires_at = min(
            context.lease_expires_at,
            request.market_valid_until,
            request.submit_deadline,
            checked_at + timedelta(seconds=1),
        )
        if expires_at <= checked_at:
            return SubmitDenied(reason=SubmitDenialReason.STALE_MARKET, outcome_uncertain=False)
        risk_position_anchors = self._position_anchors(snapshot, context)
        risk_anchor_uids = {item.instrument_uid for item in risk_position_anchors}
        unresolved_states = {
            SubmitReservationState.IN_FLIGHT.value,
            SubmitReservationState.BROKER_ACTIVE.value,
            SubmitReservationState.UNKNOWN.value,
        }
        if any(
            item.state in unresolved_states and item.instrument_uid not in risk_anchor_uids
            for item in context.reservations
        ):
            return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
        required_mark_uids = {
            request.instrument_uid,
            *(uid for uid, (quantity, _) in positions_by_uid.items() if quantity > 0),
            *(item.instrument_uid for item in unsettled),
        }
        risk_instruments = instruments_by_uid
        if any(
            uid not in risk_instruments or risk_instruments[uid].mark_captured_at is None for uid in required_mark_uids
        ):
            return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
        snapshot_valid_until = min(
            [snapshot.valid_until]
            + [risk_instruments[uid].mark_captured_at + timedelta(seconds=1) for uid in required_mark_uids]
        )
        grant = await run_sync(
            partial(
                self._repository.grant_submit,
                request,
                runtime_instance_id=runtime_instance_id,
                generation=generation,
                final_clock=self._clock,
                risk_snapshot_captured_at=snapshot.captured_at,
                risk_snapshot_valid_until=snapshot_valid_until,
                risk_instrument=snapshot.instrument,
                risk_position_anchors=risk_position_anchors,
                order_states=snapshot.order_states,
                risk_snapshot_id=UUID(snapshot.snapshot_id),
                reserved_cash=quote_notional + quote_fee if request.side == "BUY" else Decimal(0),
                reserved_position_units=units if request.side == "SELL" else Decimal(0),
                requested_amount=quote_notional,
                expected_reservation_digest=context.reservation_digest,
            )
        )
        if not isinstance(grant, SubmitPermit):
            return grant
        server_response_at = self._clock()
        try:
            return SubmitPermit(
                **{
                    **grant.model_dump(),
                    "server_response_at": server_response_at,
                    "server_processing_ms": max(0, (perf_counter_ns() - processing_started_ns) // 1_000_000),
                }
            )
        except ValueError:
            attempt = await run_sync(
                partial(
                    self._repository.submit_attempt,
                    broker_id,
                    str(request.idempotency_key),
                    original_runtime_instance_id=str(runtime_instance_id),
                    original_generation=generation,
                )
            )
            return SubmitDenied(
                reason=SubmitDenialReason.ALREADY_CONSUMED,
                outcome_uncertain=True,
                attempt=attempt,
            )

    @staticmethod
    def _position_anchors(snapshot: AdmissionRiskSnapshot, context=None) -> tuple[ReconciliationPositionAnchor, ...]:
        """Anchor every freshly valued UID, including zero holdings for first fills."""
        positions = {item.instrument_uid: item for item in snapshot.positions}
        instruments = {item.instrument_uid: item for item in snapshot.instruments}
        if snapshot.instrument is not None:
            instruments[snapshot.instrument.instrument_uid] = snapshot.instrument
        anchors = []
        for uid, instrument in instruments.items():
            position = positions.get(uid)
            anchors.append(
                ReconciliationPositionAnchor(
                    instrument_uid=uid,
                    quantity_units=Decimal(0) if position is None else position.quantity_units,
                    lot_size=instrument.lot_size,
                )
            )
        anchored_uids = {item.instrument_uid for item in anchors}
        reconciliation = None if context is None else context.reconciliation
        previous_anchors = (
            {} if reconciliation is None else {item.instrument_uid: item for item in reconciliation.position_anchors}
        )
        for reservation in () if context is None else context.reservations:
            if reservation.state != SubmitReservationState.SETTLED.value or reservation.instrument_uid in anchored_uids:
                continue
            prior = previous_anchors.get(reservation.instrument_uid)
            if prior is None:
                continue
            # Fresh complete positions omit a closed UID. Preserve its known lot
            # for settled-history accounting without requiring a dead market quote.
            anchors.append(
                ReconciliationPositionAnchor(
                    instrument_uid=reservation.instrument_uid,
                    quantity_units=Decimal(0),
                    lot_size=prior.lot_size,
                )
            )
            anchored_uids.add(reservation.instrument_uid)
        return tuple(anchors)

    @staticmethod
    def _active_orders(orders: tuple[AdmissionOrder, ...]) -> tuple[AdmissionOrder, ...]:
        terminal = {"EXECUTED", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
        return tuple(order for order in orders if order.status.upper() not in terminal)

    @staticmethod
    def _project_submit_reservations(context, snapshot, current_positions, instruments_by_uid=None):
        unsettled_items = [
            item for item in context.reservations if item.state in {"IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN"}
        ]
        if not unsettled_items:
            return context.reservations
        reconciliation = context.reconciliation
        if reconciliation is None:
            return SubmitDenialReason.DATA_UNKNOWN
        anchor_id = str(reconciliation.reconciliation_id)
        anchors = {item.instrument_uid: item for item in reconciliation.position_anchors}
        legacy_scalar_anchor = not anchors
        if legacy_scalar_anchor:
            legacy_uid = reconciliation.canary_uid
            legacy_units = reconciliation.position_anchor_units
            current_value = current_positions.get(legacy_uid) if legacy_uid is not None else None
            current_instrument = (instruments_by_uid or {}).get(legacy_uid)
            reservations_match = all(item.instrument_uid == legacy_uid for item in context.reservations)
            if (
                reconciliation.scope != "CANARY_UID"
                or reconciliation.status is not ReconciliationStatus.CANARY_READY
                or not legacy_uid
                or legacy_units is None
                or not legacy_units.is_finite()
                or legacy_units < 0
                or current_value is None
                or current_instrument is None
                or type(current_instrument.lot_size) is not int
                or current_instrument.lot_size <= 0
                or not reservations_match
            ):
                return SubmitDenialReason.DATA_UNKNOWN
            anchors[legacy_uid] = ReconciliationPositionAnchor(
                instrument_uid=legacy_uid,
                quantity_units=legacy_units,
                lot_size=current_instrument.lot_size,
            )
        unsettled = {item.idempotency_key: item for item in unsettled_items}
        states_by_key: dict[str, list[AdmissionOrderState]] = {}
        for state in snapshot.order_states:
            states_by_key.setdefault(state.request_id, []).append(state)
        if set(states_by_key) != set(unsettled) or any(len(items) != 1 for items in states_by_key.values()):
            return SubmitDenialReason.DATA_UNKNOWN

        expected_units = {uid: anchor.quantity_units for uid, anchor in anchors.items()}
        projections = {item.idempotency_key: item for item in context.reservations}
        active_state_keys: set[str] = set()
        terminal_states = {"FILLED", "CANCELLED", "REJECTED"}
        active_states = {"NEW", "PARTIALLYFILL", "PARTIALLY_FILLED"}
        for item in context.reservations:
            if item.state != SubmitReservationState.SETTLED.value:
                continue
            anchor = anchors.get(item.instrument_uid)
            if anchor is None or item.anchor_reconciliation_id != anchor_id:
                return SubmitDenialReason.DATA_UNKNOWN
            delta_lots = item.executed_lots - item.anchor_executed_lots
            if delta_lots < 0:
                return SubmitDenialReason.DATA_UNKNOWN
            sign = Decimal(1) if item.side == "BUY" else Decimal(-1)
            expected_units[item.instrument_uid] += sign * Decimal(delta_lots * anchor.lot_size)
        for item in unsettled_items:
            anchor = anchors.get(item.instrument_uid)
            if anchor is None or item.anchor_reconciliation_id != anchor_id:
                return SubmitDenialReason.DATA_UNKNOWN
            state = states_by_key[item.idempotency_key][0]
            status = state.status.upper()
            if (
                not state.attributable
                or state.unknown_reason is not None
                or state.broker_order_id is None
                or state.instrument_uid != item.instrument_uid
                or state.side is None
                or state.side.value != item.side
                or state.requested_lots != item.quantity_lots
                or state.executed_lots is None
                or state.executed_lots < item.executed_lots
                or state.executed_lots > item.quantity_lots
                or state.executed_amount is None
                or not state.executed_amount.is_finite()
                or state.executed_amount < item.executed_amount
                or state.executed_commission is None
                or not state.executed_commission.is_finite()
                or state.executed_commission < item.executed_commission
                or state.currency != "RUB"
                or (status not in terminal_states and status not in active_states)
                or (status == "REJECTED" and state.executed_lots != 0)
                or (state.executed_lots == 0 and state.executed_amount != 0)
                or (state.executed_lots > 0 and state.executed_amount <= 0)
            ):
                return SubmitDenialReason.DATA_UNKNOWN
            delta_lots = state.executed_lots - item.anchor_executed_lots
            if delta_lots < 0:
                return SubmitDenialReason.DATA_UNKNOWN
            sign = Decimal(1) if item.side == "BUY" else Decimal(-1)
            expected_units[item.instrument_uid] += sign * Decimal(delta_lots * anchor.lot_size)
            is_terminal = status in terminal_states
            if not is_terminal:
                active_state_keys.add(item.idempotency_key)
            projections[item.idempotency_key] = replace(
                item,
                state=(
                    SubmitReservationState.SETTLED.value if is_terminal else SubmitReservationState.BROKER_ACTIVE.value
                ),
                reserved_cash=(
                    Decimal(0)
                    if is_terminal or item.side != "BUY"
                    else max(
                        Decimal(0),
                        item.reserved_cash
                        - (state.executed_amount - item.executed_amount)
                        - (state.executed_commission - item.executed_commission),
                    )
                ),
                reserved_position_units=(
                    Decimal(0)
                    if is_terminal or item.side != "SELL"
                    else Decimal((item.quantity_lots - state.executed_lots) * anchor.lot_size)
                ),
                executed_lots=state.executed_lots,
                executed_amount=state.executed_amount,
                executed_commission=state.executed_commission,
            )

        current = {uid: units for uid, (units, _) in current_positions.items()}
        current_keys = set(expected_units) if legacy_scalar_anchor else set(expected_units) | set(current)
        if any(expected_units.get(uid, Decimal(0)) != current.get(uid, Decimal(0)) for uid in current_keys):
            return SubmitDenialReason.DATA_UNKNOWN
        active_order_keys = {
            order.request_id
            for order in TradingAdmissionService._active_orders(snapshot.ordinary_orders)
            if order.request_id is not None
        }
        if active_order_keys - set(unsettled):
            return SubmitDenialReason.FOREIGN_ACTIVE_ORDER
        if active_order_keys != active_state_keys:
            return SubmitDenialReason.DATA_UNKNOWN
        return tuple(projections.values())

    @staticmethod
    def _net_external_cash_flow(snapshot: AdmissionRiskSnapshot, account_id: str) -> Decimal | SubmitDenialReason:
        if not snapshot.operations_complete or snapshot.operations_through != snapshot.captured_at:
            return SubmitDenialReason.DATA_UNKNOWN
        total = Decimal(0)
        external_types = INPUT_OPERATION_TYPES | OUTPUT_OPERATION_TYPES
        for operation in snapshot.operations:
            if (
                operation.account_id != account_id
                or operation.currency != "RUB"
                or operation.payment is None
                or not operation.payment.is_finite()
                or operation.occurred_at > snapshot.operations_through
            ):
                return SubmitDenialReason.DATA_UNKNOWN
            if operation.operation_type not in external_types:
                if operation.operation_type.startswith(("OPERATION_TYPE_INPUT", "OPERATION_TYPE_OUTPUT")):
                    return SubmitDenialReason.DATA_UNKNOWN
                continue
            if (
                operation.state not in EXECUTED_OPERATION_STATES
                or snapshot.capture_started_at <= operation.occurred_at <= snapshot.captured_at
            ):
                return SubmitDenialReason.DATA_UNKNOWN
            if operation.operation_type in INPUT_OPERATION_TYPES:
                total += abs(operation.payment)
            else:
                total -= abs(operation.payment)
        return total

    @staticmethod
    def _validate_submit_snapshot(snapshot, context, estimate, request, checked_at):
        if (
            snapshot.account_id != context.account_id
            or snapshot.unknown_reasons
            or snapshot.capture_started_at > snapshot.captured_at
            or snapshot.captured_at > checked_at
            or snapshot.valid_until <= checked_at
            or snapshot.equity is None
            or not snapshot.equity.is_finite()
            or snapshot.equity < 0
            or snapshot.equity_currency != "RUB"
            or not estimate.initial_order_amount.is_finite()
            or estimate.initial_order_amount <= 0
            or estimate.currency != "RUB"
            or estimate.captured_at > checked_at
            or estimate.captured_at < snapshot.capture_started_at
            or not estimate.initial_order_amount.is_finite()
            or not estimate.total_commission.is_finite()
            or estimate.total_commission < 0
        ):
            return SubmitDenialReason.DATA_UNKNOWN
        instrument = snapshot.instrument
        if (
            instrument is None
            or instrument.instrument_uid != request.instrument_uid
            or instrument.currency != "RUB"
            or instrument.instrument_type.upper() not in {"SHARE", "FUND"}
            or instrument.lot_size != context.lot_size
            or instrument.price_increment != context.min_price_increment
            or not instrument.api_trade_available
            or instrument.mark_price is None
            or not instrument.mark_price.is_finite()
            or instrument.mark_price <= 0
            or instrument.mark_captured_at is None
            or instrument.mark_captured_at > checked_at
            or checked_at - instrument.mark_captured_at > timedelta(seconds=1)
        ):
            return SubmitDenialReason.DATA_UNKNOWN
        instruments = {item.instrument_uid: item for item in snapshot.instruments}
        instruments.setdefault(instrument.instrument_uid, instrument)
        positions_by_uid = {}
        for position in snapshot.positions:
            if position.instrument_uid in positions_by_uid:
                return SubmitDenialReason.DATA_UNKNOWN
            if (
                position.currency != "RUB"
                or not position.quantity_units.is_finite()
                or not position.blocked_units.is_finite()
                or position.quantity_units < 0
                or position.blocked_units < 0
                or position.blocked_units > position.quantity_units
            ):
                return SubmitDenialReason.DATA_UNKNOWN
            if position.quantity_units > 0:
                if (
                    position.mark_price is None
                    or not position.mark_price.is_finite()
                    or position.mark_price <= 0
                    or position.lot_size is None
                    or position.lot_size <= 0
                    or position.mark_captured_at is None
                    or position.mark_captured_at > checked_at
                    or checked_at - position.mark_captured_at > timedelta(seconds=1)
                ):
                    return SubmitDenialReason.DATA_UNKNOWN
                held_instrument = instruments.get(position.instrument_uid)
                if (
                    held_instrument is None
                    or held_instrument.currency != "RUB"
                    or held_instrument.instrument_type.upper() not in {"SHARE", "FUND"}
                    or held_instrument.lot_size != position.lot_size
                    or held_instrument.mark_price != position.mark_price
                ):
                    return SubmitDenialReason.DATA_UNKNOWN
            positions_by_uid[position.instrument_uid] = (position.quantity_units, position.blocked_units)
        for reservation in getattr(context, "reservations", ()):
            if reservation.state not in {"IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN"}:
                continue
            reserved_instrument = instruments.get(reservation.instrument_uid)
            if (
                reserved_instrument is None
                or reserved_instrument.currency != "RUB"
                or reserved_instrument.instrument_type.upper() not in {"SHARE", "FUND"}
                or reserved_instrument.mark_price is None
                or not reserved_instrument.mark_price.is_finite()
                or reserved_instrument.mark_price <= 0
                or reserved_instrument.lot_size <= 0
                or reserved_instrument.mark_captured_at is None
                or reserved_instrument.mark_captured_at > checked_at
                or checked_at - reserved_instrument.mark_captured_at > timedelta(seconds=1)
            ):
                return SubmitDenialReason.DATA_UNKNOWN
        return estimate.initial_order_amount, estimate.total_commission, positions_by_uid, instruments

    def _require_risk(self) -> AdmissionRiskDataPort:
        if self._risk is None:
            raise ValueError("Admission risk data source is unavailable.")
        return self._risk

    def _connection(self, broker_id: str) -> BrokerConnection:
        if self._connection_resolver is None:
            raise ValueError("Broker connection source is unavailable.")
        return self._connection_resolver(broker_id)
