"""Business rules for durable ownership and guarded trade admission."""

import hashlib
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from functools import partial
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
    AdmissionLimits,
    AdmissionView,
    ArmRequest,
    CaptureBaselineRequest,
    OwnerStoppedRequest,
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

    async def prepare_daily(self, broker_id: str, *, canary_uid: str) -> DailyAdmissionPreparationView:
        """Accept today's baseline idempotently and optionally prepare an unarmed canary."""
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

        if view.recovery_only or view.admission_status.value == "STOP_REQUIRED":
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.RECOVERY_REQUIRED,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        if not canary_uid:
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BASELINE_ACCEPTED,
                DailyAdmissionPreparationReason.RECONCILIATION_REQUIRED,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        if view.admission_status.value != "DISARMED":
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.ARM_REQUIRED,
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

        reconciliation = view.reconciliation
        if reconciliation is not None and (
            reconciliation.owner_runtime_instance_id != view.owner_runtime_instance_id
            or reconciliation.owner_generation != view.owner_generation
            or reconciliation.configuration_fingerprint != connection.configuration_fingerprint
            or reconciliation.canary_uid != canary_uid
            or reconciliation.captured_at.astimezone(ZoneInfo("Europe/Moscow")).date() != day
        ):
            reconciliation = None
        if reconciliation is not None and reconciliation.can_arm and reconciliation.valid_until <= self._clock():
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.RECONCILIATION_EXPIRED,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )

        if reconciliation is None:
            request = ReconciliationRequest(expected_revision=view.revision, canary_uid=canary_uid)
            try:
                reconciliation = await self.reconcile(
                    broker_id,
                    request,
                    runtime_instance_id=view.owner_runtime_instance_id,
                    generation=view.owner_generation,
                    automatic=True,
                )
            except (ValueError, AdmissionConflictError):
                return self._daily_with_view(
                    view,
                    DailyAdmissionPreparationStatus.BLOCKED,
                    DailyAdmissionPreparationReason.RECONCILIATION_REQUIRED,
                    moscow_day=day,
                    configuration_fingerprint=connection.configuration_fingerprint,
                )
        if not reconciliation.can_arm:
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.RISK_DATA_UNKNOWN,
                risk_reasons=reconciliation.reasons,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )

        checkpoint = DailyAdmissionPreparationView(
            moscow_day=day,
            captured_at=self._clock(),
            status=DailyAdmissionPreparationStatus.BASELINE_ACCEPTED,
            reason=DailyAdmissionPreparationReason.RECONCILIATION_REQUIRED,
            baseline_id=view.baseline.baseline_id,
            owner_runtime_instance_id=view.owner_runtime_instance_id,
            owner_generation=view.owner_generation,
            configuration_fingerprint=connection.configuration_fingerprint,
            reconciliation_id=reconciliation.reconciliation_id,
            reconciliation_valid_until=reconciliation.valid_until,
            suggested_limits=None,
            risk_reasons=(),
        )
        checkpoint = await self.validate_daily_preparation(broker_id, checkpoint)
        if checkpoint.status is not DailyAdmissionPreparationStatus.BASELINE_ACCEPTED:
            return checkpoint

        preview = await risk.collect(
            connection,
            instrument_uid=canary_uid,
            operations_from=datetime.combine(day, time.min, ZoneInfo("Europe/Moscow")).astimezone(UTC),
        )
        now = self._clock()
        suggested = self._suggested_limits(canary_uid, view.baseline, preview)
        instrument = preview.instrument
        market_invalid = (
            instrument is None
            or instrument.instrument_uid != canary_uid
            or not instrument.api_trade_available
            or instrument.mark_price is None
            or not instrument.mark_price.is_finite()
            or instrument.mark_price <= 0
            or instrument.mark_captured_at is None
            or instrument.mark_captured_at > now
            or now - instrument.mark_captured_at > timedelta(seconds=1)
        )
        if (
            suggested is None
            or preview.account_id != account_id
            or preview.valid_until <= now
            or preview.unknown_reasons
            or market_invalid
        ):
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.RISK_DATA_UNKNOWN,
                risk_reasons=(SubmitDenialReason.STALE_MARKET if market_invalid else SubmitDenialReason.DATA_UNKNOWN,),
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        prepared = DailyAdmissionPreparationView(
            moscow_day=day,
            captured_at=now,
            status=DailyAdmissionPreparationStatus.PREPARED,
            reason=DailyAdmissionPreparationReason.ARM_REQUIRED,
            baseline_id=view.baseline.baseline_id,
            owner_runtime_instance_id=view.owner_runtime_instance_id,
            owner_generation=view.owner_generation,
            configuration_fingerprint=connection.configuration_fingerprint,
            reconciliation_id=reconciliation.reconciliation_id,
            reconciliation_valid_until=reconciliation.valid_until,
            suggested_limits=suggested,
            risk_reasons=(),
        )
        return await self.validate_daily_preparation(broker_id, prepared)

    async def validate_daily_preparation(
        self, broker_id: str, observation: DailyAdmissionPreparationView
    ) -> DailyAdmissionPreparationView:
        """Revalidate a volatile observation against current DB identity and time."""
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
        if view.recovery_only or view.admission_status.value == "STOP_REQUIRED":
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.RECOVERY_REQUIRED,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        if observation.status is DailyAdmissionPreparationStatus.PREPARED:
            if (
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
        elif observation.status is DailyAdmissionPreparationStatus.BASELINE_ACCEPTED:
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
        configuration_changed = (
            observation.configuration_fingerprint != connection.configuration_fingerprint
            if observation.status
            in {
                DailyAdmissionPreparationStatus.PREPARED,
                DailyAdmissionPreparationStatus.BASELINE_ACCEPTED,
            }
            else observation.configuration_fingerprint is not None
            and observation.configuration_fingerprint != connection.configuration_fingerprint
        )
        if configuration_changed:
            return self._daily_with_view(
                view,
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationReason.CONFIGURATION_CHANGED,
                moscow_day=day,
                configuration_fingerprint=connection.configuration_fingerprint,
            )
        if (
            observation.status
            in {
                DailyAdmissionPreparationStatus.PREPARED,
                DailyAdmissionPreparationStatus.BASELINE_ACCEPTED,
            }
            and observation.reconciliation_id is not None
        ):
            reconciliation = view.reconciliation
            if (
                reconciliation is None
                or reconciliation.reconciliation_id != observation.reconciliation_id
                or not reconciliation.can_arm
                or reconciliation.owner_runtime_instance_id != view.owner_runtime_instance_id
                or reconciliation.owner_generation != view.owner_generation
                or reconciliation.configuration_fingerprint != connection.configuration_fingerprint
                or reconciliation.valid_until != observation.reconciliation_valid_until
                or reconciliation.valid_until <= now
                or (
                    observation.suggested_limits is not None
                    and reconciliation.canary_uid != observation.suggested_limits.canary_uid
                )
                or view.admission_status.value != "DISARMED"
            ):
                return self._daily_with_view(
                    view,
                    DailyAdmissionPreparationStatus.BLOCKED,
                    DailyAdmissionPreparationReason.RECONCILIATION_EXPIRED,
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

    @staticmethod
    def _suggested_limits(canary_uid, baseline, snapshot):
        cash_values = (snapshot.money, snapshot.blocked_cash)
        if (
            baseline is None
            or baseline.equity <= 0
            or snapshot.equity is None
            or snapshot.equity <= 0
            or snapshot.equity_currency != "RUB"
            or not snapshot.money_loaded
            or any(value is None or not value.is_finite() for value in cash_values)
            or snapshot.money_currency != "RUB"
            or snapshot.blocked_cash_currency != "RUB"
            or snapshot.money < 0
            or snapshot.blocked_cash < 0
            or snapshot.blocked_cash > snapshot.money
            or any(not item.quantity_units.is_finite() or item.quantity_units < 0 for item in snapshot.positions)
        ):
            return None
        free_cash = snapshot.money - snapshot.blocked_cash
        positive_positions = sum(1 for position in snapshot.positions if position.quantity_units > 0)
        if free_cash <= 0 or positive_positions <= 0:
            return None
        return AdmissionLimits(
            canary_uid=canary_uid,
            currency="RUB",
            max_order_notional=free_cash,
            exposure_scope="CANARY_UID_GROSS",
            max_exposure=snapshot.equity,
            max_outstanding_orders=positive_positions,
            max_daily_loss=baseline.equity * Decimal("0.05"),
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
        snapshot = await risk.collect(
            connection,
            instrument_uid=request.canary_uid,
            operations_from=operations_from,
            request_ids=request_ids,
        )
        now = self._clock()
        reasons: list[SubmitDenialReason] = []
        if (
            snapshot.account_id != account_id
            or snapshot.unknown_reasons
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
        if any(not state.attributable or state.unknown_reason for state in snapshot.order_states):
            reasons.append(SubmitDenialReason.DATA_UNKNOWN)
        unresolved_count = await run_sync(partial(self._repository.unresolved_reservation_count, broker_id))
        position_units = None
        scope = "ACCOUNT" if request.canary_uid is None else "CANARY_UID"
        if request.canary_uid is not None:
            if snapshot.instrument is None or snapshot.instrument.instrument_uid != request.canary_uid:
                reasons.append(SubmitDenialReason.DATA_UNKNOWN)
            matching = [item for item in snapshot.positions if item.instrument_uid == request.canary_uid]
            if len(matching) > 1 or (
                matching
                and (
                    matching[0].currency != "RUB"
                    or not matching[0].quantity_units.is_finite()
                    or not matching[0].blocked_units.is_finite()
                    or matching[0].quantity_units < 0
                    or matching[0].blocked_units < 0
                    or matching[0].blocked_units > matching[0].quantity_units
                )
            ):
                reasons.append(SubmitDenialReason.DATA_UNKNOWN)
            else:
                position_units = Decimal(0) if not matching else matching[0].quantity_units
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
            else ReconciliationStatus.CANARY_READY if scope == "CANARY_UID" else ReconciliationStatus.ACCOUNT_ONLY
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
            canary_uid=request.canary_uid,
            captured_at=snapshot.captured_at,
            valid_until=valid_until,
            can_arm=status is ReconciliationStatus.CANARY_READY and not reasons,
            position_anchor_units=position_units,
            active_ordinary_order_count=len(active_ordinary_orders),
            active_stop_order_count=len(snapshot.stop_orders),
            unresolved_reservation_count=unresolved_count,
            reasons=tuple(dict.fromkeys(reasons)),
            risk_snapshot_id=UUID(snapshot.snapshot_id),
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
        if context.recovery_only:
            return SubmitDenied(reason=SubmitDenialReason.STOP_REQUIRED, outcome_uncertain=False)
        if request.lot_size != context.lot_size or request.currency != context.currency:
            return SubmitDenied(reason=SubmitDenialReason.DATA_UNKNOWN, outcome_uncertain=False)
        if (
            context.revision != request.revision
            or context.arm_revision != request.revision
            or context.admission_status != "ARMED"
            or context.limits is None
            or context.baseline is None
            or context.reconciliation is None
        ):
            return SubmitDenied(reason=SubmitDenialReason.DISARMED, outcome_uncertain=False)
        if request.instrument_uid != context.limits.canary_uid:
            return SubmitDenied(reason=SubmitDenialReason.UID_NOT_ALLOWED, outcome_uncertain=False)
        connection = await run_sync(partial(self._connection, broker_id))
        if (
            not connection.configuration_fingerprint
            or connection.configuration_fingerprint != request.configuration_fingerprint
            or connection.configuration_fingerprint != context.configuration_fingerprint
        ):
            return SubmitDenied(reason=SubmitDenialReason.CONFIGURATION_CHANGED, outcome_uncertain=False)
        now = self._clock()
        if (
            context.lease_expires_at is None
            or context.lease_expires_at <= now
            or context.arm_expires_at is None
            or context.arm_expires_at <= now
        ):
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

        quote_notional, quote_fee, position_units, blocked_units, mark_price = safe_snapshot
        limits = context.limits
        reservation_projection = self._project_submit_reservations(context, snapshot, position_units)
        if isinstance(reservation_projection, SubmitDenialReason):
            return SubmitDenied(reason=reservation_projection, outcome_uncertain=False)
        effective_reservations = reservation_projection
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

        mark_exposure = position_units * mark_price
        existing_buy_exposure = sum(
            (
                max(
                    item.requested_amount
                    * Decimal(max(0, item.quantity_lots - item.executed_lots))
                    / Decimal(item.quantity_lots),
                    Decimal(max(0, item.quantity_lots - item.executed_lots) * context.lot_size) * mark_price,
                )
                for item in unsettled
                if item.side == "BUY" and item.instrument_uid == request.instrument_uid
            ),
            Decimal(0),
        )
        projected = max(quote_notional, units * mark_price) if request.side == "BUY" else Decimal(0)
        if mark_exposure + existing_buy_exposure + projected > limits.max_exposure:
            return SubmitDenied(reason=SubmitDenialReason.EXPOSURE_EXCEEDED, outcome_uncertain=False)

        expires_at = min(
            context.lease_expires_at,
            context.arm_expires_at,
            request.market_valid_until,
            request.submit_deadline,
            checked_at + timedelta(seconds=1),
        )
        if expires_at <= checked_at:
            return SubmitDenied(reason=SubmitDenialReason.STALE_MARKET, outcome_uncertain=False)
        grant = await run_sync(
            partial(
                self._repository.grant_submit,
                request,
                runtime_instance_id=runtime_instance_id,
                generation=generation,
                final_clock=self._clock,
                risk_snapshot_captured_at=snapshot.captured_at,
                risk_snapshot_valid_until=min(
                    snapshot.valid_until,
                    snapshot.instrument.mark_captured_at + timedelta(seconds=1),
                ),
                risk_instrument=snapshot.instrument,
                risk_position_anchor_units=position_units,
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
    def _active_orders(orders: tuple[AdmissionOrder, ...]) -> tuple[AdmissionOrder, ...]:
        terminal = {"EXECUTED", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
        return tuple(order for order in orders if order.status.upper() not in terminal)

    @staticmethod
    def _project_submit_reservations(context, snapshot, current_units):
        reconciliation = context.reconciliation
        if reconciliation is None or reconciliation.position_anchor_units is None:
            return SubmitDenialReason.DATA_UNKNOWN
        anchor_id = str(reconciliation.reconciliation_id)
        unsettled = {
            item.idempotency_key: item
            for item in context.reservations
            if item.state in {"IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN"}
        }
        states_by_key: dict[str, list[AdmissionOrderState]] = {}
        for state in snapshot.order_states:
            states_by_key.setdefault(state.request_id, []).append(state)
        if set(states_by_key) != set(unsettled) or any(len(items) != 1 for items in states_by_key.values()):
            return SubmitDenialReason.DATA_UNKNOWN

        expected_units = reconciliation.position_anchor_units
        projections = {item.idempotency_key: item for item in context.reservations}
        active_state_keys: set[str] = set()
        terminal_states = {"FILLED", "CANCELLED", "REJECTED"}
        active_states = {"NEW", "PARTIALLYFILL", "PARTIALLY_FILLED"}
        for item in context.reservations:
            if item.anchor_reconciliation_id != anchor_id:
                continue
            if item.state not in {"IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN"}:
                delta_lots = item.executed_lots - item.anchor_executed_lots
                if delta_lots < 0:
                    return SubmitDenialReason.DATA_UNKNOWN
                sign = Decimal(1) if item.side == "BUY" else Decimal(-1)
                expected_units += sign * Decimal(delta_lots * context.lot_size)
                continue

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
            expected_units += sign * Decimal(delta_lots * context.lot_size)
            is_terminal = status in terminal_states
            if not is_terminal:
                active_state_keys.add(item.idempotency_key)
            if is_terminal:
                projections[item.idempotency_key] = replace(
                    item,
                    state=SubmitReservationState.SETTLED.value,
                    reserved_cash=Decimal(0),
                    reserved_position_units=Decimal(0),
                    executed_lots=state.executed_lots,
                    executed_amount=state.executed_amount,
                    executed_commission=state.executed_commission,
                )
            else:
                delta_amount = state.executed_amount - item.executed_amount
                delta_commission = state.executed_commission - item.executed_commission
                projections[item.idempotency_key] = replace(
                    item,
                    state=SubmitReservationState.BROKER_ACTIVE.value,
                    reserved_cash=(
                        max(Decimal(0), item.reserved_cash - delta_amount - delta_commission)
                        if item.side == "BUY"
                        else Decimal(0)
                    ),
                    reserved_position_units=(
                        Decimal((item.quantity_lots - state.executed_lots) * context.lot_size)
                        if item.side == "SELL"
                        else Decimal(0)
                    ),
                    executed_lots=state.executed_lots,
                    executed_amount=state.executed_amount,
                    executed_commission=state.executed_commission,
                )

        if expected_units != current_units:
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
        positions = [item for item in snapshot.positions if item.instrument_uid == request.instrument_uid]
        if len(positions) > 1 or (positions and positions[0].currency != "RUB"):
            return SubmitDenialReason.DATA_UNKNOWN
        if positions and (
            not positions[0].quantity_units.is_finite()
            or not positions[0].blocked_units.is_finite()
            or positions[0].quantity_units < 0
            or positions[0].blocked_units < 0
        ):
            return SubmitDenialReason.DATA_UNKNOWN
        quantity = Decimal(0) if not positions else positions[0].quantity_units
        blocked = Decimal(0) if not positions else positions[0].blocked_units
        return estimate.initial_order_amount, estimate.total_commission, quantity, blocked, instrument.mark_price

    def _require_risk(self) -> AdmissionRiskDataPort:
        if self._risk is None:
            raise ValueError("Admission risk data source is unavailable.")
        return self._risk

    def _connection(self, broker_id: str) -> BrokerConnection:
        if self._connection_resolver is None:
            raise ValueError("Broker connection source is unavailable.")
        return self._connection_resolver(broker_id)
