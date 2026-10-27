# D1 core: post-minor final delta vs original baseline
Only changes since accepted feature freeze: src/moex_sentinel/storage/repositories/order_facts.py, tests/storage/test_execution_diagnostics.py

## src/moex_sentinel/storage/repositories/order_facts.py

```diff
--- baseline/src/moex_sentinel/storage/repositories/order_facts.py
+++ src/moex_sentinel/storage/repositories/order_facts.py
@@ -23,6 +23,7 @@
     TradeExecutionModel,
 )
 from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
+from moex_sentinel.storage.models.trading_observability import AutomationEventModel
 from moex_sentinel.storage.repositories.trading_facts_support import (
     append_idempotent,
     flush_or_translate,
@@ -416,6 +417,182 @@
             raise TradingFactPersistenceError(TradingFactErrorCode.NOT_FOUND, entity_type="broker_order")
         return _order_value(model)
 
+    def execution_diagnostics(
+        self, user_broker_id: str, automation_id: str, *, limit: int, intent_id: str | None = None
+    ):
+        """Read a bounded scalar projection; deliberately does not acquire row locks."""
+        orders = self._session.execute(
+            select(
+                BrokerOrderModel.idempotency_key,
+                BrokerOrderModel.fact_id,
+                BrokerOrderModel.id,
+                BrokerOrderModel.decision_id,
+                TradeDecisionModel.process_id,
+                TradeDecisionModel.decision,
+                TradeDecisionModel.reason_code,
+                TradeDecisionModel.lot_size,
+                BrokerOrderModel.external_order_id,
+                BrokerOrderModel.state,
+                BrokerOrderModel.side,
+                BrokerOrderModel.quantity_lots,
+                BrokerOrderModel.limit_price,
+                BrokerOrderModel.requested_amount,
+                BrokerOrderModel.executed_amount,
+                BrokerOrderModel.estimated_commission,
+                BrokerOrderModel.executed_commission,
+                BrokerOrderModel.created_at,
+                BrokerOrderModel.dispatch_started_at,
+                BrokerOrderModel.broker_responded_at,
+                BrokerOrderModel.executed_at,
+                BrokerOrderModel.terminal_at,
+            )
+            .where(
+                BrokerOrderModel.user_broker_id == user_broker_id,
+                BrokerOrderModel.automation_id == automation_id,
+                *(() if intent_id is None else (BrokerOrderModel.idempotency_key == intent_id,)),
+            )
+            .outerjoin(
+                TradeDecisionModel,
+                (TradeDecisionModel.user_broker_id == BrokerOrderModel.user_broker_id)
+                & (TradeDecisionModel.id == BrokerOrderModel.decision_id),
+            )
+            .order_by(BrokerOrderModel.created_at.desc(), BrokerOrderModel.id)
+            .limit(limit + 1)
+        ).all()
+        truncated = len(orders) > limit
+        orders = orders[:limit]
+        order_ids = [row.id for row in orders]
+        facts = [row.fact_id for row in orders]
+        events = []
+        executions = []
+        envelopes = []
+        if order_ids:
+            events = self._session.execute(
+                select(
+                    BrokerOrderEventModel.id,
+                    BrokerOrderEventModel.fact_id,
+                    BrokerOrderEventModel.broker_order_id,
+                    BrokerOrderEventModel.to_state,
+                    BrokerOrderEventModel.safe_reason,
+                    BrokerOrderEventModel.occurred_at,
+                )
+                .where(
+                    BrokerOrderEventModel.user_broker_id == user_broker_id,
+                    BrokerOrderEventModel.automation_id == automation_id,
+                    BrokerOrderEventModel.broker_order_id.in_(order_ids),
+                )
+                .order_by(BrokerOrderEventModel.occurred_at.desc(), BrokerOrderEventModel.id)
+                .limit(limit + 1)
+            ).all()
+            executions = self._session.execute(
+                select(
+                    TradeExecutionModel.id,
+                    TradeExecutionModel.fact_id,
+                    TradeExecutionModel.broker_order_id,
+                    TradeExecutionModel.external_execution_id,
+                    TradeExecutionModel.side,
+                    TradeExecutionModel.executed_lots,
+                    TradeExecutionModel.price,
+                    TradeExecutionModel.value,
+                    TradeExecutionModel.broker_commission,
+                    TradeExecutionModel.other_fees,
+                    TradeExecutionModel.currency,
+                    TradeExecutionModel.executed_at,
+                )
+                .where(
+                    TradeExecutionModel.user_broker_id == user_broker_id,
+                    TradeExecutionModel.automation_id == automation_id,
+                    TradeExecutionModel.broker_order_id.in_(order_ids),
+                )
+                .order_by(TradeExecutionModel.executed_at.desc(), TradeExecutionModel.id)
+                .limit(limit + 1)
+            ).all()
+            facts.extend(row.fact_id for row in events[:limit])
+            facts.extend(row.fact_id for row in executions[:limit])
+            envelopes = self._session.execute(
+                select(
+                    AutomationEventModel.event_id,
+                    AutomationEventModel.sequence_number,
+                    AutomationEventModel.fact_kind,
+                    AutomationEventModel.received_at,
+                )
+                .where(
+                    AutomationEventModel.user_broker_id == user_broker_id,
+                    AutomationEventModel.automation_id == automation_id,
+                    AutomationEventModel.event_id.in_(facts),
+                )
+                .order_by(AutomationEventModel.sequence_number)
+                .limit(3 * limit)
+            ).all()
+        return {
+            "truncated": truncated or len(events) > limit or len(executions) > limit,
+            "orders": tuple(
+                {
+                    "intent_id": row.idempotency_key,
+                    "fact_id": row.fact_id,
+                    "order_id": row.id,
+                    "decision_id": row.decision_id,
+                    "process_id": row.process_id,
+                    "decision": row.decision,
+                    "reason_code": row.reason_code,
+                    "lot_size": row.lot_size,
+                    "external_order_id": row.external_order_id,
+                    "state": row.state,
+                    "side": row.side,
+                    "quantity_lots": row.quantity_lots,
+                    "limit_price": row.limit_price,
+                    "requested_amount": row.requested_amount,
+                    "executed_amount": row.executed_amount,
+                    "estimated_commission": row.estimated_commission,
+                    "executed_commission": row.executed_commission,
+                    "created_at": row.created_at,
+                    "dispatch_started_at": row.dispatch_started_at,
+                    "broker_responded_at": row.broker_responded_at,
+                    "executed_at": row.executed_at,
+                    "terminal_at": row.terminal_at,
+                }
+                for row in orders
+            ),
+            "events": tuple(
+                {
+                    "event_id": row.id,
+                    "fact_id": row.fact_id,
+                    "order_id": row.broker_order_id,
+                    "to_state": row.to_state,
+                    "safe_reason": row.safe_reason,
+                    "occurred_at": row.occurred_at,
+                }
+                for row in events[:limit]
+            ),
+            "executions": tuple(
+                {
+                    "execution_id": row.id,
+                    "fact_id": row.fact_id,
+                    "order_id": row.broker_order_id,
+                    "external_execution_id": row.external_execution_id,
+                    "side": row.side,
+                    "executed_lots": row.executed_lots,
+                    "price": row.price,
+                    "value": row.value,
+                    "broker_commission": row.broker_commission,
+                    "other_fees": row.other_fees,
+                    "currency": row.currency,
+                    "executed_at": row.executed_at,
+                }
+                for row in executions[:limit]
+            ),
+            "accepted_envelopes": tuple(
+                {
+                    "fact_id": row.event_id,
+                    "event_id": row.event_id,
+                    "sequence_number": row.sequence_number,
+                    "fact_kind": row.fact_kind,
+                    "received_at": row.received_at,
+                }
+                for row in envelopes[: 3 * limit]
+            ),
+        }
+
     def list_order_events(self, user_broker_id: str, order_id: str) -> tuple[BrokerOrderEventDraft, ...]:
         statement = (
             select(BrokerOrderEventModel)
```

## tests/storage/test_execution_diagnostics.py

```diff
--- baseline/tests/storage/test_execution_diagnostics.py
+++ tests/storage/test_execution_diagnostics.py
@@ -0,0 +1,221 @@
+from decimal import Decimal
+
+from sqlalchemy import create_engine, event, select
+from sqlalchemy.orm import sessionmaker
+
+from moex_sentinel.storage.models import Base
+from moex_sentinel.storage.models.order_facts import BrokerOrderEventModel, BrokerOrderModel
+from moex_sentinel.storage.models.trading_admission import TradingSubmitReservationModel
+from moex_sentinel.storage.models.trading_observability import AutomationEventModel
+from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
+from tests.storage.trading_facts_helpers import NOW, execution_model, seed_order
+
+
+def test_execution_diagnostics_three_exact_facts_at_limit_one_are_not_truncated(tmp_path):
+    engine = create_engine(f"sqlite:///{tmp_path / 'execution-complete.db'}")
+    Base.metadata.create_all(engine)
+    factory = sessionmaker(engine, expire_on_commit=False)
+    with factory.begin() as session:
+        seed_order(session)
+        session.scalar(select(BrokerOrderModel)).fact_id = "order-envelope"
+        execution = execution_model("execution-1", external_execution_id="fill-1", source="BROKER_FILL")
+        execution.fact_id = "execution-envelope"
+        session.add(execution)
+        session.add(
+            BrokerOrderEventModel(
+                id="state-1",
+                fact_id="state-envelope",
+                user_broker_id="scope-1",
+                automation_id="automation-1",
+                broker_order_id="order-1",
+                from_state="CREATED",
+                to_state="ACCEPTED",
+                safe_reason="BROKER_ACCEPTED",
+                safe_message="accepted",
+                occurred_at=NOW,
+                created_at=NOW,
+            )
+        )
+        for fact_id, sequence, kind in (
+            ("order-envelope", 1, "BROKER_ORDER_RECORDED"),
+            ("state-envelope", 2, "BROKER_ORDER_STATE_CHANGED"),
+            ("execution-envelope", 3, "TRADE_EXECUTION_RECORDED"),
+        ):
+            session.add(
+                AutomationEventModel(
+                    event_id=fact_id,
+                    automation_id="automation-1",
+                    user_broker_id="scope-1",
+                    sequence_number=sequence,
+                    expected_revision=1,
+                    fact_kind=kind,
+                    safe_message="accepted",
+                    payload={},
+                    occurred_at=NOW,
+                    received_at=NOW,
+                )
+            )
+
+    with TradingFactsUnitOfWork(factory) as uow:
+        result = uow.orders.execution_diagnostics("scope-1", "automation-1", limit=1)
+    engine.dispose()
+
+    assert len(result["orders"]) == len(result["events"]) == len(result["executions"]) == 1
+    assert len(result["accepted_envelopes"]) == 3
+    assert result["truncated"] is False
+
+
+def test_execution_diagnostics_joins_each_fact_to_exact_accepted_envelope_read_only(tmp_path):
+    engine = create_engine(f"sqlite:///{tmp_path / 'execution-read.db'}")
+    Base.metadata.create_all(engine)
+    factory = sessionmaker(engine, expire_on_commit=False)
+    with factory.begin() as session:
+        seed_order(session)
+        session.scalar(select(BrokerOrderModel)).fact_id = "order-envelope"
+        execution = execution_model("execution-1", external_execution_id="fill-1", source="BROKER_FILL")
+        execution.fact_id = "execution-envelope"
+        session.add(execution)
+        second_execution = execution_model("execution-2", external_execution_id="fill-2", source="BROKER_FILL")
+        second_execution.fact_id = "truncated-execution-envelope"
+        session.add(second_execution)
+        session.add(
+            TradingSubmitReservationModel(
+                reservation_id="reservation-1",
+                user_broker_id="scope-1",
+                automation_id="automation-1",
+                idempotency_key="intent-1",
+                request_fingerprint="a" * 64,
+                state="UNKNOWN",
+                runtime_instance_id="runtime-1",
+                generation=2,
+                configuration_fingerprint="b" * 64,
+                side="BUY",
+                instrument_uid="TEST@TQBR",
+                quantity_lots=3,
+                limit_price=Decimal("10"),
+                requested_amount=Decimal("30"),
+                reserved_cash=Decimal("30"),
+                reserved_position_units=Decimal("0"),
+                payload={"private": "not selected"},
+                created_at=NOW,
+                updated_at=NOW,
+            )
+        )
+        session.add_all(
+            [
+                AutomationEventModel(
+                    event_id="order-envelope",
+                    automation_id="automation-1",
+                    user_broker_id="scope-1",
+                    sequence_number=4,
+                    expected_revision=1,
+                    fact_kind="BROKER_ORDER_RECORDED",
+                    safe_message="order accepted",
+                    payload={},
+                    occurred_at=NOW,
+                    received_at=NOW,
+                ),
+                AutomationEventModel(
+                    event_id="execution-envelope",
+                    automation_id="automation-1",
+                    user_broker_id="scope-1",
+                    sequence_number=9,
+                    expected_revision=1,
+                    fact_kind="TRADE_EXECUTION_RECORDED",
+                    safe_message="execution accepted",
+                    payload={},
+                    occurred_at=NOW,
+                    received_at=NOW,
+                ),
+                AutomationEventModel(
+                    event_id="state-envelope",
+                    automation_id="automation-1",
+                    user_broker_id="scope-1",
+                    sequence_number=6,
+                    expected_revision=1,
+                    fact_kind="BROKER_ORDER_STATE_CHANGED",
+                    safe_message="state accepted",
+                    payload={},
+                    occurred_at=NOW,
+                    received_at=NOW,
+                ),
+                AutomationEventModel(
+                    event_id="unlinked-latest-envelope",
+                    automation_id="automation-1",
+                    user_broker_id="scope-1",
+                    sequence_number=99,
+                    expected_revision=1,
+                    fact_kind="TRADE_EXECUTION_RECORDED",
+                    safe_message="must not look like an acknowledgement",
+                    payload={},
+                    occurred_at=NOW,
+                    received_at=NOW,
+                ),
+            ]
+        )
+        session.add(
+            BrokerOrderEventModel(
+                id="order-event-1",
+                fact_id="state-envelope",
+                user_broker_id="scope-1",
+                automation_id="automation-1",
+                broker_order_id="order-1",
+                from_state="CREATED",
+                to_state="ACCEPTED",
+                safe_reason="BROKER_ACCEPTED",
+                safe_message="accepted",
+                occurred_at=NOW,
+                created_at=NOW,
+            )
+        )
+
+    statements = []
+
+    def capture_statement(_conn, _cursor, sql, _params, _ctx, _many):
+        statements.append(sql)
+
+    event.listen(engine, "before_cursor_execute", capture_statement)
+    with TradingFactsUnitOfWork(factory) as uow:
+        result = uow.orders.execution_diagnostics("scope-1", "automation-1", limit=1)
+        scope = uow.execution_diagnostics_scope("scope-1", "automation-1", limit=20)
+    engine.dispose()
+
+    assert {row["fact_id"] for row in result["accepted_envelopes"]} == {
+        "order-envelope",
+        "execution-envelope",
+        "state-envelope",
+    }
+    assert {row["sequence_number"] for row in result["accepted_envelopes"]} == {4, 6, 9}
+    assert {row["fact_id"] for row in result["orders"]} == {"order-envelope"}
+    order = result["orders"][0]
+    assert order["lot_size"] == 10
+    assert order["decision_id"] == "decision-1"
+    assert order["process_id"] == "process-1"
+    assert order["estimated_commission"] == Decimal("0.01")
+    assert {row["fact_id"] for row in result["executions"]} == {"execution-envelope"}
+    assert result["executions"][0]["broker_commission"] == Decimal("0.01")
+    assert result["executions"][0]["other_fees"] == Decimal("0")
+    assert result["executions"][0]["execution_id"] == "execution-1"
+    assert result["executions"][0]["fact_id"] == "execution-envelope"
+    assert result["executions"][0]["execution_id"] != result["executions"][0]["fact_id"]
+    assert "unlinked-latest-envelope" not in {row["fact_id"] for row in result["accepted_envelopes"]}
+    assert len(result["events"]) == len(result["executions"]) == len(result["orders"]) == 1
+    assert result["truncated"] is True
+    assert scope is not None
+    assert scope["reservations"] == (
+        {
+            "intent_id": "intent-1",
+            "state": "UNKNOWN",
+            "side": "BUY",
+            "origin_runtime_instance_id": "runtime-1",
+            "origin_generation": 2,
+            "quantity_lots": 3,
+            "reserved_cash": Decimal("30"),
+            "reserved_position_units": Decimal("0"),
+            "created_at": NOW,
+            "updated_at": NOW,
+        },
+    )
+    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
+    assert all("FOR UPDATE" not in sql.upper() for sql in statements)
+    assert not any("strategy_snapshot" in sql.lower() or "payload" in sql.lower() for sql in statements)
```
