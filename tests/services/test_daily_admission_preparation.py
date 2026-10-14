import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from moex_sentinel.domain import daily_admission_preparation as contract
from moex_sentinel.services import daily_admission_preparation as service_module
from moex_sentinel.services import trading_admission as trading_admission_module
from moex_sentinel.services.trading_admission import TradingAdmissionService
from moex_sentinel.services.trading_admission_ports import AdmissionInstrument, AdmissionPosition, AdmissionRiskSnapshot
from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.trading_admission import (
    AdmissionLimits,
    AdmissionStatus,
)

NOW = datetime(2026, 10, 14, 8, tzinfo=UTC)


class AdmissionStub:
    def __init__(self, result):
        self.result = result
        self.prepared = []
        self.validated = []

    async def prepare_daily(self, broker_id, *, canary_uid):
        self.prepared.append((broker_id, canary_uid))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def validate_daily_preparation(self, broker_id, observation):
        self.validated.append((broker_id, observation))
        return observation


def waiting_view(contract):
    return contract.DailyAdmissionPreparationView(
        moscow_day=None,
        captured_at=None,
        status=contract.DailyAdmissionPreparationStatus.WAITING,
        reason=contract.DailyAdmissionPreparationReason.NOT_OBSERVED,
        baseline_id=None,
        owner_runtime_instance_id=None,
        owner_generation=None,
        configuration_fingerprint=None,
        reconciliation_id=None,
        reconciliation_valid_until=None,
        suggested_limits=None,
        risk_reasons=(),
    )


def test_restart_snapshot_is_unknown_and_read_only() -> None:
    admission = AdmissionStub(waiting_view(contract))
    service = service_module.DailyAdmissionPreparationService(
        admission,
        broker_id="00000000-0000-4000-8000-000000000001",
        enabled=True,
        environment="PROD",
        canary_uid="",
        clock=lambda: NOW,
    )

    result = asyncio.run(service.snapshot("00000000-0000-4000-8000-000000000001"))

    assert result.status is contract.DailyAdmissionPreparationStatus.WAITING
    assert result.reason is contract.DailyAdmissionPreparationReason.NOT_OBSERVED
    assert admission.prepared == []
    assert admission.validated == []


def test_run_prepares_immediately_then_waits_for_day_poll_and_propagates_cancel() -> None:
    observed = waiting_view(contract)
    admission = AdmissionStub(observed)
    prepared = asyncio.Event()
    delays = []

    async def sleep(seconds: float):
        delays.append(seconds)
        prepared.set()
        await asyncio.Event().wait()

    service = service_module.DailyAdmissionPreparationService(
        admission,
        broker_id="00000000-0000-4000-8000-000000000001",
        enabled=True,
        environment="PROD",
        canary_uid="",
        clock=lambda: NOW,
        sleep=sleep,
    )

    async def scenario():
        task = asyncio.create_task(service.run())
        await prepared.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Lifespan cancellation must stop the preparation worker")

    asyncio.run(scenario())

    assert admission.prepared == [("00000000-0000-4000-8000-000000000001", "")]
    assert delays == [60]


def test_run_checks_moscow_day_before_retry_deadline_and_retries_on_new_day() -> None:
    now = [datetime(2026, 10, 14, 20, 58, 59, tzinfo=UTC)]
    attempted_at = []

    class Admission:
        async def prepare_daily(self, _broker_id, *, canary_uid):
            attempted_at.append(now[0])
            if len(attempted_at) == 3:
                raise asyncio.CancelledError
            return contract.DailyAdmissionPreparationView(
                moscow_day=now[0].date(),
                captured_at=now[0],
                status=contract.DailyAdmissionPreparationStatus.BLOCKED,
                reason=contract.DailyAdmissionPreparationReason.CORE_UNAVAILABLE,
                baseline_id=None,
                owner_runtime_instance_id=None,
                owner_generation=None,
                configuration_fingerprint=None,
                reconciliation_id=None,
                reconciliation_valid_until=None,
                suggested_limits=None,
                risk_reasons=(),
            )

    delays = []

    async def advance(seconds: float):
        delays.append(seconds)
        now[0] += timedelta(seconds=seconds)

    service = service_module.DailyAdmissionPreparationService(
        Admission(),
        broker_id="00000000-0000-4000-8000-000000000001",
        enabled=True,
        environment="PROD",
        canary_uid="",
        clock=lambda: now[0],
        sleep=advance,
    )

    async def scenario():
        try:
            await service.run()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("The test admission should stop the scheduler on its third attempt")

    asyncio.run(scenario())

    assert delays == [60, 1]
    assert [value.astimezone(ZoneInfo("Europe/Moscow")).date() for value in attempted_at] == [
        datetime(2026, 10, 14).date(),
        datetime(2026, 10, 14).date(),
        datetime(2026, 10, 15).date(),
    ]


@pytest.mark.parametrize(
    ("clock_advance", "lease_seconds", "expected_status", "expected_reason"),
    [
        (
            timedelta(days=1),
            60,
            contract.DailyAdmissionPreparationStatus.WAITING,
            contract.DailyAdmissionPreparationReason.MOSCOW_DAY_CHANGED,
        ),
        (
            timedelta(seconds=16),
            60,
            contract.DailyAdmissionPreparationStatus.BLOCKED,
            contract.DailyAdmissionPreparationReason.RECONCILIATION_EXPIRED,
        ),
        (
            timedelta(seconds=16),
            15,
            contract.DailyAdmissionPreparationStatus.BLOCKED,
            contract.DailyAdmissionPreparationReason.LEASE_EXPIRED,
        ),
    ],
)
def test_validate_daily_preparation_uses_time_after_all_database_reads(
    monkeypatch, clock_advance, lease_seconds, expected_status, expected_reason
) -> None:
    broker_id = "00000000-0000-4000-8000-000000000151"
    owner_id = UUID("00000000-0000-4000-8000-000000000152")
    baseline_id = UUID("00000000-0000-4000-8000-000000000153")
    reconciliation_id = UUID("00000000-0000-4000-8000-000000000154")
    fingerprint = "a" * 64
    now = [NOW]
    validity = NOW + timedelta(seconds=15)
    view = SimpleNamespace(
        owner_runtime_instance_id=owner_id,
        owner_generation=1,
        lease_expires_at=NOW + timedelta(seconds=lease_seconds),
        recovery_only=False,
        admission_status=AdmissionStatus.DISARMED,
        baseline=SimpleNamespace(baseline_id=baseline_id, moscow_day=NOW.date()),
        configuration_fingerprint=fingerprint,
        reconciliation=SimpleNamespace(
            reconciliation_id=reconciliation_id,
            owner_runtime_instance_id=owner_id,
            owner_generation=1,
            configuration_fingerprint=fingerprint,
            canary_uid="safe-canary",
            can_arm=True,
            valid_until=validity,
        ),
    )
    repository = SimpleNamespace(
        get_scope=lambda _broker_id: ("synthetic-account", "PROD"),
        get_view=lambda _broker_id, *, now: view,
    )
    connection = BrokerConnection(
        broker_id=broker_id,
        adapter_code="t_invest",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - deterministic fake credential.
        is_test=False,
        environment="PROD",
        access_mode="TRADE",
        account_id="synthetic-account",
        configuration_fingerprint=fingerprint,
    )
    service = TradingAdmissionService(
        repository,
        connection_resolver=lambda _broker_id: connection,
        clock=lambda: now[0],
    )
    observation = contract.DailyAdmissionPreparationView(
        moscow_day=NOW.date(),
        captured_at=NOW,
        status=contract.DailyAdmissionPreparationStatus.PREPARED,
        reason=contract.DailyAdmissionPreparationReason.ARM_REQUIRED,
        baseline_id=baseline_id,
        owner_runtime_instance_id=owner_id,
        owner_generation=1,
        configuration_fingerprint=fingerprint,
        reconciliation_id=reconciliation_id,
        reconciliation_valid_until=validity,
        suggested_limits=AdmissionLimits(
            canary_uid="safe-canary",
            currency="RUB",
            max_order_notional=100,
            exposure_scope="CANARY_UID_GROSS",
            max_exposure=1000,
            max_outstanding_orders=1,
            max_daily_loss=500,
        ),
        risk_reasons=(),
    )
    reads = 0

    async def run_sync(function):
        nonlocal reads
        result = function()
        reads += 1
        if reads == 3:
            now[0] = NOW + clock_advance
        return result

    monkeypatch.setattr(trading_admission_module, "run_sync", run_sync)

    result = asyncio.run(service.validate_daily_preparation(broker_id, observation))

    assert reads == 3
    assert result.status is expected_status
    assert result.reason is expected_reason


def test_blocked_observation_keeps_snapshot_reason_and_resolved_config_without_baseline(monkeypatch) -> None:
    broker_id = "00000000-0000-4000-8000-000000000161"
    owner_id = UUID("00000000-0000-4000-8000-000000000162")
    old_baseline_id = UUID("00000000-0000-4000-8000-000000000163")
    new_baseline_id = UUID("00000000-0000-4000-8000-000000000164")
    fingerprint = "b" * 64
    current_day = NOW.astimezone(ZoneInfo("Europe/Moscow")).date()
    view = SimpleNamespace(
        owner_runtime_instance_id=owner_id,
        owner_generation=1,
        lease_expires_at=NOW + timedelta(minutes=1),
        recovery_only=False,
        admission_status=AdmissionStatus.DISARMED,
        baseline=SimpleNamespace(baseline_id=old_baseline_id, moscow_day=current_day - timedelta(days=1)),
        configuration_fingerprint=None,
        reconciliation=None,
    )
    repository = SimpleNamespace(
        get_scope=lambda _broker_id: ("synthetic-account", "PROD"),
        get_view=lambda _broker_id, *, now: view,
    )
    connection = BrokerConnection(
        broker_id=broker_id,
        adapter_code="t_invest",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - deterministic fake credential.
        is_test=False,
        environment="PROD",
        access_mode="TRADE",
        account_id="synthetic-account",
        configuration_fingerprint=fingerprint,
    )
    service = TradingAdmissionService(
        repository,
        connection_resolver=lambda _broker_id: connection,
        clock=lambda: NOW,
    )

    async def run_sync(function):
        return function()

    monkeypatch.setattr(trading_admission_module, "run_sync", run_sync)
    blocked = service._daily_with_view(
        view,
        contract.DailyAdmissionPreparationStatus.BLOCKED,
        contract.DailyAdmissionPreparationReason.SNAPSHOT_INVALID,
        moscow_day=current_day,
        configuration_fingerprint=connection.configuration_fingerprint,
    )

    checked_blocked = asyncio.run(service.validate_daily_preparation(broker_id, blocked))

    assert blocked.moscow_day == current_day
    assert blocked.configuration_fingerprint == fingerprint
    assert checked_blocked.status is contract.DailyAdmissionPreparationStatus.BLOCKED
    assert checked_blocked.reason is contract.DailyAdmissionPreparationReason.SNAPSHOT_INVALID

    view.baseline = SimpleNamespace(baseline_id=new_baseline_id, moscow_day=current_day)
    accepted = service._daily_with_view(
        view,
        contract.DailyAdmissionPreparationStatus.BASELINE_ACCEPTED,
        contract.DailyAdmissionPreparationReason.RECONCILIATION_REQUIRED,
        moscow_day=current_day,
        configuration_fingerprint=connection.configuration_fingerprint,
    )
    checked_accepted = asyncio.run(service.validate_daily_preparation(broker_id, accepted))

    assert accepted.configuration_fingerprint == fingerprint
    assert checked_accepted.status is contract.DailyAdmissionPreparationStatus.BASELINE_ACCEPTED
    assert checked_accepted.reason is contract.DailyAdmissionPreparationReason.RECONCILIATION_REQUIRED


def test_prepare_daily_revalidates_after_canary_preview(monkeypatch) -> None:
    broker_id = "00000000-0000-4000-8000-000000000171"
    owner_id = UUID("00000000-0000-4000-8000-000000000172")
    baseline_id = UUID("00000000-0000-4000-8000-000000000173")
    reconciliation_id = UUID("00000000-0000-4000-8000-000000000174")
    fingerprint = "c" * 64
    current_day = NOW.astimezone(ZoneInfo("Europe/Moscow")).date()
    now = [NOW]
    baseline = SimpleNamespace(baseline_id=baseline_id, moscow_day=current_day, equity=Decimal("100000"))
    reconciliation = SimpleNamespace(
        reconciliation_id=reconciliation_id,
        owner_runtime_instance_id=owner_id,
        owner_generation=1,
        configuration_fingerprint=fingerprint,
        canary_uid="safe-canary",
        captured_at=NOW - timedelta(seconds=1),
        can_arm=True,
        valid_until=NOW + timedelta(seconds=5),
        reasons=(),
    )
    view = SimpleNamespace(
        owner_runtime_instance_id=owner_id,
        owner_generation=1,
        lease_expires_at=NOW + timedelta(minutes=1),
        recovery_only=False,
        admission_status=AdmissionStatus.DISARMED,
        baseline=baseline,
        configuration_fingerprint=None,
        reconciliation=reconciliation,
        revision=3,
    )
    repository = SimpleNamespace(
        get_scope=lambda _broker_id: ("synthetic-account", "PROD"),
        get_view=lambda _broker_id, *, now: view,
    )
    connection = BrokerConnection(
        broker_id=broker_id,
        adapter_code="t_invest",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - deterministic fake credential.
        is_test=False,
        environment="PROD",
        access_mode="TRADE",
        account_id="synthetic-account",
        configuration_fingerprint=fingerprint,
    )
    preview = AdmissionRiskSnapshot(
        snapshot_id="00000000-0000-4000-8000-000000000175",
        account_id="synthetic-account",
        capture_started_at=NOW - timedelta(seconds=1),
        captured_at=NOW + timedelta(seconds=6),
        valid_until=NOW + timedelta(seconds=20),
        equity=Decimal("100000"),
        equity_currency="RUB",
        money=Decimal("50000"),
        money_currency="RUB",
        blocked_cash=Decimal("5000"),
        blocked_cash_currency="RUB",
        money_loaded=True,
        positions=(AdmissionPosition("safe-canary", Decimal("1"), Decimal(0), Decimal("100"), "RUB"),),
        ordinary_orders=(),
        stop_orders=(),
        order_states=(),
        operations=(),
        operations_complete=True,
        operations_through=NOW + timedelta(seconds=6),
        instrument=AdmissionInstrument(
            instrument_uid="safe-canary",
            instrument_type="SHARE",
            currency="RUB",
            lot_size=1,
            price_increment=Decimal("0.01"),
            api_trade_available=True,
            mark_price=Decimal("100"),
            mark_captured_at=NOW + timedelta(seconds=6),
        ),
    )

    class Risk:
        async def collect(self, _connection, *, instrument_uid, operations_from, request_ids=()):
            assert instrument_uid == "safe-canary"
            now[0] = NOW + timedelta(seconds=6)
            return preview

    service = TradingAdmissionService(
        repository,
        risk=Risk(),
        connection_resolver=lambda _broker_id: connection,
        clock=lambda: now[0],
    )

    async def run_sync(function):
        return function()

    monkeypatch.setattr(trading_admission_module, "run_sync", run_sync)

    result = asyncio.run(service.prepare_daily(broker_id, canary_uid="safe-canary"))

    assert result.status is contract.DailyAdmissionPreparationStatus.BLOCKED
    assert result.reason is contract.DailyAdmissionPreparationReason.RECONCILIATION_EXPIRED
