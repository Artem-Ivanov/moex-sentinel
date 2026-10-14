"""Fresh-source admission gates reject unsafe trade requests before permit creation."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from moex_sentinel.services.trading_admission import TradingAdmissionService
from moex_sentinel.services.trading_admission_ports import (
    AdmissionInstrument,
    AdmissionOperation,
    AdmissionOrder,
    AdmissionOrderEstimate,
    AdmissionOrderState,
    AdmissionRiskSnapshot,
)
from moex_sentinel.storage.repositories.trading_admission import AdmissionReservationRecord
from sentinel_contracts.broker_execution import BrokerConnection, OrderSide
from sentinel_contracts.trading_admission import (
    AdmissionLimits,
    SubmitAttemptView,
    SubmitAuthorizationRequest,
    SubmitDenialReason,
    SubmitDenied,
)

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
BROKER_ID = UUID("00000000-0000-4000-8000-000000000001")
RUNTIME_ID = UUID("00000000-0000-4000-8000-000000000002")
AUTOMATION_ID = UUID("00000000-0000-4000-8000-000000000004")
REQUEST_KEY = UUID("00000000-0000-4000-8000-000000000005")
UID = "canary-uid"
FINGERPRINT = "c" * 64


class AdmissionRisk:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def collect(self, _connection, **_kwargs):
        return self.snapshot

    async def estimate_limit_order(self, _connection, **_kwargs):
        return AdmissionOrderEstimate(Decimal("100"), Decimal("1"), "RUB", NOW)


class UnusedRepository:
    def __init__(self, context):
        self.context = context
        self.grant_called = False

    def submit_attempt(self, broker_id, idempotency_key, **_kwargs):
        return SubmitAttemptView(
            broker_id=UUID(broker_id),
            account_id="synthetic-account",
            idempotency_key=UUID(idempotency_key),
            consumed_state=None,
            proven_not_granted=False,
            original_runtime_instance_id=None,
            original_generation=None,
            original_generation_retired=False,
            absence_basis=None,
        )

    def get_submit_context(self, *_args, **_kwargs):
        return self.context

    def grant_submit(self, *_args, **_kwargs):
        self.grant_called = True
        raise AssertionError("unsafe request reached durable permit creation")


class GrantProbeRepository(UnusedRepository):
    def grant_submit(self, *_args, **kwargs):
        self.grant_called = True
        self.final_now = kwargs["final_clock"]()
        self.risk_snapshot_valid_until = kwargs["risk_snapshot_valid_until"]
        return SubmitDenied(reason=SubmitDenialReason.STALE_MARKET, outcome_uncertain=False)


def _snapshot(*, money=Decimal("1000"), positions=(), ordinary=(), order_states=(), operations=()):
    return AdmissionRiskSnapshot(
        snapshot_id="00000000-0000-4000-8000-000000000017",
        account_id="synthetic-account",
        capture_started_at=NOW - timedelta(milliseconds=100),
        captured_at=NOW,
        valid_until=NOW + timedelta(seconds=1),
        equity=Decimal("100000"),
        equity_currency="RUB",
        money=money,
        money_currency="RUB",
        blocked_cash=Decimal(0),
        blocked_cash_currency="RUB",
        money_loaded=True,
        positions=positions,
        ordinary_orders=ordinary,
        stop_orders=(),
        order_states=order_states,
        operations=operations,
        operations_complete=True,
        operations_through=NOW,
        instrument=AdmissionInstrument(
            instrument_uid=UID,
            instrument_type="SHARE",
            currency="RUB",
            lot_size=10,
            price_increment=Decimal("0.01"),
            api_trade_available=True,
            mark_price=Decimal("10"),
            mark_captured_at=NOW,
        ),
    )


def _service_context(*, side="BUY", max_exposure=Decimal("20000"), max_outstanding=1, reservations=()):
    request = SubmitAuthorizationRequest(
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        automation_id=AUTOMATION_ID,
        idempotency_key=UUID("00000000-0000-4000-8000-000000000006"),
        generation=1,
        revision=4,
        configuration_fingerprint=FINGERPRINT,
        instrument_uid=UID,
        side=side,
        lot_size=10,
        currency="RUB",
        quantity_lots=1,
        limit_price=Decimal("10"),
        market_valid_until=NOW + timedelta(seconds=10),
        submit_deadline=NOW + timedelta(seconds=9),
    )
    context = SimpleNamespace(
        environment="PROD",
        account_id="synthetic-account",
        owner_runtime_instance_id=str(RUNTIME_ID),
        owner_generation=1,
        owner_access_mode="TRADE",
        owner_strategy_enabled=True,
        recovery_only=False,
        revision=4,
        arm_revision=4,
        admission_status="ARMED",
        limits=AdmissionLimits(
            canary_uid=UID,
            currency="RUB",
            max_order_notional=Decimal("10000"),
            exposure_scope="CANARY_UID_GROSS",
            max_exposure=max_exposure,
            max_outstanding_orders=max_outstanding,
            max_daily_loss=Decimal("1000"),
        ),
        baseline=SimpleNamespace(moscow_day=NOW.date(), equity=Decimal("100000"), net_external_cash_flow=Decimal(0)),
        reconciliation=SimpleNamespace(
            reconciliation_id=UUID("00000000-0000-4000-8000-000000000011"),
            position_anchor_units=Decimal(0),
        ),
        configuration_fingerprint=FINGERPRINT,
        lease_expires_at=NOW + timedelta(seconds=30),
        arm_expires_at=NOW + timedelta(seconds=60),
        instrument_active=True,
        api_trade_available=True,
        instrument_type="SHARE",
        currency="RUB",
        instrument_uid=UID,
        lot_size=10,
        min_price_increment=Decimal("0.01"),
        reservations=reservations,
        reservation_digest="stable-reservation-digest",
    )
    return request, context


@pytest.mark.parametrize(
    ("request_lot_size", "context_currency"),
    [(1, "RUB"), (10, "USD")],
)
def test_request_economics_must_match_submit_context_before_risk_reads(request_lot_size, context_currency):
    request, context = _service_context()
    request = request.model_copy(update={"lot_size": request_lot_size})
    context.currency = context_currency
    repository = UnusedRepository(context)

    class RiskMustNotBeCollected:
        async def collect(self, *_args, **_kwargs):
            raise AssertionError("Mismatched submit economics must stop before risk reads.")

    connection = BrokerConnection(
        broker_id=str(BROKER_ID),
        adapter_code="tinkoff",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-test-token",  # noqa: S106 - fake connection is never opened.
        is_test=False,
        access_mode="TRADE",
        account_id="synthetic-account",
        configuration_fingerprint=FINGERPRINT,
    )
    service = TradingAdmissionService(
        repository,
        risk=RiskMustNotBeCollected(),
        connection_resolver=lambda _broker_id: connection,
        clock=lambda: NOW,
    )
    denied = asyncio.run(service.authorize_submit(request, runtime_instance_id=RUNTIME_ID, generation=1))

    assert isinstance(denied, SubmitDenied)
    assert denied.reason is SubmitDenialReason.DATA_UNKNOWN
    assert not repository.grant_called


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("insufficient_cash", SubmitDenialReason.INSUFFICIENT_CASH),
        ("insufficient_position", SubmitDenialReason.INSUFFICIENT_POSITION),
        ("exposure", SubmitDenialReason.EXPOSURE_EXCEEDED),
        ("foreign_order", SubmitDenialReason.FOREIGN_ACTIVE_ORDER),
        ("outstanding", SubmitDenialReason.OUTSTANDING_ORDERS_EXCEEDED),
    ],
)
def test_fresh_risk_gate_rejects_caps_and_unattributed_orders(scenario, expected):
    side = "SELL" if scenario == "insufficient_position" else "BUY"
    max_exposure = Decimal("50") if scenario == "exposure" else Decimal("20000")
    reservations = ()
    ordinary = ()
    states = ()
    if scenario == "foreign_order":
        ordinary = (AdmissionOrder("foreign-broker-order", UID, "external-request", "NEW", OrderSide.BUY, 1),)
    if scenario == "outstanding":
        own_key = str(REQUEST_KEY)
        reservations = (
            AdmissionReservationRecord(
                idempotency_key=own_key,
                state="IN_FLIGHT",
                side="BUY",
                instrument_uid=UID,
                requested_amount=Decimal("100"),
                reserved_cash=Decimal("101"),
                reserved_position_units=Decimal(0),
                quantity_lots=1,
                runtime_instance_id=str(RUNTIME_ID),
                generation=1,
                anchor_reconciliation_id="00000000-0000-4000-8000-000000000011",
                anchor_executed_lots=0,
                executed_lots=0,
                executed_amount=Decimal(0),
                executed_commission=Decimal(0),
            ),
        )
        states = (
            AdmissionOrderState(
                request_id=own_key,
                broker_order_id="owned-broker-order",
                instrument_uid=UID,
                side=OrderSide.BUY,
                requested_lots=1,
                executed_lots=0,
                executed_amount=Decimal(0),
                executed_commission=Decimal(0),
                currency="RUB",
                status="NEW",
                attributable=True,
                unknown_reason=None,
            ),
        )
        ordinary = (AdmissionOrder("owned-broker-order", UID, own_key, "NEW", OrderSide.BUY, 1),)

    request, context = _service_context(
        side=side,
        max_exposure=max_exposure,
        max_outstanding=1,
        reservations=reservations,
    )
    snapshot = _snapshot(
        money=Decimal("10") if scenario == "insufficient_cash" else Decimal("1000"),
        ordinary=ordinary,
        order_states=states,
    )
    connection = BrokerConnection(
        broker_id=str(BROKER_ID),
        adapter_code="tinkoff",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-test-token",  # noqa: S106 - fake connection is never opened.
        is_test=False,
        access_mode="TRADE",
        account_id="synthetic-account",
        configuration_fingerprint=FINGERPRINT,
    )
    repository = UnusedRepository(context)
    service = TradingAdmissionService(repository, clock=lambda: NOW + timedelta(milliseconds=200))
    service._risk = AdmissionRisk(snapshot)
    service._connection_resolver = lambda _broker_id: connection

    denied = asyncio.run(service.authorize_submit(request, runtime_instance_id=RUNTIME_ID, generation=1))

    assert isinstance(denied, SubmitDenied)
    assert denied.reason is expected
    assert not repository.grant_called


@pytest.mark.parametrize(
    ("instrument_type", "mark_captured_at", "expected_grant", "expected_reason"),
    [
        ("FUND", NOW, True, SubmitDenialReason.STALE_MARKET),
        ("ETF", NOW, False, SubmitDenialReason.DATA_UNKNOWN),
        ("FUND", NOW - timedelta(seconds=2), False, SubmitDenialReason.DATA_UNKNOWN),
    ],
)
def test_submit_gate_accepts_canonical_fund_but_rejects_unsupported_or_stale_instrument(
    instrument_type, mark_captured_at, expected_grant, expected_reason
):
    request, context = _service_context()
    context.instrument_type = "FUND"
    snapshot = replace(
        _snapshot(),
        instrument=AdmissionInstrument(
            instrument_uid=UID,
            instrument_type=instrument_type,
            currency="RUB",
            lot_size=10,
            price_increment=Decimal("0.01"),
            api_trade_available=True,
            mark_price=Decimal("10"),
            mark_captured_at=mark_captured_at,
        ),
    )
    repository = GrantProbeRepository(context)
    service = TradingAdmissionService(repository, clock=lambda: NOW + timedelta(milliseconds=200))
    service._risk = AdmissionRisk(snapshot)
    service._connection_resolver = lambda _broker_id: BrokerConnection(
        broker_id=str(BROKER_ID),
        adapter_code="tinkoff",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-test-token",  # noqa: S106 - fake connection is never opened.
        is_test=False,
        access_mode="TRADE",
        account_id="synthetic-account",
        configuration_fingerprint=FINGERPRINT,
    )

    result = asyncio.run(service.authorize_submit(request, runtime_instance_id=RUNTIME_ID, generation=1))

    assert repository.grant_called is expected_grant, result
    assert isinstance(result, SubmitDenied)
    assert result.reason is expected_reason


def test_pending_buy_exposure_uses_remaining_units_at_fresh_mark_or_limit():
    request, context = _service_context(max_exposure=Decimal("800"), max_outstanding=3)
    reservation_key = str(REQUEST_KEY)
    context.reservations = (
        AdmissionReservationRecord(
            idempotency_key=reservation_key,
            state="BROKER_ACTIVE",
            side="BUY",
            instrument_uid=UID,
            requested_amount=Decimal("200"),
            reserved_cash=Decimal("201"),
            reserved_position_units=Decimal(0),
            quantity_lots=2,
            runtime_instance_id=str(RUNTIME_ID),
            generation=1,
            anchor_reconciliation_id="00000000-0000-4000-8000-000000000011",
            anchor_executed_lots=0,
            executed_lots=0,
            executed_amount=Decimal(0),
            executed_commission=Decimal(0),
        ),
    )
    state = AdmissionOrderState(
        request_id=reservation_key,
        broker_order_id="owned-broker-order",
        instrument_uid=UID,
        side=OrderSide.BUY,
        requested_lots=2,
        executed_lots=1,
        executed_amount=Decimal("100"),
        executed_commission=Decimal(0),
        currency="RUB",
        status="PARTIALLY_FILLED",
        attributable=True,
        unknown_reason=None,
    )
    snapshot = _snapshot(
        positions=(
            SimpleNamespace(instrument_uid=UID, currency="RUB", quantity_units=Decimal("10"), blocked_units=Decimal(0)),
        ),
        ordinary=(AdmissionOrder("owned-broker-order", UID, reservation_key, "PARTIALLY_FILLED", OrderSide.BUY, 2),),
        order_states=(state,),
    )
    snapshot = replace(
        snapshot,
        instrument=AdmissionInstrument(
            instrument_uid=UID,
            instrument_type="SHARE",
            currency="RUB",
            lot_size=10,
            price_increment=Decimal("0.01"),
            api_trade_available=True,
            mark_price=Decimal("30"),
            mark_captured_at=NOW,
        ),
    )
    connection = BrokerConnection(
        broker_id=str(BROKER_ID),
        adapter_code="tinkoff",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-test-token",  # noqa: S106 - fake connection is never opened.
        is_test=False,
        access_mode="TRADE",
        account_id="synthetic-account",
        configuration_fingerprint=FINGERPRINT,
    )
    repository = UnusedRepository(context)
    service = TradingAdmissionService(repository, clock=lambda: NOW + timedelta(milliseconds=200))
    service._risk = AdmissionRisk(snapshot)
    service._connection_resolver = lambda _broker_id: connection

    denied = asyncio.run(service.authorize_submit(request, runtime_instance_id=RUNTIME_ID, generation=1))

    assert isinstance(denied, SubmitDenied)
    assert denied.reason is SubmitDenialReason.EXPOSURE_EXCEEDED
    assert not repository.grant_called


def test_non_rub_trade_operation_is_validated_but_not_added_to_external_cash_flow():
    operation = AdmissionOperation(
        operation_id="foreign-currency-trade",
        occurred_at=NOW - timedelta(seconds=1),
        operation_type="OPERATION_TYPE_SELL",
        state="OPERATION_STATE_EXECUTED",
        payment=Decimal("-10"),
        currency="EUR",
        account_id="synthetic-account",
    )

    result = TradingAdmissionService._net_external_cash_flow(_snapshot(operations=(operation,)), "synthetic-account")

    assert result is SubmitDenialReason.DATA_UNKNOWN


def test_mark_freshness_deadline_is_carried_to_locked_grant_check():
    request, context = _service_context()
    snapshot = replace(_snapshot(), valid_until=NOW + timedelta(seconds=5))
    connection = BrokerConnection(
        broker_id=str(BROKER_ID),
        adapter_code="tinkoff",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-test-token",  # noqa: S106 - fake connection is never opened.
        is_test=False,
        access_mode="TRADE",
        account_id="synthetic-account",
        configuration_fingerprint=FINGERPRINT,
    )
    times = iter(
        (
            NOW + timedelta(milliseconds=100),
            NOW + timedelta(milliseconds=800),
            NOW + timedelta(milliseconds=1100),
        )
    )
    repository = GrantProbeRepository(context)
    service = TradingAdmissionService(repository, clock=lambda: next(times))
    service._risk = AdmissionRisk(snapshot)
    service._connection_resolver = lambda _broker_id: connection

    asyncio.run(service.authorize_submit(request, runtime_instance_id=RUNTIME_ID, generation=1))

    assert repository.risk_snapshot_valid_until == NOW + timedelta(seconds=1)
    assert repository.final_now > repository.risk_snapshot_valid_until
