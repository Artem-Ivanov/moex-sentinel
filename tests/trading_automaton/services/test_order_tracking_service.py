import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.broker_execution import BrokerOrderState, BrokerPosition, OrderSide
from tests.trading_automaton.command_factory import command, decision_item
from trading_automaton.services.active_intent_gate import ActiveIntentGateService
from trading_automaton.services.order_dispatch import DispatchRequest
from trading_automaton.services.order_tracking import OrderTrackingService
from trading_automaton.storage.models import Base
from trading_automaton.storage.repository import (
    ExecutionFinalization,
    ExecutionFinalizationResult,
    LocalAutomationRepository,
)

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)
BASELINE_INTENT_ID = "00000000-0000-4000-8000-000000000405"


class Repository:
    def __init__(self) -> None:
        self.updates = []
        self.holds = []
        self.execution_events = []
        self.finalizations: list[ExecutionFinalization] = []
        self.finalization_error: Exception | None = None
        self.finalization_applied = True

    def update_intent(self, intent_id, **values):
        self.updates.append((intent_id, values))

    def hold_active(self, reason, automation_id=None):
        self.holds.append((reason, automation_id))

    def save_execution_event(self, **values):
        self.execution_events.append(values)

    def finalize_execution(self, finalization: ExecutionFinalization):
        if self.finalization_error is not None:
            raise self.finalization_error
        self.finalizations.append(finalization)
        self.updates.append((finalization.intent_id, {"state": finalization.state}))
        snapshot = None
        if finalization.executed_lots:
            quantity = finalization.executed_lots if finalization.side == "BUY" else 0
            snapshot = {
                "quantity_lots": quantity,
                "average_price": str(finalization.executed_price if quantity else 0),
                "invested_amount": str(finalization.executed_price * finalization.lot_size * quantity),
                "realized_pnl": "97" if finalization.side == "SELL" else "0",
                "unrealized_pnl": "0",
                "net_pnl": "97" if finalization.side == "SELL" else "0",
                "actual_commissions": str(finalization.executed_commission),
            }
            self.execution_events.append(
                {
                    "automation_id": finalization.automation_id,
                    "operation": {"broker_execution_id": finalization.broker_order_id},
                    "position_snapshot": snapshot,
                }
            )
        return ExecutionFinalizationResult(None, self.finalization_applied, snapshot)  # type: ignore[arg-type]


def test_tracks_broker_response_outside_dispatch_start() -> None:
    async def scenario():
        repository = Repository()
        tracking = OrderTrackingService(repository, now=lambda: NOW)
        response = BrokerOrderState(
            "order-1",
            "intent-1",
            "ACCEPTED",
            1,
            0,
            Decimal("1000"),
            Decimal(),
            Decimal("1"),
            Decimal(),
            "RUB",
        )
        task = asyncio.create_task(asyncio.sleep(0, result=response))
        tracking.track("intent-1", task)
        await tracking.wait_all()
        return repository

    repository = asyncio.run(scenario())

    assert repository.updates[0][0] == "intent-1"
    assert repository.updates[0][1]["state"] == "ACCEPTED"
    assert repository.updates[0][1]["broker_order_id"] == "order-1"


class CommissionProfiles:
    def __init__(self) -> None:
        self.observations = []

    def observe_execution(self, key, **values):
        self.observations.append((key, values))


class Broker:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = 0

    async def get_order_state(self, account_id, broker_order_id):
        self.calls += 1
        return self.response


class Ledger:
    def __init__(self) -> None:
        self.buys = []

    def record_buy_execution(self, **values):
        self.buys.append(values)


class Portfolio:
    def __init__(self) -> None:
        self.value = None

    async def position(self, account_id, instrument_id):
        return self.value

    async def apply_position_event(self, account_id, position):
        self.value = position


class Cash:
    def __init__(self) -> None:
        self.completions = []

    async def complete(self, intent_id, state):
        self.completions.append((intent_id, state))


class FailingCash(Cash):
    async def complete(self, intent_id, state):
        raise RuntimeError("cash completion failed")


class FailsOnceCash(Cash):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(self, intent_id, state):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("cash completion failed once")
        await super().complete(intent_id, state)


class Audit:
    def __init__(self) -> None:
        self.events = []

    def record_order_stage(self, **values):
        self.events.append(values)


def test_filled_response_refines_account_commission_profile() -> None:
    async def scenario():
        repository = Repository()
        profiles = CommissionProfiles()
        tracking = OrderTrackingService(
            repository,
            now=lambda: NOW,
            broker_id="broker-1",
            commission_profiles=profiles,
        )
        response = BrokerOrderState(
            "order-1",
            "intent-1",
            "FILLED",
            1,
            1,
            Decimal("1000"),
            Decimal("1000"),
            Decimal("1"),
            Decimal("2"),
            "RUB",
            executed_price=Decimal("100"),
        )
        request = DispatchRequest(
            "intent-1",
            "account-1",
            "instrument-1",
            OrderSide.BUY,
            1,
            Decimal("100"),
            instrument_type="SHARE",
        )
        task = asyncio.create_task(asyncio.sleep(0, result=response))
        tracking.track("intent-1", task, request=request)
        await tracking.wait_all()
        return profiles

    profiles = asyncio.run(scenario())

    key, values = profiles.observations[0]
    assert key.broker_id == "broker-1"
    assert key.instrument_type == "SHARE"
    assert values["side"] == "BUY"
    assert values["order_amount"] == Decimal("1000")
    assert values["actual_commission"] == Decimal("2")


def test_filled_without_broker_timestamp_audits_local_confirmation_time() -> None:
    async def scenario():
        repository = Repository()
        audit = Audit()
        tracking = OrderTrackingService(repository, now=lambda: NOW, audit=audit)
        response = BrokerOrderState(
            "order-1",
            "intent-1",
            "FILLED",
            1,
            1,
            Decimal("1000"),
            Decimal("1000"),
            Decimal("1"),
            Decimal("1"),
            "RUB",
            executed_price=Decimal("100"),
        )
        request = DispatchRequest(
            "intent-1",
            "account",
            "instrument",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id="automation",
            process_id="process-1",
            broker_id="broker-1",
        )
        tracking.track("intent-1", asyncio.create_task(asyncio.sleep(0, result=response)), request=request)
        await tracking.wait_all()
        return audit

    audit = asyncio.run(scenario())

    terminal = next(event for event in audit.events if event["stage"].value == "BROKER_ORDER_TERMINAL_STATE")
    assert terminal["timestamp_source"] == "LOCAL_CONFIRMATION_TIME"


def test_accepted_order_is_tracked_until_fill_and_atomically_updates_durable_execution() -> None:
    async def scenario():
        accepted = BrokerOrderState(
            "order-1",
            "intent-1",
            "ACCEPTED",
            1,
            0,
            Decimal("1000"),
            Decimal(),
            Decimal("1"),
            Decimal(),
            "RUB",
        )
        filled = BrokerOrderState(
            "order-1",
            "intent-1",
            "FILLED",
            1,
            1,
            Decimal("1000"),
            Decimal("1000"),
            Decimal("1"),
            Decimal("2"),
            "RUB",
            executed_price=Decimal("100"),
        )
        repository = Repository()
        broker = Broker(filled)
        ledger = Ledger()
        portfolio = Portfolio()
        cash = Cash()
        tracking = OrderTrackingService(
            repository,
            now=lambda: NOW,
            broker=broker,
            ledger=ledger,
            portfolio=portfolio,
            cash=cash,
            sleep=lambda _delay: asyncio.sleep(0),
        )
        request = DispatchRequest(
            "intent-1",
            "account",
            "instrument",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id="automation",
            lot_size=10,
        )
        task = asyncio.create_task(asyncio.sleep(0, result=accepted))
        tracking.track("intent-1", task, request=request)
        await tracking.wait_all()
        return repository, broker, ledger, portfolio, cash

    repository, broker, ledger, portfolio, cash = asyncio.run(scenario())

    assert broker.calls == 1
    assert repository.updates[-1][1]["state"] == "FILLED"
    assert repository.finalizations[0].intent_id == "intent-1"
    assert ledger.buys == []
    assert portfolio.value.quantity_lots == Decimal("1")
    assert cash.completions == [("intent-1", "FILLED")]
    assert repository.execution_events[0]["automation_id"] == "automation"
    assert repository.execution_events[0]["operation"]["broker_execution_id"] == "order-1"
    assert repository.execution_events[0]["position_snapshot"]["quantity_lots"] == 1


def test_failed_atomic_finalization_keeps_cash_gate_and_portfolio_unchanged() -> None:
    async def scenario():
        repository = Repository()
        repository.finalization_error = RuntimeError("database commit failed")
        portfolio = Portfolio()
        cash = Cash()
        gate = ActiveIntentGateService()
        gate.activate("automation", "intent-1")
        tracking = OrderTrackingService(
            repository,
            now=lambda: NOW,
            portfolio=portfolio,
            cash=cash,
            active_intents=gate,
        )
        response = BrokerOrderState(
            "order-1",
            "intent-1",
            "FILLED",
            1,
            1,
            Decimal("1000"),
            Decimal("1000"),
            Decimal("1"),
            Decimal("2"),
            "RUB",
            executed_price=Decimal("100"),
            executed_at=NOW,
        )
        request = DispatchRequest(
            "intent-1",
            "account",
            "instrument",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id="automation",
            lot_size=10,
        )

        with pytest.raises(RuntimeError, match="database commit failed"):
            await tracking._finish(
                "intent-1",
                asyncio.create_task(asyncio.sleep(0, result=response)),
                request,
            )
        return portfolio, cash, gate

    portfolio, cash, gate = asyncio.run(scenario())

    assert portfolio.value is None
    assert cash.completions == []
    assert gate.has_active_intent("automation") is True


def test_sell_execution_snapshot_includes_realized_pnl_from_current_lifo_allocation() -> None:
    async def scenario():
        repository = Repository()
        portfolio = Portfolio()
        portfolio.value = BrokerPosition("instrument", Decimal("1"), Decimal("100"), Decimal("100"), "RUB")
        tracking = OrderTrackingService(repository, now=lambda: NOW, portfolio=portfolio)
        response = BrokerOrderState(
            "order-1",
            "intent-1",
            "FILLED",
            1,
            1,
            Decimal("1100"),
            Decimal("1100"),
            Decimal("1"),
            Decimal("2"),
            "RUB",
            executed_price=Decimal("110"),
            executed_at=NOW,
        )
        request = DispatchRequest(
            "intent-1",
            "account",
            "instrument",
            OrderSide.SELL,
            1,
            Decimal("110"),
            automation_id="automation",
            lot_size=10,
        )

        await tracking._finish("intent-1", asyncio.create_task(asyncio.sleep(0, result=response)), request)
        return repository.execution_events[0]["position_snapshot"]

    position_snapshot = asyncio.run(scenario())

    assert position_snapshot["realized_pnl"] == "97"


def test_buy_execution_replay_does_not_repeat_post_commit_side_effects() -> None:
    async def scenario():
        repository = Repository()
        portfolio = Portfolio()
        cash = Cash()
        gate = ActiveIntentGateService()
        gate.activate("automation", "intent-1")
        tracking = OrderTrackingService(
            repository,
            now=lambda: NOW,
            portfolio=portfolio,
            cash=cash,
            active_intents=gate,
        )
        response = BrokerOrderState(
            "order-1",
            "intent-1",
            "FILLED",
            1,
            1,
            Decimal("1000"),
            Decimal("1000"),
            Decimal("1"),
            Decimal("2"),
            "RUB",
            executed_price=Decimal("100"),
            executed_at=NOW,
        )
        request = DispatchRequest(
            "intent-1",
            "account",
            "instrument",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id="automation",
            lot_size=10,
        )
        await tracking._finish("intent-1", asyncio.create_task(asyncio.sleep(0, result=response)), request)
        repository.finalization_applied = False
        await tracking._finish("intent-1", asyncio.create_task(asyncio.sleep(0, result=response)), request)
        return portfolio, cash, gate

    portfolio, cash, gate = asyncio.run(scenario())

    assert portfolio.value.quantity_lots == 1
    assert cash.completions == [("intent-1", "FILLED")]
    assert gate.has_active_intent("automation") is False


def test_gate_remains_active_when_post_commit_cash_completion_fails() -> None:
    async def scenario():
        repository = Repository()
        gate = ActiveIntentGateService()
        gate.activate("automation", "intent-1")
        tracking = OrderTrackingService(
            repository,
            now=lambda: NOW,
            cash=FailingCash(),
            active_intents=gate,
        )
        response = BrokerOrderState(
            "order-1",
            "intent-1",
            "REJECTED",
            1,
            0,
            Decimal("1000"),
            Decimal(),
            Decimal("1"),
            Decimal(),
            "RUB",
        )
        request = DispatchRequest(
            "intent-1",
            "account",
            "instrument",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id="automation",
            lot_size=10,
        )
        with pytest.raises(RuntimeError, match="cash completion failed"):
            await tracking._finish("intent-1", asyncio.create_task(asyncio.sleep(0, result=response)), request)
        return gate

    gate = asyncio.run(scenario())

    assert gate.has_active_intent("automation") is True


def test_tracking_retries_post_commit_cleanup_without_replaying_finalization() -> None:
    async def scenario():
        repository = Repository()
        cash = FailsOnceCash()
        gate = ActiveIntentGateService()
        gate.activate("automation", "intent-1")
        tracking = OrderTrackingService(
            repository,
            now=lambda: NOW,
            cash=cash,
            active_intents=gate,
        )
        response = BrokerOrderState(
            "order-1",
            "intent-1",
            "REJECTED",
            1,
            0,
            Decimal("1000"),
            Decimal(),
            Decimal("1"),
            Decimal(),
            "RUB",
        )
        request = DispatchRequest(
            "intent-1",
            "account",
            "instrument",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id="automation",
        )
        tracking.track(
            "intent-1",
            asyncio.create_task(asyncio.sleep(0, result=response)),
            request=request,
        )
        await tracking.wait_all()
        return repository, cash, gate

    repository, cash, gate = asyncio.run(scenario())

    assert len(repository.finalizations) == 1
    assert repository.finalizations[0].intent_id == "intent-1"
    assert cash.calls == 2
    assert cash.completions == [("intent-1", "REJECTED")]
    assert gate.has_active_intent("automation") is False


def test_permanent_cleanup_failure_holds_only_affected_automation() -> None:
    async def scenario():
        failing_repository = Repository()
        failing_audit = Audit()
        failing_gate = ActiveIntentGateService()
        failing_gate.activate("automation", "intent-1")
        failing_tracking = OrderTrackingService(
            failing_repository,
            now=lambda: NOW,
            audit=failing_audit,
            cash=FailingCash(),
            active_intents=failing_gate,
        )
        healthy_repository = Repository()
        healthy_gate = ActiveIntentGateService()
        healthy_gate.activate("automation-2", "intent-2")
        healthy_tracking = OrderTrackingService(
            healthy_repository,
            now=lambda: NOW,
            cash=Cash(),
            active_intents=healthy_gate,
        )
        failing_response = BrokerOrderState(
            "order-1",
            "intent-1",
            "REJECTED",
            1,
            0,
            Decimal("1000"),
            Decimal(),
            Decimal("1"),
            Decimal(),
            "RUB",
        )
        healthy_response = BrokerOrderState(
            "order-2",
            "intent-2",
            "REJECTED",
            1,
            0,
            Decimal("1000"),
            Decimal(),
            Decimal("1"),
            Decimal(),
            "RUB",
        )
        failing_request = DispatchRequest(
            "intent-1",
            "account",
            "instrument",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id="automation",
            broker_id="broker",
            process_id="process-1",
        )
        healthy_request = DispatchRequest(
            "intent-2",
            "account",
            "instrument",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id="automation-2",
        )
        failing_tracking.track(
            "intent-1",
            asyncio.create_task(asyncio.sleep(0, result=failing_response)),
            request=failing_request,
        )
        healthy_tracking.track(
            "intent-2",
            asyncio.create_task(asyncio.sleep(0, result=healthy_response)),
            request=healthy_request,
        )
        await failing_tracking.wait_all()
        await healthy_tracking.wait_all()
        return failing_repository, failing_audit, failing_gate, healthy_gate

    failing_repository, failing_audit, failing_gate, healthy_gate = asyncio.run(scenario())

    assert failing_repository.holds == [("Post-commit terminal cleanup failed", "automation")]
    assert failing_gate.has_active_intent("automation") is True
    assert healthy_gate.has_active_intent("automation-2") is False
    assert len(failing_audit.events) == 1
    event = failing_audit.events[0]
    assert event["stage"].value == "AUTOMATION_MOVED_TO_HOLD"
    assert event["process_id"] == "process-1"
    assert event["automation_id"] == "automation"
    assert event["critical"] is True
    assert event["reason_code"] == "POST_COMMIT_CLEANUP_FAILED"
    assert event["exception_type"] == "RuntimeError"


def test_real_repository_buy_replay_does_not_double_portfolio(tmp_path) -> None:
    async def scenario():
        engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
        Base.metadata.create_all(engine)
        repository = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
        command_value = command(broker="broker-1", account="account-1", instrument="instrument-1")
        automation_id = str(command_value.automation_id)
        broker_id = str(command_value.broker_id)
        repository.cache_command(command_value)
        repository.save_decision_batch((decision_item(command_value),), occurred_at=NOW)
        portfolio = Portfolio()
        cash = Cash()
        tracking = OrderTrackingService(repository, now=lambda: NOW, portfolio=portfolio, cash=cash)
        response = BrokerOrderState(
            "order-1",
            BASELINE_INTENT_ID,
            "FILLED",
            1,
            1,
            Decimal("1000"),
            Decimal("1000"),
            Decimal("1"),
            Decimal("2"),
            "RUB",
            executed_price=Decimal("100"),
        )
        request = DispatchRequest(
            BASELINE_INTENT_ID,
            "account-1",
            "instrument-1",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id=automation_id,
            broker_id=broker_id,
            lot_size=10,
        )
        await tracking._finish(
            BASELINE_INTENT_ID,
            asyncio.create_task(asyncio.sleep(0, result=response)),
            request,
        )
        await tracking._finish(
            BASELINE_INTENT_ID,
            asyncio.create_task(asyncio.sleep(0, result=response)),
            request,
        )
        return portfolio, cash

    portfolio, cash = asyncio.run(scenario())

    assert portfolio.value.quantity_lots == 1
    assert cash.completions == [(BASELINE_INTENT_ID, "FILLED")]


def test_poll_limit_keeps_intent_uncertain_and_holds_automation() -> None:
    async def scenario():
        accepted = BrokerOrderState(
            "order-1",
            "intent-1",
            "ACCEPTED",
            1,
            0,
            Decimal("1000"),
            Decimal(),
            Decimal("1"),
            Decimal(),
            "RUB",
        )
        repository = Repository()
        cash = Cash()
        tracking = OrderTrackingService(
            repository,
            now=lambda: NOW,
            broker=Broker(accepted),
            sleep=lambda _delay: asyncio.sleep(0),
            poll_limit=1,
            cash=cash,
        )
        request = DispatchRequest(
            "intent-1",
            "account",
            "instrument",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id="automation",
        )
        tracking.track(
            "intent-1",
            asyncio.create_task(asyncio.sleep(0, result=accepted)),
            request=request,
        )
        await tracking.wait_all()
        return repository, cash

    repository, cash = asyncio.run(scenario())

    assert repository.updates[-1][1]["state"] == "UNCERTAIN"
    assert all(values["state"] != "ACCEPTED" for _intent, values in repository.updates)
    assert repository.holds == [("Broker order status polling exhausted", "automation")]
    assert cash.completions == [("intent-1", "UNCERTAIN")]


def test_filled_without_executed_lots_becomes_uncertain_without_ledger_update() -> None:
    async def scenario():
        repository = Repository()
        ledger = Ledger()
        cash = Cash()
        tracking = OrderTrackingService(
            repository,
            now=lambda: NOW,
            ledger=ledger,
            cash=cash,
        )
        response = BrokerOrderState(
            "order-1",
            "intent-1",
            "FILLED",
            1,
            0,
            Decimal("1000"),
            Decimal("1000"),
            Decimal("1"),
            Decimal("1"),
            "RUB",
            executed_price=Decimal(),
        )
        request = DispatchRequest(
            "intent-1",
            "account",
            "instrument",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id="automation",
            lot_size=10,
        )
        tracking.track(
            "intent-1",
            asyncio.create_task(asyncio.sleep(0, result=response)),
            request=request,
        )
        await tracking.wait_all()
        return repository, ledger, cash

    repository, ledger, cash = asyncio.run(scenario())

    assert repository.updates[-1][1]["state"] == "UNCERTAIN"
    assert ledger.buys == []
    assert cash.completions == [("intent-1", "UNCERTAIN")]


def test_successful_terminal_pipeline_clears_matching_active_intent() -> None:
    async def scenario():
        repository = Repository()
        gate = ActiveIntentGateService()
        gate.activate("automation", "intent-1")
        tracking = OrderTrackingService(repository, now=lambda: NOW, active_intents=gate)
        response = BrokerOrderState(
            "order-1",
            "intent-1",
            "REJECTED",
            1,
            0,
            Decimal("1000"),
            Decimal(),
            Decimal("1"),
            Decimal(),
            "RUB",
        )
        request = DispatchRequest(
            "intent-1",
            "account",
            "instrument",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id="automation",
        )
        tracking.track(
            "intent-1",
            asyncio.create_task(asyncio.sleep(0, result=response)),
            request=request,
        )
        await tracking.wait_all()
        return gate

    gate = asyncio.run(scenario())

    assert gate.has_active_intent("automation") is False


def test_uncertain_pipeline_keeps_active_intent() -> None:
    async def scenario():
        repository = Repository()
        gate = ActiveIntentGateService()
        gate.activate("automation", "intent-1")
        tracking = OrderTrackingService(
            repository,
            now=lambda: NOW,
            broker=Broker(
                BrokerOrderState(
                    "order-1",
                    "intent-1",
                    "ACCEPTED",
                    1,
                    0,
                    Decimal("1000"),
                    Decimal(),
                    Decimal("1"),
                    Decimal(),
                    "RUB",
                )
            ),
            sleep=lambda _delay: asyncio.sleep(0),
            poll_limit=1,
            active_intents=gate,
        )
        request = DispatchRequest(
            "intent-1",
            "account",
            "instrument",
            OrderSide.BUY,
            1,
            Decimal("100"),
            automation_id="automation",
        )
        tracking.track(
            "intent-1",
            asyncio.create_task(
                asyncio.sleep(
                    0,
                    result=BrokerOrderState(
                        "order-1",
                        "intent-1",
                        "ACCEPTED",
                        1,
                        0,
                        Decimal("1000"),
                        Decimal(),
                        Decimal("1"),
                        Decimal(),
                        "RUB",
                    ),
                )
            ),
            request=request,
        )
        await tracking.wait_all()
        return gate

    gate = asyncio.run(scenario())

    assert gate.has_active_intent("automation") is True
