# Core Trading Facts Shadow Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build milestone `0.2.1.2`: the complete user-broker-scoped Core trading-fact shadow schema, typed aggregate repositories and one atomic unit of work without changing legacy runtime paths.

**Status:** completed and verified on 2026-08-12. Alembic head is
`a0f212000001`; the next unimplemented milestone is `0.2.1.3`. Trading-history
transfer has not occurred and remains `0.2.1.4`.

**Architecture:** One Alembic expansion revision creates every `_v2` fact and analytical table after `a0f211000001`. SQLAlchemy-independent frozen records and Protocol ports define the boundary; five session-bound repository adapters are composed by `TradingFactsShadowUnitOfWork`, which owns the only commit or rollback.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy 2, Alembic, SQLite, PostgreSQL 16, pytest, mypy strict, Ruff, Black, uv, Docker Compose.

## Global Constraints

- Follow `docs/superpowers/specs/2026-08-12-core-trading-facts-shadow-schema-design.md` and the canonical entity fields in `docs/superpowers/specs/2026-08-10-trading-data-schema-migration-design.md`.
- Use the `_v2` suffix for every table introduced by this milestone.
- Use `user_brokers_v2` and `broker_instruments_v2` as existing parents; do not create replacement parent catalogs.
- Do not register shadow repositories in current API/usecase composition roots.
- Do not dual-write, copy, reconcile, rename, delete or mutate any legacy trading table or row.
- Every repository method requires `user_broker_id`; no unscoped lookup is allowed.
- Every scoped relationship is guarded by a composite foreign key containing `user_broker_id`.
- Immutable facts permit exact idempotent retry and reject the same identity with different normalized content.
- Repositories bound to a unit of work may flush but must never commit or close its session.
- Use portable `String(36)` UUIDs, `UTCDateTime`, SQLAlchemy `JSON` and shared `Numeric(28, 9)` money columns.
- Partial indexes must declare equivalent `sqlite_where` and `postgresql_where` predicates.
- Use only synthetic local test values.
- Run targeted tests after each RED/GREEN cycle; run the complete regression at Tasks 5 and 10.
- Do not weaken existing assertions, lint, formatting or strict mypy configuration.
- Do not create Git commits.

## File Map

| Responsibility | File |
|---|---|
| Frozen records, drafts, enums and typed persistence errors | `src/moex_sentinel/domain/trading_facts.py` |
| Repository and unit-of-work Protocols | `src/moex_sentinel/services/trading_fact_ports.py` |
| Automation, strategy and cycle ORM models | `src/moex_sentinel/storage/models/automation_facts_v2.py` |
| Decision, order, event and execution ORM models | `src/moex_sentinel/storage/models/order_facts_v2.py` |
| Lot and allocation ORM models | `src/moex_sentinel/storage/models/position_facts_v2.py` |
| Audit and accepted-envelope ORM models | `src/moex_sentinel/storage/models/trading_observability_v2.py` |
| Fee profile and analytical snapshot ORM models | `src/moex_sentinel/storage/models/trading_analytics_v2.py` |
| Shared ORM exports | `src/moex_sentinel/storage/models/__init__.py` |
| Atomic schema expansion | `alembic/versions/a0f212000001_add_core_trading_fact_shadow_schema.py` |
| Shared repository comparison and constraint translation | `src/moex_sentinel/storage/repositories/trading_facts_support.py` |
| Automation/strategy adapter | `src/moex_sentinel/storage/repositories/automation_facts_v2.py` |
| Decision/order/execution adapter | `src/moex_sentinel/storage/repositories/order_facts_v2.py` |
| Cycle/lot/allocation adapter | `src/moex_sentinel/storage/repositories/position_ledger_v2.py` |
| Audit/envelope adapter | `src/moex_sentinel/storage/repositories/trading_audit_v2.py` |
| Fee/snapshot adapter | `src/moex_sentinel/storage/repositories/trading_analytics_v2.py` |
| Session and transaction owner | `src/moex_sentinel/storage/repositories/trading_facts_uow.py` |
| Shared synthetic builders | `tests/storage/trading_facts_v2_helpers.py` |
| Domain contract tests | `tests/domain/test_trading_facts.py` |
| Model and constraint tests | `tests/storage/test_trading_facts_v2_models.py` |
| Migration round-trip tests | `tests/migrations/test_trading_facts_v2_schema.py` |
| Unit-of-work tests | `tests/storage/test_trading_facts_uow.py` |
| Aggregate repository tests | `tests/storage/test_automation_facts_v2_repository.py`, `tests/storage/test_order_facts_v2_repository.py`, `tests/storage/test_position_ledger_v2_repository.py`, `tests/storage/test_trading_observability_v2_repository.py`, `tests/storage/test_trading_analytics_v2_repository.py` |
| PostgreSQL acceptance | `tests/integration/postgresql/test_trading_facts_v2.py` |
| Runtime isolation | `tests/integration/test_trading_facts_shadow_isolation.py` |

---

### Task 1: Typed trading-fact records, errors and ports

**Files:**

- Create: `src/moex_sentinel/domain/trading_facts.py`
- Create: `src/moex_sentinel/services/trading_fact_ports.py`
- Create: `tests/domain/test_trading_facts.py`

**Interfaces:**

- Consumes: `sentinel_contracts.base.LegacyPositionalModel`, `AutomationState`, `DecisionKind`, `OrderSide`, `StrategyValues`, `datetime`, `Decimal`, `Protocol`.
- Produces: immutable persistence drafts with stable upstream identities and timestamps, `TradingFactErrorCode`, `TradingFactPersistenceError`, five repository Protocols and `TradingFactsUnitOfWorkPort`.

- [x] **Step 1: Write failing tests for frozen contracts and safe errors**

Add tests that instantiate `TradingAutomationDraft`, `TradeDecisionDraft`,
`BrokerOrderDraft`, `TradeExecutionDraft`, `PositionCycleDraft`,
`PositionLotDraft`, `ExecutionLotAllocationDraft`, `TradeAuditEventDraft`,
`AutomationEnvelopeDraft`, `BrokerAccountFeeProfileDraft`,
`PortfolioSnapshotDraft` and `PositionValuationSnapshotDraft` with synthetic
values. Assert `extra="forbid"`, frozen mutation rejection and these safe error
semantics:

```python
def test_persistence_error_exposes_stable_code_without_payload() -> None:
    error = TradingFactPersistenceError(
        TradingFactErrorCode.FACT_ID_CONFLICT,
        entity_type="trade_execution",
        constraint_name="uq_trade_executions_v2_fact_id",
    )

    assert error.code is TradingFactErrorCode.FACT_ID_CONFLICT
    assert "synthetic-payload" not in str(error)
    assert error.entity_type == "trade_execution"
```

- [x] **Step 2: Run the domain test and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/domain/test_trading_facts.py
```

Expected: collection fails because `moex_sentinel.domain.trading_facts` does not
exist.

- [x] **Step 3: Implement the frozen domain record manifest**

Use this base and error surface:

```python
class FrozenFactModel(LegacyPositionalModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TradingFactErrorCode(StrEnum):
    NOT_FOUND = "TRADING_FACT_NOT_FOUND"
    CROSS_SCOPE = "CROSS_SCOPE_RELATION"
    FACT_ID_CONFLICT = "FACT_ID_CONFLICT"
    ORDER_IDEMPOTENCY_CONFLICT = "ORDER_IDEMPOTENCY_CONFLICT"
    SEQUENCE_CONFLICT = "AUTOMATION_SEQUENCE_CONFLICT"
    REVISION_CONFLICT = "AUTOMATION_REVISION_CONFLICT"
    INVALID_STATE = "INVALID_FACT_STATE"
```

Define the remaining value domains exactly:

| Enum | Values |
|---|---|
| `PositionCycleState` | `OPEN`, `CLOSED` |
| `OrderIntentKind` | `OPEN`, `BUY_MORE`, `SELL_PART`, `SELL_ALL` |
| `BrokerOrderType` | `MARKET`, `LIMIT` |
| `BrokerOrderStatus` | `CREATED`, `DISPATCH_PENDING`, `SUBMITTING`, `SUBMITTED`, `ACCEPTED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED`, `UNCERTAIN`, `FAILED` |
| `ExecutionSource` | `BROKER_FILL`, `LEGACY_AGGREGATE` |
| `TradeAuditLevel` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

Reuse `AutomationState`, `DecisionKind` and `OrderSide` from
`sentinel_contracts`; do not introduce duplicate enums for them. Keep
`AutomationEnvelopeDraft.fact_kind` as a non-blank string because the exhaustive
typed ingress union is introduced in `0.2.1.3`, not this milestone.

Define drafts with the exact canonical fields below. Their identities and event
timestamps are stable inputs supplied by Core or Worker, so repositories return
the same frozen draft type after persistence rather than generating a second
record shape:

| Draft | Required fields |
|---|---|
| `TradingAutomationDraft` | `id`, `user_broker_id`, `instrument_id`, `state`, `suspended_from_state`, `hold_reason`, `revision`, `last_sequence_number`, `resume_requested`, `created_at`, `updated_at`, `closed_at` |
| `AutomationStrategyDraft` | `id`, `user_broker_id`, `automation_id`, `revision`, `values: StrategyValues`, `created_at`, `updated_at` |
| `PositionCycleDraft` | `id`, `user_broker_id`, `automation_id`, `instrument_id`, `state`, `quantity_lots`, `average_entry_price`, `invested_amount`, `realized_pnl`, `unrealized_pnl`, `net_pnl`, `accumulated_commissions`, `opened_at`, `closed_at`, `created_at`, `updated_at` |
| `TradeDecisionDraft` | `id`, `fact_id`, `process_id`, `user_broker_id`, `automation_id`, `position_cycle_id`, `instrument_id`, `quantity_lots`, `lot_size`, `average_price`, `invested_amount`, `current_price`, `best_bid`, `best_ask`, `indicators`, `estimated_commission`, `decision`, `reason_code`, `requested_quantity_lots`, `limit_price`, `strategy_snapshot`, `decided_at`, `created_at` |
| `BrokerOrderDraft` | `id`, `fact_id`, `user_broker_id`, `automation_id`, `decision_id`, `position_cycle_id`, `instrument_id`, `idempotency_key`, `external_order_id`, `intent_kind`, `side`, `order_type`, `state`, `quantity_lots`, `limit_price`, `requested_amount`, `executed_amount`, `estimated_commission`, `executed_commission`, `strategy_snapshot`, `created_at`, `dispatch_started_at`, `broker_responded_at`, `executed_at`, `terminal_at`, `updated_at` |
| `BrokerOrderEventDraft` | `id`, `fact_id`, `user_broker_id`, `automation_id`, `broker_order_id`, `from_state`, `to_state`, `safe_reason`, `safe_message`, `occurred_at`, `created_at` |
| `TradeExecutionDraft` | `id`, `fact_id`, `user_broker_id`, `automation_id`, `broker_order_id`, `position_cycle_id`, `instrument_id`, `external_execution_id`, `side`, `executed_lots`, `price`, `value`, `broker_commission`, `other_fees`, `currency`, `source`, `is_aggregated`, `legacy_source_id`, `migrated_at`, `executed_at`, `created_at` |
| `PositionLotDraft` | `id`, `user_broker_id`, `automation_id`, `position_cycle_id`, `buy_execution_id`, `original_lots`, `remaining_lots`, `entry_price`, `entry_commission`, `opened_at`, `created_at`, `updated_at` |
| `ExecutionLotAllocationDraft` | `id`, `user_broker_id`, `automation_id`, `position_cycle_id`, `sell_execution_id`, `position_lot_id`, `allocated_lots`, `entry_value`, `exit_value`, `entry_commission`, `exit_commission`, `realized_pnl`, `allocated_at`, `created_at` |
| `TradeAuditEventDraft` | `event_id`, `process_id`, `parent_process_id`, `user_broker_id`, `automation_id`, `decision_id`, `broker_order_id`, `execution_id`, `instrument_id`, `level`, `stage`, `safe_message`, `data`, `occurred_at`, `created_at`, `critical` |
| `AutomationEnvelopeDraft` | `event_id`, `automation_id`, `user_broker_id`, `sequence_number`, `expected_revision`, `fact_kind`, `safe_message`, `payload`, `occurred_at`, `received_at` |
| `BrokerAccountFeeProfileDraft` | `id`, `user_broker_id`, `instrument_type`, `currency`, `buy_rate`, `sell_rate`, `service_rate`, `deal_rate`, `source`, `calculated_at`, `valid_until`, `created_at`, `updated_at` |
| `PortfolioSnapshotDraft` | `id`, `user_broker_id`, `total_value`, `free_cash`, `realized_pnl`, `unrealized_pnl`, `net_pnl`, `currency`, `captured_at`, `created_at` |
| `PositionValuationSnapshotDraft` | `id`, `user_broker_id`, `automation_id`, `position_cycle_id`, `instrument_id`, `quantity_lots`, `average_price`, `current_price`, `invested_amount`, `market_value`, `realized_pnl`, `unrealized_pnl`, `net_pnl`, `actual_commissions`, `source`, `captured_at`, `created_at` |

Use `dict[str, object]` for JSON fields, `Decimal` for money/rates/prices,
`datetime` for timestamps and `str | None` for optional identifiers.
`TradingFactPersistenceError` stores only code, entity type and optional
constraint name; its message never accepts a row or payload.

- [x] **Step 4: Define exact repository Protocol signatures**

In `services/trading_fact_ports.py`, expose session-bound ports with these
methods:

```python
class AutomationFactsPort(Protocol):
    def create(self, user_broker_id: str, automation: TradingAutomationDraft,
               strategy: AutomationStrategyDraft) -> TradingAutomationDraft: ...
    def get(self, user_broker_id: str, automation_id: str) -> TradingAutomationDraft: ...
    def get_strategy(self, user_broker_id: str, automation_id: str) -> AutomationStrategyDraft: ...
    def compare_and_set_state(self, user_broker_id: str, automation_id: str, *,
                              expected_revision: int, state: AutomationState,
                              hold_reason: str | None, closed_at: datetime | None) -> TradingAutomationDraft: ...
    def compare_and_set_strategy(self, user_broker_id: str, automation_id: str, *,
                                 expected_revision: int,
                                 strategy: AutomationStrategyDraft) -> AutomationStrategyDraft: ...


class OrderFactsPort(Protocol):
    def append_decision(self, user_broker_id: str, value: TradeDecisionDraft) -> TradeDecisionDraft: ...
    def append_order(self, user_broker_id: str, value: BrokerOrderDraft) -> BrokerOrderDraft: ...
    def append_order_event(self, user_broker_id: str, value: BrokerOrderEventDraft) -> BrokerOrderEventDraft: ...
    def append_execution(self, user_broker_id: str, value: TradeExecutionDraft) -> TradeExecutionDraft: ...
    def get_decision(self, user_broker_id: str, decision_id: str) -> TradeDecisionDraft: ...
    def get_order(self, user_broker_id: str, order_id: str) -> BrokerOrderDraft: ...
    def list_order_events(self, user_broker_id: str, order_id: str) -> tuple[BrokerOrderEventDraft, ...]: ...
    def list_executions(self, user_broker_id: str, order_id: str) -> tuple[TradeExecutionDraft, ...]: ...


class PositionLedgerPort(Protocol):
    def open_cycle(self, user_broker_id: str, value: PositionCycleDraft) -> PositionCycleDraft: ...
    def replace_cycle_aggregate(self, user_broker_id: str, value: PositionCycleDraft) -> PositionCycleDraft: ...
    def append_lot(self, user_broker_id: str, value: PositionLotDraft) -> PositionLotDraft: ...
    def append_allocation(self, user_broker_id: str,
                          value: ExecutionLotAllocationDraft) -> ExecutionLotAllocationDraft: ...
    def get_cycle(self, user_broker_id: str, cycle_id: str) -> PositionCycleDraft: ...
    def list_open_lots(self, user_broker_id: str, cycle_id: str) -> tuple[PositionLotDraft, ...]: ...
    def list_allocations(self, user_broker_id: str,
                         cycle_id: str) -> tuple[ExecutionLotAllocationDraft, ...]: ...


class TradingAuditPort(Protocol):
    def append_audit(self, user_broker_id: str, value: TradeAuditEventDraft) -> TradeAuditEventDraft: ...
    def append_envelope(self, user_broker_id: str,
                        value: AutomationEnvelopeDraft) -> AutomationEnvelopeDraft: ...


class TradingAnalyticsPort(Protocol):
    def upsert_fee_profile(self, user_broker_id: str,
                           value: BrokerAccountFeeProfileDraft) -> BrokerAccountFeeProfileDraft: ...
    def append_portfolio_snapshot(self, user_broker_id: str,
                                  value: PortfolioSnapshotDraft) -> PortfolioSnapshotDraft: ...
    def append_position_valuation(self, user_broker_id: str,
                                  value: PositionValuationSnapshotDraft) -> PositionValuationSnapshotDraft: ...
```

`TradingFactsUnitOfWorkPort` exposes the five ports as attributes and typed
`__enter__`/`__exit__` methods. Attribute names are `automations`, `orders`,
`positions`, `audit` and `analytics`. It exposes no public `commit()` method.

- [x] **Step 5: Run tests, strict typing and style**

Run:

```bash
uv run python -m pytest -q tests/domain/test_trading_facts.py
uv run mypy src/moex_sentinel/domain/trading_facts.py src/moex_sentinel/services/trading_fact_ports.py
uv run ruff check src/moex_sentinel/domain/trading_facts.py src/moex_sentinel/services/trading_fact_ports.py tests/domain/test_trading_facts.py
uv run black --check src/moex_sentinel/domain/trading_facts.py src/moex_sentinel/services/trading_fact_ports.py tests/domain/test_trading_facts.py
```

Expected: all commands pass. Review `git diff`; do not stage or commit.

---

### Task 2: Automation, strategy and position-cycle models

**Files:**

- Create: `src/moex_sentinel/storage/models/automation_facts_v2.py`
- Modify: `src/moex_sentinel/storage/models/__init__.py`
- Create: `tests/storage/trading_facts_v2_helpers.py`
- Create: `tests/storage/test_trading_facts_v2_models.py`

**Interfaces:**

- Consumes: `Base`, `TimestampMixin`, `UuidPrimaryKeyMixin`, `UTCDateTime`, shared money precision, `UserBrokerV2Model`, `BrokerInstrumentV2Model`.
- Produces: `TradingAutomationV2Model`, `AutomationStrategyV2Model`, `PositionCycleV2Model` and reusable synthetic parent builders.

- [x] **Step 1: Write failing scoped-model tests**

Create an in-memory SQLite session with foreign keys enabled and tests proving:

```python
def test_automation_cannot_reference_instrument_from_another_scope(session: Session) -> None:
    seed_two_scopes(session)
    session.add(automation_model("automation-1", "scope-2", "instrument-scope-1"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_only_one_non_closed_automation_exists_per_scoped_instrument(session: Session) -> None:
    seed_scope(session)
    session.add_all([
        automation_model("automation-1", "scope-1", "instrument-1", state="IN_WORK"),
        automation_model("automation-2", "scope-1", "instrument-1", state="HOLD"),
    ])
    with pytest.raises(IntegrityError):
        session.flush()


def test_only_one_open_cycle_exists_per_scoped_automation(session: Session) -> None:
    seed_automation(session)
    session.add_all([
        position_cycle_model("cycle-1", closed_at=None),
        position_cycle_model("cycle-2", closed_at=None),
    ])
    with pytest.raises(IntegrityError):
        session.flush()
```

Also prove one-to-one strategy ownership, positive revision, non-negative
sequence and quantity, and the `OPEN`/`CLOSED` timestamp consistency check.

- [x] **Step 2: Run model tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/storage/test_trading_facts_v2_models.py
```

Expected: import failure for `automation_facts_v2` models.

- [x] **Step 3: Implement the three ORM models and named constraints**

Use these physical rules:

```python
ACTIVE_AUTOMATION = sa.text("closed_at IS NULL")
OPEN_CYCLE = sa.text("closed_at IS NULL")

Index(
    "uq_trading_automations_v2_active_scope_instrument",
    "user_broker_id",
    "instrument_id",
    unique=True,
    sqlite_where=ACTIVE_AUTOMATION,
    postgresql_where=ACTIVE_AUTOMATION,
)
Index(
    "uq_position_cycles_v2_open_scope_automation",
    "user_broker_id",
    "automation_id",
    unique=True,
    sqlite_where=OPEN_CYCLE,
    postgresql_where=OPEN_CYCLE,
)
```

`TradingAutomationV2Model` uses a scoped instrument FK, unique
`(user_broker_id, id)`, lifecycle check, `revision > 0`,
`last_sequence_number >= 0` and `closed_at` as the active-row predicate.
`AutomationStrategyV2Model` carries `user_broker_id`, uses a scoped automation
FK, is unique by `(user_broker_id, automation_id)` and stores the typed strategy
columns plus its own positive revision. `PositionCycleV2Model` uses scoped
automation and instrument FKs and stores the complete aggregate fields from
Task 1.

- [x] **Step 4: Export models and run GREEN**

Import the three models in `storage/models/__init__.py`, add them to `__all__`,
then run:

```bash
uv run python -m pytest -q tests/storage/test_trading_facts_v2_models.py
uv run ruff check src/moex_sentinel/storage/models/automation_facts_v2.py tests/storage/test_trading_facts_v2_models.py tests/storage/trading_facts_v2_helpers.py
uv run black --check src/moex_sentinel/storage/models/automation_facts_v2.py tests/storage/test_trading_facts_v2_models.py tests/storage/trading_facts_v2_helpers.py
```

Expected: all tests and style checks pass. Review the diff; do not commit.

---

### Task 3: Decision, order and execution fact models

**Files:**

- Create: `src/moex_sentinel/storage/models/order_facts_v2.py`
- Modify: `src/moex_sentinel/storage/models/__init__.py`
- Modify: `tests/storage/test_trading_facts_v2_models.py`
- Modify: `tests/storage/trading_facts_v2_helpers.py`

**Interfaces:**

- Consumes: scoped parents from Task 2 and the Task 1 field manifest.
- Produces: `TradeDecisionV2Model`, `BrokerOrderV2Model`, `BrokerOrderEventV2Model`, `TradeExecutionV2Model`.

- [x] **Step 1: Add failing lineage and idempotency tests**

Add tests proving:

```python
def test_decision_creates_at_most_one_order(session: Session) -> None:
    seed_decision(session)
    session.add_all([order_model("order-1"), order_model("order-2")])
    with pytest.raises(IntegrityError):
        session.flush()


def test_external_execution_id_is_unique_only_when_present(session: Session) -> None:
    seed_order(session)
    session.add_all([
        execution_model("execution-1", external_execution_id=None, source="LEGACY_AGGREGATE"),
        execution_model("execution-2", external_execution_id=None, source="LEGACY_AGGREGATE"),
    ])
    session.flush()
```

Also prove scoped `fact_id`, order idempotency key and non-null external IDs are
unique; an order/event/execution cannot point across scope; `BROKER_FILL`
requires `external_execution_id`; quantity, price, value, commissions and fees
obey their checks; an order keeps immutable strategy JSON.

- [x] **Step 2: Run the focused model tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/storage/test_trading_facts_v2_models.py -k "decision or order or execution"
```

Expected: imports or assertions fail because the four models do not exist.

- [x] **Step 3: Implement fact models with scoped composite FKs**

Use unique constraints/indexes with these exact names and keys:

| Name | Keys/predicate |
|---|---|
| `uq_trade_decisions_v2_fact_id` | `fact_id` |
| `uq_trade_decisions_v2_scope_id` | `(user_broker_id, id)` |
| `uq_broker_orders_v2_fact_id` | `fact_id` |
| `uq_broker_orders_v2_scope_id` | `(user_broker_id, id)` |
| `uq_broker_orders_v2_scope_decision` | `(user_broker_id, decision_id)` |
| `uq_broker_orders_v2_scope_idempotency` | `(user_broker_id, idempotency_key)` |
| `uq_broker_orders_v2_scope_external_order` | `(user_broker_id, external_order_id) WHERE external_order_id IS NOT NULL` |
| `uq_broker_order_events_v2_fact_id` | `fact_id` |
| `uq_trade_executions_v2_fact_id` | `fact_id` |
| `uq_trade_executions_v2_scope_id` | `(user_broker_id, id)` |
| `uq_trade_executions_v2_scope_external_execution` | `(user_broker_id, external_execution_id) WHERE external_execution_id IS NOT NULL` |

All automation, cycle, decision, order, execution and instrument relationships
carry `user_broker_id`. Use restrictive deletes. Store `indicators` and strategy
snapshots as non-null JSON objects.

- [x] **Step 4: Export and verify the four models**

Run:

```bash
uv run python -m pytest -q tests/storage/test_trading_facts_v2_models.py
uv run ruff check src/moex_sentinel/storage/models/order_facts_v2.py tests/storage/test_trading_facts_v2_models.py
uv run black --check src/moex_sentinel/storage/models/order_facts_v2.py tests/storage/test_trading_facts_v2_models.py
```

Expected: all model tests pass. Review the diff; do not commit.

---

### Task 4: Inventory, audit and analytical models

**Files:**

- Create: `src/moex_sentinel/storage/models/position_facts_v2.py`
- Create: `src/moex_sentinel/storage/models/trading_observability_v2.py`
- Create: `src/moex_sentinel/storage/models/trading_analytics_v2.py`
- Modify: `src/moex_sentinel/storage/models/__init__.py`
- Modify: `tests/storage/test_trading_facts_v2_models.py`
- Modify: `tests/storage/trading_facts_v2_helpers.py`

**Interfaces:**

- Consumes: cycle, order and execution scoped parents from Tasks 2 and 3.
- Produces: the remaining seven shadow models and a complete `Base.metadata` graph.

- [x] **Step 1: Add failing inventory, ordering and analytical tests**

Add tests proving:

```python
def test_sell_execution_allocates_source_lot_once(session: Session) -> None:
    seed_buy_lot_and_sell_execution(session)
    session.add_all([
        allocation_model("allocation-1"),
        allocation_model("allocation-2"),
    ])
    with pytest.raises(IntegrityError):
        session.flush()


def test_automation_envelope_sequence_is_unique(session: Session) -> None:
    seed_automation(session)
    session.add_all([
        envelope_model("event-1", sequence_number=1),
        envelope_model("event-2", sequence_number=1),
    ])
    with pytest.raises(IntegrityError):
        session.flush()
```

Also prove remaining lots cannot exceed original lots, allocation scope cannot
cross a cycle or lot, audit links are scoped, audit `automation_id` is mandatory,
fee profiles are unique by scope/instrument type/currency, and snapshots accept
multiple capture times. BUY/SELL semantic validation belongs to the repository
tests in Task 8 because a portable foreign key cannot inspect a referenced row's
side.

- [x] **Step 2: Run focused tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/storage/test_trading_facts_v2_models.py -k "lot or allocation or envelope or audit or profile or snapshot"
```

Expected: imports or assertions fail for the remaining models.

- [x] **Step 3: Implement inventory and observability constraints**

Create `PositionLotV2Model`, `ExecutionLotAllocationV2Model`,
`TradeAuditEventV2Model` and `AutomationEventV2Model`. Use these exact unique
rules:

```text
position_lots_v2: (user_broker_id, id), (user_broker_id, buy_execution_id)
execution_lot_allocations_v2: (user_broker_id, id), (sell_execution_id, position_lot_id)
trade_audit_events_v2: event_id primary key
automation_events_v2: event_id primary key, (user_broker_id, automation_id, sequence_number)
```

Add named checks for positive original/allocated lots, non-negative remaining
lots, `remaining_lots <= original_lots`, positive entry/exit values, non-negative
commission attribution, positive sequence/revision and
`length(trim(fact_kind)) > 0`.

- [x] **Step 4: Implement analytical models and export all models**

Create `BrokerAccountFeeProfileV2Model`, `PortfolioSnapshotV2Model` and
`PositionValuationSnapshotV2Model`. Fee rates and free cash may be zero but not
negative; snapshot P&L values may be negative. Scope every row to
`user_brokers_v2`; position valuations additionally use scoped automation,
cycle and instrument FKs. Export all seven models from `storage/models/__init__.py`.

- [x] **Step 5: Run complete model GREEN and style checks**

Run:

```bash
uv run python -m pytest -q tests/storage/test_trading_facts_v2_models.py
uv run ruff check src/moex_sentinel/storage/models tests/storage/test_trading_facts_v2_models.py tests/storage/trading_facts_v2_helpers.py
uv run black --check src/moex_sentinel/storage/models tests/storage/test_trading_facts_v2_models.py tests/storage/trading_facts_v2_helpers.py
```

Expected: complete model suite passes. Review the diff; do not commit.

---

### Task 5: Atomic Alembic expansion and round-trip

**Files:**

- Create: `alembic/versions/a0f212000001_add_core_trading_fact_shadow_schema.py`
- Create: `tests/migrations/test_trading_facts_v2_schema.py`
- Modify: `tests/storage/test_schema_revision.py`

**Interfaces:**

- Consumes: complete ORM metadata from Tasks 2-4 and direct parent revision `a0f211000001`.
- Produces: Alembic head `a0f212000001` with atomic upgrade and isolated downgrade.

- [x] **Step 1: Write failing Alembic round-trip tests**

Use a temporary SQLite file and `alembic.command` to prove:

```python
command.upgrade(config, "a0f211000001")
seed_legacy_and_reference_rows(engine)
command.upgrade(config, "a0f212000001")
assert set(TRADING_FACT_V2_TABLES) <= set(inspect(engine).get_table_names())

command.downgrade(config, "a0f211000001")
assert set(TRADING_FACT_V2_TABLES).isdisjoint(inspect(engine).get_table_names())
assert legacy_and_reference_rows_are_preserved(engine)

command.upgrade(config, "head")
assert current_schema_revision(engine) == "a0f212000001"
```

Add a second test upgrading an empty database directly to head. Update the
schema-revision expectation to `a0f212000001`.

- [x] **Step 2: Run migration tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/migrations/test_trading_facts_v2_schema.py tests/storage/test_schema_revision.py
```

Expected: the requested revision cannot be resolved or the expected head differs.

- [x] **Step 3: Write the single migration in dependency order**

Set:

```python
revision: str = "a0f212000001"
down_revision: str | None = "a0f211000001"
```

Upgrade order is automation, strategy, cycle, decision, order, order event,
execution, lot, allocation, audit, envelope, fee profile, portfolio snapshot,
position valuation snapshot. Create indexes immediately after their owning
table. Downgrade drops only these indexes and tables in exact reverse dependency
order. Use the same names, lengths, nullability, checks and predicates as ORM
metadata; do not use autogenerate output without manual comparison.

- [x] **Step 4: Verify migration and metadata parity**

Run:

```bash
uv run python -m pytest -q tests/migrations/test_trading_facts_v2_schema.py tests/storage/test_schema_revision.py tests/storage/test_trading_facts_v2_models.py
uv run python -m pytest -q
uv run mypy
```

Expected: targeted tests, complete Python regression and strict mypy pass.
Review the diff; do not commit.

---

### Task 6: Shared repository idempotency and safe constraint translation

**Files:**

- Create: `src/moex_sentinel/storage/repositories/trading_facts_support.py`
- Create: `tests/storage/test_trading_facts_support.py`

**Interfaces:**

- Consumes: Task 1 errors, SQLAlchemy `Session`, immutable shadow models and converter callables.
- Produces: `append_idempotent`, `flush_or_translate` and the named-constraint error map used by Tasks 7-9.

- [x] **Step 1: Write failing exact-retry and safe-error tests**

Use `TradeDecisionV2Model` with a local converter and prove both branches:

```python
def test_append_idempotent_returns_existing_equal_value(session: Session) -> None:
    value = decision_draft()
    first = append_idempotent(session, candidate=decision_model(value),
                              identity=TradeDecisionV2Model.fact_id == value.fact_id,
                              to_value=decision_value, conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
                              entity_type="trade_decision")
    second = append_idempotent(session, candidate=decision_model(value),
                               identity=TradeDecisionV2Model.fact_id == value.fact_id,
                               to_value=decision_value, conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
                               entity_type="trade_decision")

    assert second == first


def test_constraint_translation_never_exposes_payload(session: Session) -> None:
    session.add(conflicting_order_model(safe_payload="synthetic-payload"))
    with pytest.raises(TradingFactPersistenceError) as caught:
        flush_or_translate(session, entity_type="broker_order")

    assert caught.value.code is TradingFactErrorCode.ORDER_IDEMPOTENCY_CONFLICT
    assert "synthetic-payload" not in str(caught.value)
```

- [x] **Step 2: Run support tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/storage/test_trading_facts_support.py
```

Expected: import failure for `trading_facts_support`.

- [x] **Step 3: Implement shared idempotency and error translation**

Use this generic interface:

```python
def append_idempotent(
    session: Session,
    *,
    candidate: ModelT,
    identity: ColumnElement[bool],
    to_value: Callable[[ModelT], ValueT],
    conflict_code: TradingFactErrorCode,
    entity_type: str,
) -> ValueT: ...
```

The function selects by identity; if absent, it adds, safely flushes and returns
the converted candidate. If present, it converts both rows and compares frozen
values; equality returns the existing value, while inequality raises
`TradingFactPersistenceError(conflict_code, entity_type=...)`.

- [x] **Step 4: Implement named-constraint translation**

Map named constraints to error codes in a constant dictionary. Never include
bound parameters, row values or raw driver text in the public error message.
`flush_or_translate(session, *, entity_type)` calls `session.flush()`. For
PostgreSQL it reads `orig.diag.constraint_name`; for SQLite it maps only known
sanitized `table.column` unique signatures and named check-constraint results.
Foreign-key scope and semantic checks are validated before flush because SQLite
does not identify the failed FK. Unknown `IntegrityError` is re-raised with its
original cause. Any translated persistence error aborts the current unit of work;
the caller exits the context so the unit of work performs the rollback before a
retry.

- [x] **Step 5: Run GREEN, typing and style checks**

Run:

```bash
uv run python -m pytest -q tests/storage/test_trading_facts_support.py
uv run mypy src/moex_sentinel/storage/repositories/trading_facts_support.py
uv run ruff check src/moex_sentinel/storage/repositories/trading_facts_support.py tests/storage/test_trading_facts_support.py
uv run black --check src/moex_sentinel/storage/repositories/trading_facts_support.py tests/storage/test_trading_facts_support.py
```

Expected: all commands pass. Review the diff; do not commit.

---

### Task 7: Automation and order fact repositories

**Files:**

- Create: `src/moex_sentinel/storage/repositories/automation_facts_v2.py`
- Create: `src/moex_sentinel/storage/repositories/order_facts_v2.py`
- Create: `tests/storage/test_automation_facts_v2_repository.py`
- Create: `tests/storage/test_order_facts_v2_repository.py`

**Interfaces:**

- Consumes: `AutomationFactsPort`, `OrderFactsPort`, Task 6 support functions and a live SQLAlchemy `Session`.
- Produces: working automation/strategy compare-and-set and immutable decision/order/event/execution persistence.

- [x] **Step 1: Write failing automation repository tests**

Prove create-with-strategy is atomic, every read is scope-filtered, strategy and
state updates require the expected revision, zero matched rows raises
`AUTOMATION_REVISION_CONFLICT`, and two active automations for one instrument
translate to `INVALID_FACT_STATE` without leaking driver text.

Use this concurrency assertion:

```python
with pytest.raises(TradingFactPersistenceError) as caught:
    repository.compare_and_set_state(
        "scope-1", "automation-1", expected_revision=7,
        state=AutomationState.HOLD, hold_reason="synthetic hold", closed_at=None,
    )
assert caught.value.code is TradingFactErrorCode.REVISION_CONFLICT
```

- [x] **Step 2: Write failing order repository tests**

Prove exact append retry returns the existing detached record, conflicting
`fact_id` reuse raises `FACT_ID_CONFLICT`, conflicting idempotency-key reuse
raises `ORDER_IDEMPOTENCY_CONFLICT`, cross-scope order lineage raises
`CROSS_SCOPE_RELATION`, and ordered readers sort events/executions by
`occurred_at`/`executed_at` then `id`.

- [x] **Step 3: Run both repository suites and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/storage/test_automation_facts_v2_repository.py tests/storage/test_order_facts_v2_repository.py
```

Expected: import failures for both adapters.

- [x] **Step 4: Implement minimal scoped adapters**

Use SQLAlchemy `select` with both `user_broker_id` and entity ID for every read.
Use `update(...).where(revision == expected_revision).values(revision=revision + 1, ...)`
for compare-and-set. Append methods call the Task 6 idempotency helper and flush
only. Convert JSON mappings with `dict(...)` before returning frozen records so
callers cannot mutate ORM state.

- [x] **Step 5: Run GREEN and quality checks**

Run:

```bash
uv run python -m pytest -q tests/storage/test_automation_facts_v2_repository.py tests/storage/test_order_facts_v2_repository.py tests/storage/test_trading_facts_support.py
uv run mypy src/moex_sentinel/storage/repositories/automation_facts_v2.py src/moex_sentinel/storage/repositories/order_facts_v2.py
uv run ruff check src/moex_sentinel/storage/repositories/automation_facts_v2.py src/moex_sentinel/storage/repositories/order_facts_v2.py tests/storage/test_automation_facts_v2_repository.py tests/storage/test_order_facts_v2_repository.py
uv run black --check src/moex_sentinel/storage/repositories/automation_facts_v2.py src/moex_sentinel/storage/repositories/order_facts_v2.py tests/storage/test_automation_facts_v2_repository.py tests/storage/test_order_facts_v2_repository.py
```

Expected: all commands pass. Review the diff; do not commit.

---

### Task 8: Position ledger repository

**Files:**

- Create: `src/moex_sentinel/storage/repositories/position_ledger_v2.py`
- Create: `tests/storage/test_position_ledger_v2_repository.py`

**Interfaces:**

- Consumes: `PositionLedgerPort`, scoped cycle/lot/allocation models and Task 6 error translation.
- Produces: deterministic cycle aggregate persistence and immutable LIFO attribution rows.

- [x] **Step 1: Write failing position-ledger tests**

Prove:

```python
def test_allocation_retry_does_not_apply_realized_pnl_twice(repository: PositionLedgerShadowRepository) -> None:
    first = repository.append_allocation("scope-1", allocation_draft())
    second = repository.append_allocation("scope-1", allocation_draft())
    cycle = repository.get_cycle("scope-1", "cycle-1")

    assert second == first
    assert cycle.realized_pnl == EXPECTED_REALIZED_PNL
```

Also test one-open-cycle enforcement, exact cycle replacement, cross-scope lot
rejection, BUY-only source lot, SELL-only allocation source, remaining-lot
ordering and deterministic readers sorted by `opened_at`/`allocated_at` then ID.

- [x] **Step 2: Run the repository test and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/storage/test_position_ledger_v2_repository.py
```

Expected: import failure for `PositionLedgerShadowRepository`.

- [x] **Step 3: Implement scoped cycle, lot and allocation operations**

`open_cycle` and immutable appends use the Task 6 idempotency/error boundary.
`replace_cycle_aggregate` updates only rebuildable aggregate columns and
timestamps after verifying scope, automation and instrument identity are
unchanged. It never edits execution, lot or allocation history. `list_open_lots`
filters `remaining_lots > 0` and orders newest first for LIFO:

```python
select(PositionLotV2Model).where(
    PositionLotV2Model.user_broker_id == user_broker_id,
    PositionLotV2Model.position_cycle_id == cycle_id,
    PositionLotV2Model.remaining_lots > 0,
).order_by(PositionLotV2Model.opened_at.desc(), PositionLotV2Model.id.desc())
```

- [x] **Step 4: Run GREEN, typing and style checks**

Run:

```bash
uv run python -m pytest -q tests/storage/test_position_ledger_v2_repository.py tests/storage/test_trading_facts_support.py
uv run mypy src/moex_sentinel/storage/repositories/position_ledger_v2.py
uv run ruff check src/moex_sentinel/storage/repositories/position_ledger_v2.py tests/storage/test_position_ledger_v2_repository.py
uv run black --check src/moex_sentinel/storage/repositories/position_ledger_v2.py tests/storage/test_position_ledger_v2_repository.py
```

Expected: all commands pass. Review the diff; do not commit.

---

### Task 9: Audit, accepted-envelope and analytical repositories

**Files:**

- Create: `src/moex_sentinel/storage/repositories/trading_audit_v2.py`
- Create: `src/moex_sentinel/storage/repositories/trading_analytics_v2.py`
- Create: `src/moex_sentinel/storage/repositories/trading_facts_uow.py`
- Create: `tests/storage/test_trading_observability_v2_repository.py`
- Create: `tests/storage/test_trading_analytics_v2_repository.py`
- Create: `tests/storage/test_trading_facts_uow.py`

**Interfaces:**

- Consumes: `TradingAuditPort`, `TradingAnalyticsPort`, Task 6 idempotency and scoped models.
- Produces: immutable diagnostic/envelope append, scoped fee/snapshot storage and the complete atomic `TradingFactsShadowUnitOfWork`.

- [x] **Step 1: Write failing audit and envelope tests**

Test exact event retry, conflicting event reuse, repeated sequence with a new
event ID, optional audit links, mandatory automation link and cross-scope
rejection. Assert that exceptions do not contain the supplied `safe_message` or
JSON payload.

- [x] **Step 2: Write failing analytical repository tests**

Test fee-profile upsert by `(user_broker_id, instrument_type, currency)`, scope
isolation, multiple time-ordered portfolio snapshots, position valuation links
and preservation of negative P&L. Upsert may change rates/source/validity but
must not change the profile ID or scope key.

- [x] **Step 3: Run both suites and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/storage/test_trading_observability_v2_repository.py tests/storage/test_trading_analytics_v2_repository.py
```

Expected: import failures for both adapters.

- [x] **Step 4: Implement the two session-bound adapters**

Audit/envelope appends use stable identity comparison and flush only. Analytical
snapshot appends are immutable; fee profile upsert selects the scoped natural
key and assigns only mutable rate/source/time fields. All returned JSON mappings
are detached copies. Link validation always includes `user_broker_id`.

- [x] **Step 5: Write failing complete-unit-of-work tests**

Test one cross-repository success and one rollback:

```python
def test_unit_of_work_commits_all_repository_groups(factory: sessionmaker[Session]) -> None:
    with TradingFactsShadowUnitOfWork(factory) as uow:
        uow.automations.create("scope-1", automation_draft(), strategy_draft())
        uow.audit.append_envelope("scope-1", envelope_draft())

    assert persisted_counts(factory) == {"automations": 1, "strategies": 1, "envelopes": 1}


def test_unit_of_work_rolls_back_every_group(factory: sessionmaker[Session]) -> None:
    with pytest.raises(RuntimeError):
        with TradingFactsShadowUnitOfWork(factory) as uow:
            uow.automations.create("scope-1", automation_draft(), strategy_draft())
            raise RuntimeError("synthetic failure")

    assert persisted_counts(factory)["automations"] == 0
```

Do not create `trading_facts_uow.py` in this step.

- [x] **Step 6: Run the unit-of-work tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/storage/test_trading_facts_uow.py
```

Expected: import failure for `TradingFactsShadowUnitOfWork`.

- [x] **Step 7: Implement the complete unit of work**

Create one factory-owned session and instantiate all five repositories with that
`Session`. Commit only when `exc_type is None`; otherwise roll back; always
close. Use `type[BaseException] | None`, `BaseException | None` and
`TracebackType | None` for the `__exit__` signature. Repository constructors
accept `Session`, not a session factory, and the session stays private.

- [x] **Step 8: Run GREEN and quality checks**

Run:

```bash
uv run python -m pytest -q tests/storage/test_trading_observability_v2_repository.py tests/storage/test_trading_analytics_v2_repository.py tests/storage/test_trading_facts_uow.py
uv run mypy src/moex_sentinel/storage/repositories/trading_audit_v2.py src/moex_sentinel/storage/repositories/trading_analytics_v2.py src/moex_sentinel/storage/repositories/trading_facts_uow.py
uv run ruff check src/moex_sentinel/storage/repositories/trading_audit_v2.py src/moex_sentinel/storage/repositories/trading_analytics_v2.py src/moex_sentinel/storage/repositories/trading_facts_uow.py tests/storage/test_trading_observability_v2_repository.py tests/storage/test_trading_analytics_v2_repository.py tests/storage/test_trading_facts_uow.py
uv run black --check src/moex_sentinel/storage/repositories/trading_audit_v2.py src/moex_sentinel/storage/repositories/trading_analytics_v2.py src/moex_sentinel/storage/repositories/trading_facts_uow.py tests/storage/test_trading_observability_v2_repository.py tests/storage/test_trading_analytics_v2_repository.py tests/storage/test_trading_facts_uow.py
```

Expected: all commands pass. Review the diff; do not commit.

---

### Task 10: PostgreSQL acceptance, runtime isolation and milestone evidence

**Files:**

- Create: `tests/integration/postgresql/test_trading_facts_v2.py`
- Create: `tests/integration/test_trading_facts_shadow_isolation.py`
- Modify: `docs/phase-0-trading-service-refactor.md`
- Modify: `docs/phase-1-implementation-plan.md`
- Modify: `AGENT_BRIEF.md`
- Modify: `docs/superpowers/specs/2026-08-12-core-trading-facts-shadow-schema-design.md`
- Modify: `docs/superpowers/plans/2026-08-12-core-trading-facts-shadow-schema.md`

**Interfaces:**

- Consumes: Alembic head and all adapters from Tasks 1-9.
- Produces: cross-dialect acceptance evidence and the handoff boundary to `0.2.1.3`.

- [x] **Step 1: Write failing PostgreSQL-specific acceptance tests**

Use `isolated_postgresql_database_url` and prove the actual migrated schema:

```python
@pytest.mark.postgresql
def test_postgresql_rejects_cross_scope_lineage_and_duplicate_active_rows(
    isolated_postgresql_database_url: URL,
) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    seed_two_scopes(factory)

    with pytest.raises(TradingFactPersistenceError):
        persist_cross_scope_order(factory)
    with pytest.raises(TradingFactPersistenceError):
        persist_second_active_automation(factory)
```

Add tests for partial external-ID uniqueness, numeric precision, UTC round-trip,
JSON round-trip, unit-of-work rollback and exact idempotent append retry.

- [x] **Step 2: Write runtime-isolation tests**

Walk imports or monkeypatch constructors to prove current API composition,
legacy services and Worker composition never instantiate
`TradingFactsShadowUnitOfWork` or import the new concrete adapters. Assert
existing API health and repository tests continue to use legacy paths.

- [x] **Step 3: Run local targeted and complete quality gates**

Run:

```bash
uv run ruff check .
uv run black --check .
uv run mypy
uv run python -m pytest -q
```

Expected: all lint, formatting, strict typing and Python tests pass.

- [x] **Step 4: Run PostgreSQL and frontend acceptance**

Run the repository's migration-container PostgreSQL harness:

```bash
POSTGRES_USER=moex_sentinel_user docker compose --profile migrations run -T --rm --user root -v "$PWD/tests:/app/tests:ro" migrations sh -eu -c 'python -m pip install --no-cache-dir "pytest>=8.4,<9" >/dev/null && POSTGRES_TEST_DATABASE_URL="$DATABASE_URL" pytest -q -m postgresql tests/integration/postgresql'
```

Then run:

```bash
npm test
npm run build
```

from `frontend/`. Expected: PostgreSQL integration, frontend tests and
production build pass.

- [x] **Step 5: Record evidence and advance the phase pointer**

Mark `0.2.1.2` complete only after every gate passes. Record exact test counts,
new Alembic head, PostgreSQL acceptance and the fact that legacy read/write paths
were unchanged. Update the design review state to implemented and verified;
check completed task boxes in this plan. Set `0.2.1.3` as the next unimplemented
stage. Do not claim data migration occurred: trading-history transfer remains
`0.2.1.4`.

- [x] **Step 6: Final diff and no-commit checkpoint**

Run:

```bash
git diff --check
git status --short
```

Review every changed path against the plan, preserve unrelated user changes and
leave the worktree uncommitted.
