"""Worker candle metadata must preserve Core's immutable order snapshot."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from moex_sentinel.storage.models import BrokerOrderModel, PositionLotModel, TradeExecutionModel
from tests.integration.test_core_worker_lifecycle import (
    NOW,
    LifecycleContext,
)
from tests.integration.test_core_worker_lifecycle import (
    lifecycle as lifecycle_fixture,
)
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.test_local_repository import INTENT_ID, filled_buy

lifecycle = lifecycle_fixture


@pytest.mark.parametrize("separate_initial_delivery", [False, True])
def test_worker_fill_with_private_candle_metadata_is_accepted_and_replayed_by_core(
    lifecycle: LifecycleContext, separate_initial_delivery: bool
) -> None:
    command = lifecycle.sync.claim_commands("synthetic-worker", 10)[0]
    lifecycle.propose("IN_WORK")
    assert lifecycle.sync.flush_outbox()
    candle_at = NOW - timedelta(minutes=1)
    strategy_snapshot = {"strategy_code": "ADAPTIVE_SCALPING", "strategy_version": "1.0"}
    item = decision_item(command).model_copy(
        update={
            "strategy_snapshot": strategy_snapshot,
            "indicators": {"last_candle_at": candle_at.isoformat()},
        }
    )
    lifecycle.worker.save_decision_batch((item,), occurred_at=NOW)
    initial = lifecycle.pending_facts()
    if separate_initial_delivery:
        assert lifecycle.core.publish_facts(initial).failures == ()
    lifecycle.worker.update_intent(INTENT_ID, state="SUBMITTING", occurred_at=NOW + timedelta(milliseconds=2))
    lifecycle.worker.finalize_execution(
        filled_buy().model_copy(
            update={
                "automation_id": str(command.automation_id),
                "broker_id": str(command.broker_id),
                "account_id": command.account_id,
                "instrument_id": command.external_instrument_id,
                "lot_size": command.lot_size,
                "requested_amount": Decimal("100") * command.lot_size,
                "executed_amount": Decimal("100") * command.lot_size,
                "occurred_at": NOW + timedelta(milliseconds=3),
                "executed_at": NOW + timedelta(milliseconds=3),
                "terminal_at": NOW + timedelta(milliseconds=3),
            }
        )
    )
    facts = lifecycle.pending_facts()
    accepted = lifecycle.core.publish_facts(facts)

    assert accepted.failures == ()
    assert accepted.results[0].accepted_event_ids == tuple(fact.event_id for fact in facts)
    assert lifecycle.core.publish_facts(facts) == accepted
    assert lifecycle.sync.flush_outbox()
    assert lifecycle.pending_facts() == []
    with lifecycle.core_factory() as session:
        order = session.get_one(BrokerOrderModel, INTENT_ID)
        assert order.strategy_snapshot == strategy_snapshot
        assert session.scalar(select(func.count()).select_from(TradeExecutionModel)) == 1
        assert session.scalar(select(func.count()).select_from(PositionLotModel)) == 1
    cycle = lifecycle.worker.get_cycle_state(str(command.automation_id), now=NOW)
    assert cycle.last_buy_candle_at == candle_at
