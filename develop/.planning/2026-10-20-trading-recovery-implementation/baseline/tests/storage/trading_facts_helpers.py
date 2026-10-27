"""Synthetic baseline trading fact values."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from moex_sentinel.storage.models import (
    BrokerInstrumentModel,
    BrokerOrderModel,
    PositionCycleModel,
    TradeDecisionModel,
    TradeExecutionModel,
    TradingAutomationModel,
    UserBrokerModel,
)

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


def user_broker_model(record_id: str, account_id: str) -> UserBrokerModel:
    return UserBrokerModel(
        id=record_id,
        api_slug="t_invest",
        display_name=record_id,
        environment="TEST",
        fqdn="sandbox-invest-public-api.tbank.ru:443",
        settings={"token": "synthetic-token"},
        external_account_id=account_id,
        state="ACTIVE",
        created_at=NOW,
        updated_at=NOW,
    )


def instrument_model(record_id: str, user_broker_id: str) -> BrokerInstrumentModel:
    return BrokerInstrumentModel(
        id=record_id,
        user_broker_id=user_broker_id,
        external_instrument_id=f"external-{record_id}",
        external_identifiers={},
        ticker=record_id.upper(),
        name=f"Synthetic {record_id}",
        instrument_type="SHARE",
        class_code="TQBR",
        currency="RUB",
        lot_size=10,
        min_price_increment=Decimal("0.01"),
        api_trade_available=True,
        is_active=True,
        is_selected=True,
        first_seen_at=NOW,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def seed_two_scopes(session: Session) -> None:
    session.add_all(
        [
            user_broker_model("scope-1", "account-1"),
            user_broker_model("scope-2", "account-2"),
        ]
    )
    session.flush()
    session.add_all(
        [
            instrument_model("instrument-1", "scope-1"),
            instrument_model("instrument-2", "scope-2"),
        ]
    )
    session.flush()


def automation_model(
    record_id: str,
    *,
    user_broker_id: str = "scope-1",
    instrument_id: str = "instrument-1",
    state: str = "IN_WORK",
    closed: bool = False,
) -> TradingAutomationModel:
    return TradingAutomationModel(
        id=record_id,
        user_broker_id=user_broker_id,
        instrument_id=instrument_id,
        state=state,
        suspended_from_state=None,
        hold_reason=None,
        revision=1,
        last_sequence_number=0,
        resume_requested=False,
        closed_at=NOW if closed else None,
        created_at=NOW,
        updated_at=NOW,
    )


def position_cycle_model(
    record_id: str,
    *,
    state: str = "OPEN",
    closed: bool = False,
) -> PositionCycleModel:
    return PositionCycleModel(
        id=record_id,
        user_broker_id="scope-1",
        automation_id="automation-1",
        instrument_id="instrument-1",
        state=state,
        quantity_lots=1,
        average_entry_price=Decimal("10"),
        invested_amount=Decimal("10"),
        realized_pnl=Decimal(),
        unrealized_pnl=Decimal(),
        net_pnl=Decimal(),
        accumulated_commissions=Decimal("0.01"),
        opened_at=NOW,
        closed_at=NOW if closed else None,
        created_at=NOW,
        updated_at=NOW,
    )


def seed_automation(session: Session) -> None:
    seed_two_scopes(session)
    session.add(automation_model("automation-1"))
    session.flush()


def seed_cycle(session: Session) -> None:
    seed_automation(session)
    session.add(position_cycle_model("cycle-1"))
    session.flush()


def decision_model(
    record_id: str = "decision-1",
    *,
    user_broker_id: str = "scope-1",
    automation_id: str = "automation-1",
    instrument_id: str = "instrument-1",
    fact_id: str = "fact-decision-1",
) -> TradeDecisionModel:
    return TradeDecisionModel(
        id=record_id,
        fact_id=fact_id,
        process_id="process-1",
        user_broker_id=user_broker_id,
        automation_id=automation_id,
        position_cycle_id="cycle-1",
        instrument_id=instrument_id,
        quantity_lots=1,
        lot_size=10,
        average_price=Decimal("10"),
        invested_amount=Decimal("10"),
        current_price=Decimal("10.25"),
        best_bid=Decimal("10.24"),
        best_ask=Decimal("10.26"),
        indicators={"signal": "synthetic"},
        estimated_commission=Decimal("0.01"),
        decision="BUY_MORE",
        reason_code="SYNTHETIC_SIGNAL",
        requested_quantity_lots=1,
        limit_price=Decimal("10.25"),
        strategy_snapshot={"version": 1},
        decided_at=NOW,
        created_at=NOW,
    )


def order_model(
    record_id: str,
    *,
    decision_id: str = "decision-1",
    user_broker_id: str = "scope-1",
    automation_id: str = "automation-1",
    instrument_id: str = "instrument-1",
    fact_id: str | None = None,
    idempotency_key: str | None = None,
) -> BrokerOrderModel:
    return BrokerOrderModel(
        id=record_id,
        fact_id=fact_id or f"fact-{record_id}",
        user_broker_id=user_broker_id,
        automation_id=automation_id,
        decision_id=decision_id,
        position_cycle_id="cycle-1",
        instrument_id=instrument_id,
        idempotency_key=idempotency_key or f"idempotency-{record_id}",
        external_order_id=None,
        intent_kind="BUY_MORE",
        side="BUY",
        order_type="LIMIT",
        state="CREATED",
        quantity_lots=1,
        limit_price=Decimal("10.25"),
        requested_amount=Decimal("10.25"),
        executed_amount=Decimal(),
        estimated_commission=Decimal("0.01"),
        executed_commission=Decimal(),
        strategy_snapshot={"version": 1},
        dispatch_started_at=None,
        broker_responded_at=None,
        executed_at=None,
        terminal_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def execution_model(
    record_id: str,
    *,
    external_execution_id: str | None,
    source: str,
) -> TradeExecutionModel:
    return TradeExecutionModel(
        id=record_id,
        fact_id=f"fact-{record_id}",
        user_broker_id="scope-1",
        automation_id="automation-1",
        broker_order_id="order-1",
        position_cycle_id="cycle-1",
        instrument_id="instrument-1",
        external_execution_id=external_execution_id,
        side="BUY",
        executed_lots=1,
        price=Decimal("10.25"),
        value=Decimal("10.25"),
        broker_commission=Decimal("0.01"),
        other_fees=Decimal(),
        currency="RUB",
        source=source,
        executed_at=NOW,
        created_at=NOW,
    )


def seed_order(session: Session) -> None:
    seed_cycle(session)
    session.add(decision_model())
    session.flush()
    session.add(order_model("order-1"))
    session.flush()


def seed_buy_and_sell_executions(session: Session) -> None:
    seed_order(session)
    buy = execution_model("execution-buy", external_execution_id="external-buy", source="BROKER_FILL")
    sell = execution_model("execution-sell", external_execution_id="external-sell", source="BROKER_FILL")
    sell.side = "SELL"
    session.add_all([buy, sell])
    session.flush()
