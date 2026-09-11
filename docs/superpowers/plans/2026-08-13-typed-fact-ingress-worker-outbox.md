# Typed Fact Ingress and Worker Outbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build milestone `0.2.1.3`: a strictly typed, idempotent Worker-to-Core `v2` fact ingress with one durable mixed-fact Worker outbox, while production remains explicitly on legacy ingress until the coordinated `0.2.1.5` cutover.

**Architecture:** Shared Pydantic contracts define nine immutable fact envelopes and safe per-automation results. A `v2` command claim idempotently projects only the claimed authoritative automation into the existing Core shadow schema; Worker then records typed facts in SQLite in the same transaction as the recovery mutation, and Core applies each automation group in one `TradingFactsShadowUnitOfWork`. Legacy and `v2` are mutually exclusive configuration selections: no dual publish and no automatic fallback.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, HTTPX, SQLAlchemy 2, SQLite, PostgreSQL 16, pytest, mypy strict, Ruff, Black, uv, Docker Compose.

## Global Constraints

- Follow `docs/superpowers/specs/2026-08-13-typed-fact-ingress-worker-outbox-design.md` and the canonical target fields in `docs/superpowers/specs/2026-08-10-trading-data-schema-migration-design.md`.
- Do not change the Alembic head `a0f212000001`; the Core target schema already exists.
- Do not migrate legacy history; that remains milestone `0.2.1.4`.
- Do not switch production reads or production ingress; that remains milestone `0.2.1.5`.
- Keep `FACT_INGRESS_VERSION=legacy` explicit in production Compose during this milestone; use `v2` only in isolated acceptance.
- Do not dual-publish a runtime fact and do not automatically fall back between ingress versions.
- Worker may import `sentinel_contracts.trading_facts` but must not import Core domain, ORM or repository modules.
- One `fact_outbox_v2` row is one immutable fact; one unified batch may mix fact kinds and automations.
- Every newly generated timestamp is UTC with millisecond precision; the HTTP contract rejects non-UTC or sub-millisecond values.
- Use UUID strings plus `(occurred_at, event_id)` for global ordering and `(sequence_number, event_id)` within one automation; do not add a global integer event ID.
- Process one automation group per Core transaction; failure of one group must not roll back successful peers.
- Preserve exact retries and reject identity reuse with different normalized content.
- A state-changing fact increments automation revision; an immutable supporting fact advances sequence without changing revision.
- Acknowledge only an accepted continuous prefix for the corresponding automation.
- Retain non-terminal Worker recovery records after outbox delivery.
- Use only synthetic local test values.
- Do not weaken Ruff, Black, strict mypy, backend, frontend, architecture or PostgreSQL gates.
- Do not create Git commits.

## File Map

| Responsibility | File |
|---|---|
| Shared millisecond UTC helper | `src/sentinel_contracts/time.py` |
| Core timestamp persistence normalization | `src/moex_sentinel/storage/types.py` |
| Nine payloads/envelopes, `v2` command and result DTOs | `src/sentinel_contracts/trading_facts.py` |
| Shared contract tests | `tests/contracts/test_trading_facts_contract.py` |
| Core aggregate sequence/revision primitives | `src/moex_sentinel/storage/repositories/automation_facts_v2.py` |
| Accepted-envelope lookup primitives | `src/moex_sentinel/storage/repositories/trading_audit_v2.py` |
| Updated persistence ports | `src/moex_sentinel/services/trading_fact_ports.py` |
| `v2` command projection from authoritative legacy rows | `src/moex_sentinel/storage/repositories/fact_command_projection.py` |
| Projection tests | `tests/storage/test_fact_command_projection.py` |
| Typed payload-to-draft mapping | `src/moex_sentinel/services/trading_fact_mapping.py` |
| Per-automation transactional ingress orchestration | `src/moex_sentinel/services/trading_fact_ingress.py` |
| Core ingress unit tests | `tests/services/test_trading_fact_ingress.py` |
| Core application usecases | `src/moex_sentinel/usecases/trading_fact_ingress.py` |
| Internal `v2` HTTP schemas and routes | `src/moex_sentinel/views/schemas/trading_facts.py`, `src/moex_sentinel/views/internal_trading_facts.py` |
| Core dependency composition | `src/moex_sentinel/composition.py`, `src/moex_sentinel/api/app.py` |
| Core HTTP contract tests | `tests/api/test_trading_fact_ingress_contract.py` |
| Worker `fact_outbox_v2` and added recovery identities | `src/trading_automaton/storage/models.py` |
| Additive Worker SQLite upgrade | `src/trading_automaton/storage/database.py` |
| Worker storage DTOs | `src/trading_automaton/domain/storage_dtos.py` |
| Worker envelope construction and sequence allocation | `src/trading_automaton/storage/fact_outbox_v2.py` |
| Worker transactional persistence integration | `src/trading_automaton/storage/repository.py` |
| Worker model/migration/repository tests | `tests/trading_automaton/storage/test_fact_outbox_v2.py`, `tests/trading_automaton/storage/test_worker_database.py`, `tests/trading_automaton/storage/test_local_repository.py` |
| Worker `v2` Core HTTP adapter | `src/trading_automaton/adapters/core_client.py` |
| Worker batch/retry/ack service | `src/trading_automaton/services/fact_synchronization.py` |
| Worker publisher tests | `tests/trading_automaton/adapters/test_core_client.py`, `tests/trading_automaton/services/test_fact_synchronization.py` |
| Exclusive ingress configuration and composition | `src/trading_automaton/config.py`, `src/trading_automaton/composition.py`, `compose.yml`, `.env.example` |
| Runtime coordinator batching behavior | `src/trading_automaton/services/streaming_runtime_coordinator.py` |
| End-to-end and boundary acceptance | `tests/integration/postgresql/test_trading_fact_ingress.py`, `tests/integration/test_worker_storage_isolation.py` |
| Milestone and rollout documentation | `AGENT_BRIEF.md`, `README.md`, `docs/development.md`, `docs/phase-0-trading-service-refactor.md`, `docs/phase-1-implementation-plan.md`, `tests/test_documentation.py` |

---

### Task 1: Strict shared fact contract and millisecond UTC invariant

**Files:**

- Create: `src/sentinel_contracts/time.py`
- Create: `src/sentinel_contracts/trading_facts.py`
- Create: `tests/contracts/test_trading_facts_contract.py`
- Modify: `src/moex_sentinel/storage/types.py`
- Modify: `src/moex_sentinel/storage/models/base.py`
- Modify: `src/trading_automaton/storage/models.py`

**Interfaces:**

- Consumes: `LegacyPositionalModel`, `AutomationState`, `DecisionKind`, `StrategyValues`, `OrderSide`, `datetime`, `Decimal`, `UUID`, Pydantic discriminated unions.
- Produces: `utc_now_ms() -> datetime`, `require_utc_millisecond(datetime) -> datetime`, `FactEnvelope`, `FactBatchRequest`, `FactBatchResult`, `FactAutomationCommand` and nine exact payload types.

- [ ] **Step 1: Write failing timestamp and contract tests**

Add tests proving UTC/millisecond rejection, frozen values, UUID validation,
extra-field rejection, discriminator/payload matching and serialization with an
explicit `Z` offset:

```python
def test_fact_envelope_requires_utc_millisecond_timestamp() -> None:
    values = state_envelope_values()
    values["occurred_at"] = datetime(2026, 8, 13, 10, 0, 0, 123001, tzinfo=UTC)

    with pytest.raises(ValidationError):
        FactBatchRequest(facts=[values])


def test_discriminator_rejects_mismatched_payload() -> None:
    values = state_envelope_values()
    values["fact_kind"] = FactKind.TRADE_DECISION_RECORDED

    with pytest.raises(ValidationError):
        FactBatchRequest(facts=[values])
```

- [ ] **Step 2: Run the contract test and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/contracts/test_trading_facts_contract.py
```

Expected: collection fails because `sentinel_contracts.trading_facts` and
`sentinel_contracts.time` do not exist.

- [ ] **Step 3: Implement the shared clock invariant**

Use one helper for generated timestamps and one strict validator for transport:

```python
def utc_now_ms() -> datetime:
    value = datetime.now(UTC)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def require_utc_millisecond(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must use an explicit UTC offset.")
    if value.microsecond % 1000:
        raise ValueError("Timestamp precision must not exceed milliseconds.")
    return value.astimezone(UTC)
```

Change both Core and Worker timestamp mixins to use `utc_now_ms`. Change both
Core and Worker `UTCDateTime.process_bind_param` implementations to normalize
timezone-aware values to UTC and floor microseconds to milliseconds before
writing. This governs every new DB row without rewriting historical rows. Add
round-trip tests for a `123001` microsecond input persisting as `123000`.

- [ ] **Step 4: Define the exact payload manifest**

Use `StrictFrozenModel` with `ConfigDict(frozen=True, extra="forbid",
hide_input_in_errors=True)` and `MillisecondUtc = Annotated[datetime,
AfterValidator(require_utc_millisecond)]`. Define these payload fields exactly:

| Payload | Fields beyond `fact_kind` |
|---|---|
| `AutomationStateChangedPayload` | `state`, `suspended_from_state`, `hold_reason`, `closed_at` |
| `TradeDecisionRecordedPayload` | `decision_id`, `process_id`, `position_cycle_id`, `instrument_id`, `quantity_lots`, `lot_size`, `average_price`, `invested_amount`, `current_price`, `best_bid`, `best_ask`, `indicators`, `estimated_commission`, `decision`, `reason_code`, `requested_quantity_lots`, `limit_price`, `strategy_snapshot`, `decided_at`, `created_at` |
| `BrokerOrderRecordedPayload` | `order_id`, `decision_id`, `position_cycle_id`, `instrument_id`, `idempotency_key`, `external_order_id`, `intent_kind`, `side`, `order_type`, `state`, `quantity_lots`, `limit_price`, `requested_amount`, `executed_amount`, `estimated_commission`, `executed_commission`, `strategy_snapshot`, `created_at`, `dispatch_started_at`, `broker_responded_at`, `executed_at`, `terminal_at`, `updated_at` |
| `BrokerOrderStateChangedPayload` | `order_event_id`, `broker_order_id`, `decision_id`, `position_cycle_id`, `instrument_id`, `idempotency_key`, `external_order_id`, `intent_kind`, `side`, `order_type`, `state`, `quantity_lots`, `limit_price`, `requested_amount`, `executed_amount`, `estimated_commission`, `executed_commission`, `strategy_snapshot`, `dispatch_started_at`, `broker_responded_at`, `executed_at`, `terminal_at`, `updated_at`, `from_state`, `to_state`, `safe_reason`, `safe_message`, `occurred_at`, `created_at` |
| `TradeExecutionRecordedPayload` | `execution_id`, `broker_order_id`, `position_cycle_id`, `instrument_id`, `external_execution_id`, `side`, `executed_lots`, `price`, `value`, `broker_commission`, `other_fees`, `currency`, `source`, `is_aggregated`, `legacy_source_id`, `migrated_at`, `executed_at`, `created_at` |
| `PositionCycleUpdatedPayload` | `position_cycle_id`, `instrument_id`, `state`, `quantity_lots`, `average_entry_price`, `invested_amount`, `realized_pnl`, `unrealized_pnl`, `net_pnl`, `accumulated_commissions`, `opened_at`, `closed_at`, `created_at`, `updated_at` |
| `PositionLotOpenedPayload` | `position_lot_id`, `position_cycle_id`, `buy_execution_id`, `original_lots`, `remaining_lots`, `entry_price`, `entry_commission`, `opened_at`, `created_at`, `updated_at` |
| `ExecutionLotAllocatedPayload` | `allocation_id`, `position_cycle_id`, `sell_execution_id`, `position_lot_id`, `allocated_lots`, `remaining_lots_after`, `entry_value`, `exit_value`, `entry_commission`, `exit_commission`, `realized_pnl`, `allocated_at`, `created_at` |
| `TradeAuditRecordedPayload` | `audit_event_id`, `process_id`, `parent_process_id`, `decision_id`, `broker_order_id`, `execution_id`, `instrument_id`, `level`, `stage`, `safe_message`, `data`, `occurred_at`, `created_at`, `critical` |

Define nine envelope subclasses so the outer `fact_kind` is the Pydantic
discriminator and the payload type cannot disagree:

```python
class AutomationStateChangedEnvelope(FactEnvelopeBase):
    fact_kind: Literal[FactKind.AUTOMATION_STATE_CHANGED]
    payload: AutomationStateChangedPayload


FactEnvelope = Annotated[
    AutomationStateChangedEnvelope
    | TradeDecisionRecordedEnvelope
    | BrokerOrderRecordedEnvelope
    | BrokerOrderStateChangedEnvelope
    | TradeExecutionRecordedEnvelope
    | PositionCycleUpdatedEnvelope
    | PositionLotOpenedEnvelope
    | ExecutionLotAllocatedEnvelope
    | TradeAuditRecordedEnvelope,
    Field(discriminator="fact_kind"),
]
```

Also define `FactPayload` as the union of the same nine payload classes. Every
payload timestamp field uses `MillisecondUtc`; producers must floor broker or
clock timestamps before constructing a payload rather than relying on JSON
serialization to lose precision.

`FactEnvelopeBase` contains `event_id: UUID`, `user_broker_id: UUID`,
`automation_id: UUID`, positive `sequence_number`, positive
`expected_revision`, `safe_message: str` with `max_length=1000`, and
`occurred_at: MillisecondUtc`.

- [ ] **Step 5: Define commands and safe results**

Use these exact result surfaces:

```python
class FactAutomationCommand(StrictFrozenModel):
    automation_id: UUID
    user_broker_id: UUID
    broker_id: UUID
    account_id: str
    external_instrument_id: str
    fact_instrument_id: UUID
    lot_size: int
    min_price_increment: Decimal
    state: AutomationState
    revision: int
    last_sequence_number: int
    resume_requested: bool
    strategy: StrategyValues


class FactAutomationStatus(StrictFrozenModel):
    automation_id: UUID
    user_broker_id: UUID
    state: AutomationState
    revision: int
    last_sequence_number: int
    resume_requested: bool
    strategy: StrategyValues


class FactAutomationStatusesResult(StrictFrozenModel):
    automations: tuple[FactAutomationStatus, ...]
    missing_automation_ids: tuple[UUID, ...] = ()


class FactGroupAcknowledgement(StrictFrozenModel):
    automation_id: UUID
    accepted_through_sequence: int
    current_revision: int
    accepted_event_ids: tuple[UUID, ...]


class FactGroupFailure(StrictFrozenModel):
    automation_id: UUID
    code: FactIngressErrorCode
    event_ids: tuple[UUID, ...]
    sequence_numbers: tuple[int, ...]
    retryable: bool


class FactBatchResult(StrictFrozenModel):
    results: tuple[FactGroupAcknowledgement, ...]
    failures: tuple[FactGroupFailure, ...] = ()
```

Define stable codes `AUTOMATION_NOT_FOUND`, `CROSS_SCOPE_RELATION`,
`AUTOMATION_SEQUENCE_GAP`, `AUTOMATION_SEQUENCE_CONFLICT`,
`AUTOMATION_REVISION_CONFLICT`, `FACT_ID_CONFLICT`, `FACT_LINEAGE_CONFLICT`,
`INVALID_FACT_STATE`, and `TEMPORARY_CORE_FAILURE`.

- [ ] **Step 6: Run contract and existing domain tests**

Run:

```bash
uv run python -m pytest -q tests/contracts/test_trading_facts_contract.py tests/domain/test_trading_facts.py
```

Expected: all tests pass.

---

### Task 2: Idempotent `v2` command projection prerequisite

**Files:**

- Create: `src/moex_sentinel/storage/repositories/fact_command_projection.py`
- Create: `tests/storage/test_fact_command_projection.py`

**Interfaces:**

- Consumes: `sessionmaker[Session]`, legacy `TradingAutomationModel`, `AutomationStrategyModel`, `BrokerInstrumentModel`, `LegacyIdMapModel`, target reference rows and `FactAutomationCommand`.
- Produces: `FactCommandProjectionRepository.claim_commands(limit: int) -> list[FactAutomationCommand]` and `statuses(automation_ids: list[UUID]) -> FactAutomationStatusesResult`.

- [ ] **Step 1: Write failing projection tests**

Cover exact scope selection by legacy broker plus account, mapped instrument,
preserved automation ID/revision/sequence, idempotent replay, missing mapping,
ambiguous mapping and the guarantee that no decision/order/execution/history row
is copied:

```python
def test_v2_claim_projects_only_authoritative_automation(database) -> None:
    repository = FactCommandProjectionRepository(database.factory)

    first = repository.claim_commands(10)
    second = repository.claim_commands(10)

    assert first == second
    assert first[0].user_broker_id == UUID(database.user_broker_id)
    assert first[0].fact_instrument_id == UUID(database.fact_instrument_id)
    assert count(database.factory, TradingAutomationV2Model) == 1
    assert count(database.factory, TradeDecisionV2Model) == 0
```

- [ ] **Step 2: Run the test and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/storage/test_fact_command_projection.py
```

Expected: collection fails because the projection repository does not exist.

- [ ] **Step 3: Resolve one unambiguous target scope**

Inside one Core transaction, select active claimable legacy automations and
resolve mappings using these predicates:

```python
user_broker_id = session.scalar(
    select(LegacyIdMapModel.target_id)
    .join(UserBrokerV2Model, UserBrokerV2Model.id == LegacyIdMapModel.target_id)
    .where(
        LegacyIdMapModel.migration_name == REFERENCE_MIGRATION_NAME,
        LegacyIdMapModel.source_kind == "broker",
        LegacyIdMapModel.source_id == automation.broker_id,
        LegacyIdMapModel.target_kind == "user_broker",
        UserBrokerV2Model.external_account_id == automation.account_id,
    )
)
```

Resolve the legacy catalog row by `(broker_id, instrument_id)`, then resolve its
`source_kind="instrument"`, `target_kind="broker_instrument"` mapping and verify
the mapped target belongs to the selected `user_broker_id`. Require exactly one
row at both steps; raise a safe `FactCommandProjectionError` with code
`REFERENCE_MAPPING_MISSING` or `REFERENCE_MAPPING_AMBIGUOUS` otherwise.

- [ ] **Step 4: Create or verify the target aggregate**

Create `TradingAutomationDraft` and `AutomationStrategyDraft` with the legacy
automation ID, strategy ID, state, revision and sequence; use the mapped target
instrument ID and millisecond-normalized timestamps. If the target exists,
compare every projected field and return it only on exact equality; raise
`AUTOMATION_PROJECTION_CONFLICT` on divergence. Do not update an existing target
and do not read or copy legacy fact tables.

- [ ] **Step 5: Return the exact `v2` command**

Return `FactAutomationCommand` with broker-facing
`external_instrument_id=legacy_automation.instrument_id` and target-facing
`fact_instrument_id=mapped_instrument.id`. Sort commands by legacy
`created_at, id` and apply the limit after the claimable predicate.

- [ ] **Step 6: Run projection and reference-migration regression tests**

Before running, implement `statuses` as a shadow-only read of
`TradingAutomationV2Model` plus `AutomationStrategyV2Model`; preserve request
order, report unknown IDs separately, and never consult the legacy aggregate.

Run:

```bash
uv run python -m pytest -q tests/storage/test_fact_command_projection.py tests/storage/test_reference_data_migration_repository.py
```

Expected: all tests pass.

---

### Task 3: Core accepted-envelope lookup and atomic aggregate primitives

**Files:**

- Modify: `src/moex_sentinel/services/trading_fact_ports.py`
- Modify: `src/moex_sentinel/storage/repositories/automation_facts_v2.py`
- Modify: `src/moex_sentinel/storage/repositories/trading_audit_v2.py`
- Modify: `src/moex_sentinel/storage/repositories/order_facts_v2.py`
- Modify: `src/moex_sentinel/storage/repositories/position_ledger_v2.py`
- Modify: `tests/storage/test_automation_facts_v2_repository.py`
- Modify: `tests/storage/test_trading_observability_v2_repository.py`
- Modify: `tests/storage/test_order_facts_v2_repository.py`
- Modify: `tests/storage/test_position_ledger_v2_repository.py`

**Interfaces:**

- Consumes: existing session-bound shadow repositories and `AutomationEnvelopeDraft`.
- Produces: `accept_state_fact`, `accept_supporting_fact`, `replace_order_aggregate`, `append_allocation_and_decrement`, `get_envelope_by_event_id`, and `get_envelope_by_sequence`.

- [ ] **Step 1: Write failing repository tests**

Prove state facts advance sequence and revision together, supporting facts only
advance sequence, order replacement preserves identity/lineage, allocation
acceptance decrements exactly the expected lot balance, stale aggregate values
fail without mutation, and envelope lookups are scope-safe:

```python
saved = repository.accept_supporting_fact(
    "scope-1", "automation-1", expected_revision=1, expected_sequence=0,
    sequence_number=1,
)
assert saved.revision == 1
assert saved.last_sequence_number == 1
```

- [ ] **Step 2: Run focused repository tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/storage/test_automation_facts_v2_repository.py tests/storage/test_trading_observability_v2_repository.py
```

Expected: attribute failures for the four new methods.

- [ ] **Step 3: Add exact port signatures**

Add to `AutomationFactsPort`:

```python
def accept_state_fact(
    self, user_broker_id: str, automation_id: str, *,
    expected_revision: int, expected_sequence: int, sequence_number: int,
    state: AutomationState, suspended_from_state: AutomationState | None,
    hold_reason: str | None, closed_at: datetime | None,
) -> TradingAutomationDraft: ...

def accept_supporting_fact(
    self, user_broker_id: str, automation_id: str, *,
    expected_revision: int, expected_sequence: int, sequence_number: int,
) -> TradingAutomationDraft: ...
```

Add to `TradingAuditPort`:

```python
def get_envelope_by_event_id(
    self, user_broker_id: str, event_id: str,
) -> AutomationEnvelopeDraft | None: ...

def get_envelope_by_sequence(
    self, user_broker_id: str, automation_id: str, sequence_number: int,
) -> AutomationEnvelopeDraft | None: ...
```

Add to `OrderFactsPort` and `PositionLedgerPort`:

```python
def replace_order_aggregate(
    self, user_broker_id: str, value: BrokerOrderDraft,
) -> BrokerOrderDraft: ...

def append_allocation_and_decrement(
    self, user_broker_id: str, value: ExecutionLotAllocationDraft, *,
    expected_remaining_lots: int, remaining_lots_after: int,
) -> ExecutionLotAllocationDraft: ...
```

- [ ] **Step 4: Implement single-statement compare-and-set updates**

Both aggregate methods must match scope, automation ID, expected revision and
expected previous sequence. `accept_state_fact` writes `revision + 1`; the
supporting method leaves revision unchanged. Both require
`sequence_number == expected_sequence + 1` before executing SQL and translate a
zero-row result into a stable sequence or revision conflict after a scoped read.

- [ ] **Step 5: Implement envelope lookups and run tests**

Return `None` rather than raising when no envelope exists. Always filter by
`user_broker_id`; sequence lookup also filters by `automation_id`.

`replace_order_aggregate` must match scope, order ID, automation, decision,
cycle, instrument and idempotency key, then replace only mutable order fields.
`append_allocation_and_decrement` validates sell execution/lot lineage, performs
a compare-and-set lot update from `expected_remaining_lots` to
`remaining_lots_after`, and appends the immutable allocation in the same
session. A zero-row update is `INVALID_FACT_STATE`.

Run:

```bash
uv run python -m pytest -q tests/storage/test_automation_facts_v2_repository.py tests/storage/test_trading_observability_v2_repository.py tests/storage/test_trading_facts_uow.py
```

Expected: all tests pass.

---

### Task 4: Typed Core mapping and per-automation ingress service

**Files:**

- Create: `src/moex_sentinel/services/trading_fact_mapping.py`
- Create: `src/moex_sentinel/services/trading_fact_ingress.py`
- Create: `tests/services/test_trading_fact_ingress.py`

**Interfaces:**

- Consumes: `FactEnvelope`, `TradingFactsUnitOfWorkPort`, all `0.2.1.2` draft types and `utc_now_ms`.
- Produces: `TradingFactMapper.apply(uow, envelope) -> None` and `TradingFactIngressService.publish(facts: list[FactEnvelope]) -> FactBatchResult`.

- [ ] **Step 1: Write failing mapping and service tests**

Parameterize all nine fact kinds. Add explicit tests for mixed automation
partial success, exact retry, conflicting event identity, duplicate sequence,
sequence gap, revision conflict, cross-scope lineage, state revision increment,
supporting-fact stable revision and rollback of every fact in a failed group.

```python
def test_failed_group_does_not_rollback_successful_peer(service, facts) -> None:
    result = service.publish([facts.valid_a, facts.invalid_b])

    assert [item.automation_id for item in result.results] == [facts.automation_a]
    assert result.failures[0].automation_id == facts.automation_b
    assert result.failures[0].retryable is False
```

- [ ] **Step 2: Run the service test and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/services/test_trading_fact_ingress.py
```

Expected: collection fails because the mapper and service do not exist.

- [ ] **Step 3: Implement explicit payload-to-draft mapping**

Use `event_id` as immutable `fact_id` for decision, initial order, order-event
and execution drafts; use payload-owned IDs as row primary keys. Add envelope scope,
automation and `received_at` explicitly. The dispatch must be exhaustive and
must raise on an unknown runtime type:

```python
match envelope:
    case TradeDecisionRecordedEnvelope(payload=payload):
        uow.orders.append_decision(
            str(envelope.user_broker_id),
            TradeDecisionDraft(
                id=str(payload.decision_id),
                fact_id=str(envelope.event_id),
                user_broker_id=str(envelope.user_broker_id),
                automation_id=str(envelope.automation_id),
                **payload.model_dump(mode="python", exclude={"decision_id"}),
            ),
        )
    case _:
        raise AssertionError(f"Unsupported fact envelope type: {type(envelope).__name__}")
```

For `POSITION_CYCLE_UPDATED`, call `get_cycle`; on `NOT_FOUND` call
`open_cycle`, otherwise call `replace_cycle_aggregate`. Do not catch a different
persistence error as a missing cycle.

For `BROKER_ORDER_STATE_CHANGED`, first build the complete current
`BrokerOrderDraft` and call `replace_order_aggregate`, then append the immutable
`BrokerOrderEventDraft`. For `EXECUTION_LOT_ALLOCATED`, call
`append_allocation_and_decrement` with
`expected_remaining_lots=remaining_lots_after + allocated_lots`.

- [ ] **Step 4: Implement exact-retry normalization**

Normalize an incoming envelope into `AutomationEnvelopeDraft` with
`payload=envelope.payload.model_dump(mode="json")`. An exact retry requires
equality of event ID, scope, automation, sequence, expected revision, kind,
safe message, payload and occurred time; ignore only the stored `received_at`.
If event ID or sequence resolves to a different normalized envelope, return
`FACT_ID_CONFLICT` or `AUTOMATION_SEQUENCE_CONFLICT`.

- [ ] **Step 5: Implement one transaction per automation group**

The constructor is:

```python
class TradingFactIngressService:
    def __init__(
        self,
        uow_factory: Callable[[], TradingFactsUnitOfWorkPort],
        mapper: TradingFactMapper,
        *,
        now: Callable[[], datetime] = utc_now_ms,
    ) -> None: ...

    def publish(self, facts: list[FactEnvelope]) -> FactBatchResult: ...
```

Group by `automation_id`, sort each group by `(sequence_number, event_id)`, and
open a fresh UOW per group. Recognize exact historical retries first. For every
new event require `sequence == current.last_sequence_number + 1` and
`expected_revision == current.revision`; apply the typed draft, append the
envelope, then call the matching aggregate accept method. Catch only typed safe
errors into non-retryable group failures; map an unexpected exception to
`TEMPORARY_CORE_FAILURE` with `retryable=True` after the UOW rolls back.

- [ ] **Step 6: Run service, repository and UOW tests**

Run:

```bash
uv run python -m pytest -q tests/services/test_trading_fact_ingress.py tests/storage/test_trading_facts_uow.py tests/storage/test_order_facts_v2_repository.py tests/storage/test_position_ledger_v2_repository.py
```

Expected: all tests pass.

---

### Task 5: Core `v2` command and fact HTTP boundary

**Files:**

- Create: `src/moex_sentinel/usecases/trading_fact_ingress.py`
- Create: `src/moex_sentinel/views/schemas/trading_facts.py`
- Create: `src/moex_sentinel/views/internal_trading_facts.py`
- Modify: `src/moex_sentinel/composition.py`
- Modify: `src/moex_sentinel/api/app.py`
- Create: `tests/api/test_trading_fact_ingress_contract.py`
- Modify: `tests/api/test_automation_contract.py`

**Interfaces:**

- Consumes: `FactCommandProjectionRepository`, `TradingFactIngressService`, shared request/result DTOs.
- Produces: `POST /internal/v2/automation-commands`, `POST /internal/v2/automation-statuses` and `POST /internal/v2/automation-facts`.

- [ ] **Step 1: Write failing HTTP contract tests**

Assert strict `422` with zero writes for any malformed envelope, success for all
nine kinds, one result per automation, partial group success, bounded safe
failure output, and that the legacy `/internal/automaton/events` route remains
unchanged.

```python
response = client.post("/internal/v2/automation-facts", json=invalid_batch)
assert response.status_code == 422
assert count_accepted_envelopes(factory) == 0
```

- [ ] **Step 2: Run API tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/api/test_trading_fact_ingress_contract.py
```

Expected: `404` for all three new routes.

- [ ] **Step 3: Add thin usecases and schemas**

Define:

```python
class ClaimFactAutomationCommandsUsecase:
    def __init__(self, repository: FactCommandProjectionRepository) -> None: ...
    def execute(self, limit: int) -> list[FactAutomationCommand]: ...


class PublishTradingFactsUsecase:
    def __init__(self, service: TradingFactIngressService) -> None: ...
    def execute(self, facts: list[FactEnvelope]) -> FactBatchResult: ...


class ViewFactAutomationStatusesUsecase:
    def __init__(self, repository: FactCommandProjectionRepository) -> None: ...
    def execute(self, automation_ids: list[UUID]) -> FactAutomationStatusesResult: ...
```

HTTP schemas may wrap shared strict DTOs but must not duplicate payload field
definitions. `PublishFactsRequestSchema.facts` is `list[FactEnvelope]` and its
response is built from `FactBatchResult`.

- [ ] **Step 4: Register dependencies and routes**

Add `claim_fact_automation_commands` and `publish_trading_facts` to
`ApplicationUsecases`. Construct `TradingFactIngressService` with a fresh
`TradingFactsShadowUnitOfWork(factory)` per group. Include one router with prefix
`/internal/v2` and the exact paths `/automation-commands`,
`/automation-statuses` and `/automation-facts`.

- [ ] **Step 5: Run API and composition tests**

Run:

```bash
uv run python -m pytest -q tests/api/test_trading_fact_ingress_contract.py tests/api/test_automation_contract.py tests/usecases/test_automaton_sync_usecases.py
```

Expected: all tests pass and legacy contract assertions remain unchanged.

---

### Task 6: Worker `fact_outbox_v2` schema and durable identities

**Files:**

- Modify: `src/trading_automaton/storage/models.py`
- Modify: `src/trading_automaton/storage/database.py`
- Modify: `src/trading_automaton/domain/storage_dtos.py`
- Create: `tests/trading_automaton/storage/test_fact_outbox_v2.py`
- Modify: `tests/trading_automaton/storage/test_worker_database.py`

**Interfaces:**

- Consumes: shared `FactKind`, `FactEnvelope`, Worker `UTCDateTime` and existing recovery models.
- Produces: `FactOutboxV2Model`, `FactOutboxRecord`, stable execution/cycle identity columns and an additive restart-safe SQLite upgrade.

- [ ] **Step 1: Write failing schema and restart tests**

Assert exact columns, unique `(automation_id, sequence_number)`, UUID event key,
mixed fact kinds, retry fields, millisecond defaults, additive upgrade from a
pre-`0.2.1.3` SQLite file, and persistence after engine disposal/reopen.

- [ ] **Step 2: Run storage tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/trading_automaton/storage/test_fact_outbox_v2.py tests/trading_automaton/storage/test_worker_database.py
```

Expected: missing table/model assertions fail.

- [ ] **Step 3: Add the outbox model**

Define `fact_outbox_v2` with:

```python
class FactOutboxV2Model(Base, TimestampMixin):
    __tablename__ = "fact_outbox_v2"
    __table_args__ = (
        UniqueConstraint("automation_id", "sequence_number", name="uq_fact_outbox_v2_sequence"),
        Index("ix_fact_outbox_v2_retry", "delivery_state", "next_retry_at"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_broker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    automation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    fact_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    safe_message: Mapped[str] = mapped_column(String(1000), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
```

- [ ] **Step 4: Add recovery identity columns**

Add nullable `user_broker_id`, `fact_instrument_id` and
`position_cycle_id` to `CachedAutomationModel`; add nullable
`fact_execution_id` and `position_cycle_id` to `LocalIntentModel`. Existing
legacy rows remain valid. `upgrade_worker_schema` adds every missing column and
creates `fact_outbox_v2` with `FactOutboxV2Model.__table__.create(engine,
checkfirst=True)`.

- [ ] **Step 5: Add the frozen storage DTO and run tests**

`FactOutboxRecord` mirrors all envelope and retry columns, with payload typed as
`dict[str, object]`. Run:

```bash
uv run python -m pytest -q tests/trading_automaton/storage/test_fact_outbox_v2.py tests/trading_automaton/storage/test_worker_database.py
```

Expected: all tests pass.

---

### Task 7: Worker fact writer, validation and continuous sequence allocation

**Files:**

- Create: `src/trading_automaton/storage/fact_outbox_v2.py`
- Modify: `src/trading_automaton/storage/repository.py`
- Modify: `tests/trading_automaton/storage/test_fact_outbox_v2.py`

**Interfaces:**

- Consumes: one SQLAlchemy `Session`, `CachedAutomationModel`, a concrete shared payload, `utc_now_ms` and UUID factory.
- Produces: `FactOutboxWriter.append(...) -> FactEnvelope`, ready-row selection, selective acknowledgement and retry scheduling.

- [ ] **Step 1: Write failing writer tests**

Prove consecutive allocation across several fact kinds in one transaction,
rollback with the recovery mutation, validation before insert, one unified
ordering, due-by-count, due-by-deadline, exact retry retention and continuous
prefix acknowledgement scoped to one automation.

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/trading_automaton/storage/test_fact_outbox_v2.py
```

Expected: writer and repository methods are absent.

- [ ] **Step 3: Implement the session-bound writer**

Use this interface:

```python
class FactOutboxWriter:
    def __init__(self, id_factory: Callable[[], UUID] = uuid4) -> None: ...

    def append(
        self,
        session: Session,
        cached: CachedAutomationModel,
        *,
        payload: FactPayload,
        safe_message: str,
        occurred_at: datetime,
        changes_revision: bool = False,
    ) -> FactEnvelope: ...
```

Require cached scope and target instrument identity. Allocate
`sequence_number=cached.last_sequence_number + 1`, use the current revision as
`expected_revision`, validate the concrete envelope through the shared type,
insert its JSON payload, update cached sequence, and increment cached revision
only when `changes_revision=True`. Normalize locally generated event time to
milliseconds before constructing the strict envelope.

- [ ] **Step 4: Add repository query/ack/retry methods**

Define exact signatures:

```python
def ready_fact_outbox(
    self, limit: int, *, now: datetime, deadline_ms: int,
) -> list[FactOutboxRecord]: ...

def acknowledge_fact_outbox(
    self, automation_id: str, *, accepted_through_sequence: int,
    current_revision: int,
) -> None: ...

def schedule_fact_retry(
    self, event_ids: tuple[str, ...], *, retry_count: int,
    next_retry_at: datetime,
) -> None: ...
```

The ready query selects one global queue ordered by `(occurred_at, event_id)`.
Return up to `limit` rows when the ready count reaches `limit`, or when the
oldest ready row is at least `deadline_ms` old; otherwise return an empty list.
Acknowledgement deletes only matching automation rows at or below the accepted
sequence and updates cached revision. It does not delete intents, lots,
allocations, cycle state or audit journal rows.

- [ ] **Step 5: Run writer and legacy outbox regression tests**

Run:

```bash
uv run python -m pytest -q tests/trading_automaton/storage/test_fact_outbox_v2.py tests/trading_automaton/storage/test_local_repository.py
```

Expected: all tests pass and legacy outbox behavior is unchanged when selected.

---

### Task 8: Atomic state, decision and broker-order fact production

**Files:**

- Modify: `src/trading_automaton/storage/repository.py`
- Modify: `src/trading_automaton/domain/storage_dtos.py`
- Modify: `src/trading_automaton/services/decision_materialization.py`
- Modify: `tests/trading_automaton/storage/test_local_repository.py`
- Modify: `tests/trading_automaton/services/test_synchronization_service.py`

**Interfaces:**

- Consumes: `FactOutboxWriter`, v2-enriched cached command, decision batch values and intent lifecycle values.
- Produces: atomic `AUTOMATION_STATE_CHANGED`, `TRADE_DECISION_RECORDED`, `BROKER_ORDER_RECORDED`, `BROKER_ORDER_STATE_CHANGED` facts.

- [ ] **Step 1: Write failing atomicity and payload tests**

Add tests for claim-to-`IN_WORK`, hold, `WAIT`, `NO_ACTION`, a decision that
creates an intent, and every later order lifecycle state. For a forced exception
after fact construction, assert both recovery mutation and fact rows roll back.
Assert no legacy `OutboxEventModel` is inserted in `v2` mode.

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/trading_automaton/storage/test_local_repository.py -k 'state or decision or intent or fact'
```

Expected: missing v2 fact assertions fail.

- [ ] **Step 3: Make ingress selection explicit at repository construction**

Add `fact_ingress_version: Literal["legacy", "v2"] = "legacy"` and one
`FactOutboxWriter` to `LocalAutomationRepository.__init__`. Every fact-producing
transaction uses exactly one branch. Do not write an `OutboxEventModel` in the
`v2` branch and do not write a `FactOutboxV2Model` in the legacy branch.

- [ ] **Step 4: Produce state facts with local revision advancement**

In `save_state_and_event` and `hold_active`, build
`AutomationStateChangedPayload` from the new state, previous state, safe hold
reason and terminal time. Call writer with `changes_revision=True`; the cached
revision becomes the revision Core will return after acceptance, so supporting
facts created later in the same local timeline carry the correct expected
revision.

- [ ] **Step 5: Produce decision and initial order facts**

Extend `DecisionBatchItem` with `indicators: dict[str, object]` and ensure
`decision_materialization.py` supplies the already calculated safe indicators.
After inserting each `TradeDecisionModel`, emit one decision fact for every
decision including `WAIT` and `NO_ACTION`. When an intent exists, use its
idempotency key as stable target `order_id`, link it to the just-created
decision ID, and emit one `BROKER_ORDER_RECORDED` with state
`DISPATCH_PENDING`. Do not emit a separate order-state event for the initial
record.

- [ ] **Step 6: Produce subsequent order lifecycle facts**

Capture `from_state` before each mutation in `update_intent` and
`finalize_execution`. Emit `BROKER_ORDER_STATE_CHANGED` after the mutation with
stable order ID, safe reason equal to the target state, and no broker driver
message. Exact replay of an already terminal intent must not create another
fact.

- [ ] **Step 7: Run state/decision/order tests**

Run:

```bash
uv run python -m pytest -q tests/trading_automaton/storage/test_local_repository.py tests/trading_automaton/services/test_streaming_batch_tick_service.py tests/trading_automaton/services/test_decision_execution_service.py
```

Expected: all tests pass.

---

### Task 9: Atomic execution, cycle, lot, allocation and audit fact production

**Files:**

- Modify: `src/trading_automaton/storage/repository.py`
- Modify: `tests/trading_automaton/storage/test_local_repository.py`
- Modify: `tests/trading_automaton/storage/test_execution_currency_replay.py`
- Modify: `tests/integration/test_worker_storage_isolation.py`

**Interfaces:**

- Consumes: terminal `ExecutionFinalization`, Worker lot/allocation rows, position snapshot and `BusinessAuditEvent`.
- Produces: atomic `TRADE_EXECUTION_RECORDED`, `POSITION_CYCLE_UPDATED`, `POSITION_LOT_OPENED`, `EXECUTION_LOT_ALLOCATED`, and `TRADE_AUDIT_RECORDED` facts with stable lineage.

- [ ] **Step 1: Write failing execution graph tests**

Cover a first filled buy, averaging buy, partial sell with multiple LIFO
allocations, full close, rejected order, exact terminal replay, rollback, and
audit insertion. Assert the emitted sequence and lineage:

```python
assert kinds == [
    FactKind.BROKER_ORDER_STATE_CHANGED,
    FactKind.TRADE_EXECUTION_RECORDED,
    FactKind.POSITION_LOT_OPENED,
    FactKind.POSITION_CYCLE_UPDATED,
]
assert lot.payload["buy_execution_id"] == execution.payload["execution_id"]
```

- [ ] **Step 2: Run focused execution tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/trading_automaton/storage/test_local_repository.py -k 'finalize or lot or allocation or audit'
```

Expected: typed execution graph assertions fail.

- [ ] **Step 3: Allocate stable execution and cycle identities**

On the first applied terminal fill, set `intent.fact_execution_id` once. For a
buy with no active cycle, set `cached.position_cycle_id` once before creating
facts. Store the chosen cycle ID on the intent. A sell uses the current cached
cycle. Terminal replay validates and reuses both IDs without appending rows.

- [ ] **Step 4: Emit the financial fact graph in dependency order**

After local rows are flushed, append facts in lineage-safe order. The first BUY
of a new cycle uses: terminal `BROKER_ORDER_STATE_CHANGED`, opening
`POSITION_CYCLE_UPDATED`, `TRADE_EXECUTION_RECORDED`, then
`POSITION_LOT_OPENED`. A BUY in an existing cycle uses: order transition,
execution, lot, then updated cycle. A SELL uses: order transition, execution,
one `EXECUTION_LOT_ALLOCATED` per persisted allocation ordered by
`(closed_at, id)` and including the persisted lot balance after that allocation,
then updated or closed cycle. This guarantees every referenced cycle,
execution and lot exists before its child fact is applied in Core.

Use persisted lot/allocation monetary values. For allocation `entry_value` is
`lot.entry_price * allocated_lots * lot_size`; `exit_value` is
`allocation.exit_price * allocated_lots * lot_size`. On quantity zero, mark the
cycle `CLOSED`, set `closed_at`, emit the fact, then clear
`cached.position_cycle_id`.

- [ ] **Step 5: Emit audit facts in the audit journal transaction**

In `append_audit_events`, retain the Worker-local journal row and, only in `v2`
mode, append one `TRADE_AUDIT_RECORDED` envelope in the same SQLite transaction.
Resolve target scope/instrument from cached automation. Do not run the separate
legacy audit-delivery service in `v2` mode. Audit event replay remains
idempotent by event ID.

- [ ] **Step 6: Prove recovery records survive acknowledgement**

After acknowledging every emitted fact, assert terminal and non-terminal intent
rows, trade lots, allocations, cycle state and business audit rows still exist.
Only the accepted outbox prefix is removed.

- [ ] **Step 7: Run execution, audit and restart tests**

Run:

```bash
uv run python -m pytest -q tests/trading_automaton/storage/test_local_repository.py tests/trading_automaton/storage/test_execution_currency_replay.py tests/integration/test_worker_storage_isolation.py
```

Expected: all tests pass.

---

### Task 10: Worker `v2` HTTP adapter and batch synchronization

**Files:**

- Modify: `src/trading_automaton/adapters/core_client.py`
- Create: `src/trading_automaton/services/fact_synchronization.py`
- Modify: `src/trading_automaton/domain/core_contracts.py`
- Modify: `tests/trading_automaton/adapters/test_core_client.py`
- Create: `tests/trading_automaton/services/test_fact_synchronization.py`

**Interfaces:**

- Consumes: `FactAutomationCommand`, `FactEnvelope`, `FactBatchResult`, ready/ack/retry repository methods and Core status reconciliation.
- Produces: `CoreClient.claim_fact_commands`, `CoreClient.fact_automation_statuses`, `CoreClient.publish_facts`, and `FactSynchronizationService` implementing the coordinator sync protocol.

- [ ] **Step 1: Write failing adapter and publisher tests**

Cover exact route/body serialization, mixed automation/fact batches, count and
deadline triggers, network/timeout/`5xx` retry with byte-equivalent facts,
partial acknowledgement, retryable group failure, non-retryable reconciliation
and hold, selective acknowledgement, and no fallback call to legacy methods.

- [ ] **Step 2: Run tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/trading_automaton/adapters/test_core_client.py tests/trading_automaton/services/test_fact_synchronization.py
```

Expected: new adapter/service symbols are missing.

- [ ] **Step 3: Add exact Core client methods**

```python
def claim_fact_commands(self, worker_id: str, limit: int) -> list[FactAutomationCommand]:
    response = self._http.post(
        "/internal/v2/automation-commands",
        json={"worker_id": worker_id, "limit": limit},
        headers=self._headers(),
    )
    response.raise_for_status()
    return [FactAutomationCommand.model_validate(item) for item in response.json()["commands"]]

def publish_facts(self, facts: list[FactEnvelope]) -> FactBatchResult:
    request = FactBatchRequest(facts=facts)
    response = self._http.post(
        "/internal/v2/automation-facts",
        json=request.model_dump(mode="json"),
        headers=self._headers(),
    )
    response.raise_for_status()
    return FactBatchResult.model_validate(response.json())
```

Add `fact_automation_statuses(automation_ids: list[UUID]) ->
FactAutomationStatusesResult` using only
`POST /internal/v2/automation-statuses`.

Keep legacy methods unchanged.

- [ ] **Step 4: Implement the v2 synchronization service**

Constructor:

```python
class FactSynchronizationService:
    def __init__(
        self, repository: FactRepositoryPort, client: FactCoreClientPort, *,
        now: Callable[[], datetime], sleep: Callable[[float], None],
        jitter: Callable[[int], float] = lambda _attempt: 0.0,
        retry_limit: int = 5, batch_size: int = 100,
        deadline_ms: int = 1000,
    ) -> None: ...
```

`claim_commands` calls only `claim_fact_commands` and caches target identities.
`flush_outbox` gets one ready unified batch. Network, timeout and `5xx` repeat
the unchanged in-memory envelopes with bounded exponential backoff; other HTTP
`4xx` are non-retryable and trigger reconciliation/hold. For a successful HTTP
response, acknowledge each success independently. Retain retryable failures;
resolve non-retryable failures through `fact_automation_statuses`, then reconcile
or hold without deleting their fact rows.

- [ ] **Step 5: Run adapter and publisher tests**

Run:

```bash
uv run python -m pytest -q tests/trading_automaton/adapters/test_core_client.py tests/trading_automaton/services/test_fact_synchronization.py tests/trading_automaton/services/test_synchronization_service.py
```

Expected: both `v2` and legacy suites pass.

---

### Task 11: Exclusive configuration, composition and coordinator batching

**Files:**

- Modify: `src/trading_automaton/config.py`
- Modify: `src/trading_automaton/composition.py`
- Modify: `src/trading_automaton/services/streaming_runtime_coordinator.py`
- Modify: `compose.yml`
- Modify: `.env.example`
- Modify: `tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py`
- Modify: `tests/test_compose_config.py`

**Interfaces:**

- Consumes: legacy `SynchronizationService`, new `FactSynchronizationService`, shared coordinator protocol.
- Produces: one selected ingress path with explicit batching settings and no busy loop below the deadline.

- [ ] **Step 1: Write failing configuration/composition tests**

Assert only `legacy` or `v2` is accepted, positive batch/deadline values, legacy
default, explicit Compose legacy value, v2 composition uses only fact sync and
disables legacy audit delivery, and a sub-threshold batch does not make the
coordinator loop indefinitely.

- [ ] **Step 2: Run tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/test_compose_config.py tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py
```

Expected: settings and selection assertions fail.

- [ ] **Step 3: Add exact settings**

```python
fact_ingress_version: Literal["legacy", "v2"] = Field(
    "legacy", validation_alias="FACT_INGRESS_VERSION"
)
fact_outbox_batch_size: int = Field(100, validation_alias="FACT_OUTBOX_BATCH_SIZE")
fact_outbox_deadline_ms: int = Field(1000, validation_alias="FACT_OUTBOX_DEADLINE_MS")
```

Validate positive batch size/deadline. Set these explicit Compose values:

```yaml
FACT_INGRESS_VERSION: legacy
FACT_OUTBOX_BATCH_SIZE: "100"
FACT_OUTBOX_DEADLINE_MS: "1000"
```

- [ ] **Step 4: Select exactly one repository/service path**

Construct `LocalAutomationRepository(...,
fact_ingress_version=settings.fact_ingress_version)`. Build legacy sync and
legacy `AuditDeliveryService` only for `legacy`; build fact sync and pass no
legacy audit delivery for `v2`. No `try/except` may instantiate the other
version after a failure.

Inject a version-matched Core status port into the coordinator: legacy uses
`automation_statuses`, while `v2` uses `fact_automation_statuses`. Never read
legacy status after a `v2` fact acknowledgement.

- [ ] **Step 5: Remove the below-deadline busy loop**

Change coordinator `_flush_all` to make one synchronization decision per call.
`flush_outbox()` returning `True` with no due rows allows the iteration to
continue; subsequent iterations reach the deadline. Shutdown and explicit
critical paths may call a `force=True` method only if that method still uses the
same selected ingress and immutable facts.

- [ ] **Step 6: Run configuration, coordinator and runtime tests**

Run:

```bash
uv run python -m pytest -q tests/test_compose_config.py tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py tests/trading_automaton/services/test_runtime_service.py
```

Expected: all tests pass.

---

### Task 12: PostgreSQL end-to-end and architecture acceptance

**Files:**

- Create: `tests/integration/postgresql/test_trading_fact_ingress.py`
- Modify: `tests/integration/test_worker_storage_isolation.py`
- Modify: `tests/integration/test_trading_facts_shadow_isolation.py`

**Interfaces:**

- Consumes: real Worker SQLite repository, FastAPI `v2` endpoint, Core PostgreSQL shadow schema and HTTP adapter.
- Produces: acceptance evidence for projection, fact graph, batching, retry, isolation and selective acknowledgement.

- [ ] **Step 1: Add Worker import-boundary assertions**

Scan production Worker imports and reject `moex_sentinel.domain`,
`moex_sentinel.storage.models`, and `moex_sentinel.storage.repositories` from
the new outbox, publisher and contract paths. Permit broker adapters already
shared by the current architecture only where existing isolation tests permit
them.

- [ ] **Step 2: Add production-v2 legacy-write assertions**

Scan the new Core ingress service/usecase/view imports and reject legacy trading
repositories. In a database test, publish a valid v2 batch and assert legacy
automation event/order/execution row counts are unchanged.

- [ ] **Step 3: Add PostgreSQL end-to-end acceptance**

Seed synthetic reference mappings and one claimable automation, claim a v2
command, cache it in Worker SQLite, produce decision/order/execution/lot/cycle
facts, publish through HTTP, and assert the normalized PostgreSQL graph plus
Worker selective acknowledgement. Then replay the identical HTTP batch and
assert row counts and financial totals do not change.

- [ ] **Step 4: Add partial success and outage recovery**

Publish a mixed batch with one valid automation and one revision conflict;
assert the valid group commits and is acknowledged while the failed group stays
in SQLite. Recreate the Worker repository from the same SQLite file, correct
the synthetic authoritative state, republish the unchanged retryable group and
assert eventual acceptance.

- [ ] **Step 5: Run SQLite architecture tests**

Run:

```bash
uv run python -m pytest -q tests/integration/test_worker_storage_isolation.py tests/integration/test_trading_facts_shadow_isolation.py
```

Expected: all tests pass.

- [ ] **Step 6: Run PostgreSQL acceptance**

Run:

```bash
POSTGRES_USER=moex_sentinel_user docker compose --profile migrations run -T --rm --user root -v "$PWD/tests:/app/tests:ro" migrations sh -eu -c 'python -m pip install --no-cache-dir "pytest>=8.4,<9" >/dev/null && POSTGRES_TEST_DATABASE_URL="$DATABASE_URL" pytest -q -m postgresql tests/integration/postgresql'
```

Expected: every PostgreSQL-marked test passes, including the new ingress graph.

---

### Task 13: Documentation, milestone evidence and full regression

**Files:**

- Modify: `AGENT_BRIEF.md`
- Modify: `README.md`
- Modify: `docs/development.md`
- Modify: `docs/phase-0-trading-service-refactor.md`
- Modify: `docs/phase-1-implementation-plan.md`
- Modify: `tests/test_documentation.py`

**Interfaces:**

- Consumes: verified implementation and gate outputs from Tasks 1–12.
- Produces: accurate `0.2.1.3` status, operational configuration/rollback instructions and next pointer `0.2.1.4`.

- [ ] **Step 1: Write failing documentation assertions**

Require all documents to state: production remains `legacy` through this
milestone, isolated acceptance uses `v2`, no dual publish/fallback, history is
still unmigrated, next stage is `0.2.1.4`, cutover is `0.2.1.5`, and cleanup is
`0.2.1.6`.

- [ ] **Step 2: Run documentation tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/test_documentation.py
```

Expected: new required fragments are missing.

- [ ] **Step 3: Update operational and milestone documentation**

Document `FACT_INGRESS_VERSION`, batch size, deadline, the two internal v2
routes, deployment order, isolated smoke procedure, manual rollback selection,
outbox retention and the fact that the production switch is intentionally
deferred. Mark `0.2.1.3` complete only after every gate below is green.

- [ ] **Step 4: Run the complete Python suite**

Run:

```bash
uv run python -m pytest -q
```

Expected: all tests pass; only documented pre-existing skips/warnings remain.

- [ ] **Step 5: Run static gates**

Run:

```bash
uv run ruff check .
uv run black --check .
uv run mypy src tests
```

Expected: Ruff clean, Black unchanged, strict mypy reports zero errors.

- [ ] **Step 6: Run frontend regression**

Run:

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: frontend tests and production build pass.

- [ ] **Step 7: Re-run PostgreSQL and documentation gates**

Run the PostgreSQL command from Task 12, then:

```bash
uv run python -m pytest -q tests/test_documentation.py tests/integration/test_worker_storage_isolation.py tests/integration/test_trading_facts_shadow_isolation.py
```

Expected: all gates pass. Record exact counts in the milestone documents, keep
`0.2.1.4` as the next unimplemented stage, and do not create a Git commit.
