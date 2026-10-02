# Phase 0 Static Port Integrity Design

## Status

- Date: 2026-08-09
- Decision: approved, option 2
- Scope: milestone `0.2.0`
- Priority: CRIT
- Constraint: no commits; no trading behavior or persistence behavior changes

## Problem

`uv run mypy src/trading_automaton/composition.py` reports 14 errors after the
Pydantic stabilization phase. All errors are at the composition boundary: a
consumer-owned Protocol advertises a broad method such as
`method(**values: object)`, while the concrete service or repository implements a
strict keyword-only signature.

These declarations are not substitutable. A concrete implementation that accepts
only named fields cannot satisfy a Protocol promising that every arbitrary keyword
is accepted. The current Protocols therefore describe capabilities the injected
objects do not have.

## Decision

Use exact consumer contracts and apply the project rule "do not generalize before
three uses":

- `IntentUpdatePort` becomes shared because `update_intent` is consumed by order
  tracking, order dispatch, and uncertain reconciliation;
- `OrderStageAuditPort` becomes shared because `record_order_stage` is consumed by
  tracking, dispatch, reconciliation, and synchronization;
- ports with one or two consumers remain local to their consuming service;
- shared ports live in `trading_automaton.domain.ports`, next to the domain DTOs
  used by their signatures;
- no service imports a concrete repository or adapter merely to satisfy typing.

The change is static-contract refactoring. Calls, order of operations, exception
handling, persistence transactions, and broker interaction remain unchanged.

## Rejected alternatives

### Duplicate every exact signature

This has a small initial diff but repeats a long `update_intent` contract three
times and an audit contract four times. The copies would drift independently.

### Replace every call with a new Pydantic command DTO

Command DTOs may become useful while designing the unified fact sink, but adding
them now would change working service and repository interfaces. That work belongs
to `0.2.1/0.2.2` and is unnecessary for closing the static contract defect.

## Shared ports

### IntentUpdatePort

The method mirrors the authoritative repository contract and returns the domain
record rather than `object`:

```python
class IntentUpdatePort(Protocol):
    def update_intent(
        self,
        idempotency_key: str,
        *,
        state: str,
        occurred_at: datetime,
        broker_order_id: str | None = None,
        requested_amount: Decimal | None = None,
        executed_amount: Decimal | None = None,
        estimated_commission: Decimal | None = None,
        executed_commission: Decimal | None = None,
        executed_lots: int | None = None,
        executed_price: Decimal | None = None,
        executed_at: datetime | None = None,
        dispatch_started_at: datetime | None = None,
        broker_responded_at: datetime | None = None,
        terminal_at: datetime | None = None,
        process_id: str | None = None,
    ) -> LocalIntentRecord: ...
```

`TrackingRepositoryPort`, `DispatchRepositoryPort`, and
`ReconciliationRepositoryPort` inherit this port and add only their own methods.

### OrderStageAuditPort

Required context is explicit; only the sanitized stage-specific payload remains
open:

```python
class OrderStageAuditPort(Protocol):
    def record_order_stage(
        self,
        *,
        stage: BusinessAuditStage,
        process_id: str,
        automation_id: str,
        broker_id: str,
        account_id: str,
        instrument_id: str,
        message: str,
        critical: bool = False,
        **data: object,
    ) -> None: ...
```

Tracking, dispatch, reconciliation, and synchronization consume this same port.
`**data: object` is retained here because the concrete audit service intentionally
supports a filtered extension payload; it is not used to hide required fields.

## Local consumer ports

### Position hydration

- `PositionConsistencyPort.reconcile` accepts `automation_id`, `broker_lots`, and
  `average_price`; it returns `PositionConsistencyResult`.
- `PositionAuditPort.record_reconciliation` explicitly requires stage and full
  process/automation/broker/account/instrument context, with `**data: object` only
  for the filtered reconciliation payload.

### Streaming batch audit

`BusinessAuditPort.record_decision_process` explicitly requires
`process_id`, `automation_id`, `broker_id`, `account_id`, and `instrument_id` and
retains only decision-specific `**data: object`.

### Order tracking and reconciliation

The two ledger ports remain consumer-local because there are only two uses. Both
mirror the concrete `LotLedgerService` operations:

- `record_buy_execution(...) -> TradeLotRecord`;
- `allocate_sell_execution(...) -> None`, including required `lot_size`.

`CommissionObservationPort` uses `Decimal` for amounts and returns
`AccountCommissionProfile | None`.

`TrackingRepositoryPort.save_execution_event` declares the exact automation,
time, process, position snapshot, and operation fields.

### Coordinator repository

`CoordinatorRepositoryPort` declares exact signatures for:

- `save_state_and_event`;
- `synchronize_core_state`;
- `pending_outbox -> list[OutboxRecord]`.

The existing `list_monitored`, `list_active`, and `hold_active` declarations stay
unchanged. JSON-like dictionaries use `object` values in new port declarations.
If an existing concrete annotation uses `Any`, it may be narrowed to `object` only
when the body and every caller already satisfy that type; `Any` must not be added
as a compatibility escape.

## Module boundaries

```text
trading_automaton.domain.ports
    IntentUpdatePort
    OrderStageAuditPort

service module
    local consumer-specific Protocols
    inherits shared port only at >= 3 real uses

composition.py
    injects concrete services/repository
    contains no casts or type ignores for port compatibility
```

Domain ports may import only standard-library types, `sentinel_contracts` enums,
and DTOs from `trading_automaton.domain`. They must not import SQLAlchemy models,
adapters, or composition code.

## Error handling and runtime behavior

This milestone introduces no new exception mapping. Protocol declarations mirror
existing callable behavior and must not add catch/rethrow wrappers. Existing typed
exceptions and recovery boundaries remain intact.

No call is reordered, no retry policy changes, and no audit field filtering is
changed.

## Decomposition

### 0.2.0.1 Shared repeated ports

Create and cover `IntentUpdatePort` and `OrderStageAuditPort`, then migrate their
three/four consumer declarations.

### 0.2.0.2 Hydration and decision-audit ports

Align position consistency, reconciliation audit, and batch decision audit.

### 0.2.0.3 Order lifecycle ports

Align tracking, dispatch, uncertain reconciliation, ledger, commission, and
execution-event repository signatures.

### 0.2.0.4 Synchronization and coordinator ports

Align Core synchronization audit plus coordinator repository inputs and result
types.

### 0.2.0.5 Acceptance

Run targeted service/integration tests, static checking, formatting, and the full
regression.

## Verification and DoD

- `uv run mypy src/trading_automaton/composition.py` reports zero errors;
- no new `Any`, `cast`, `type: ignore`, or weakened DTO fields are introduced to
  satisfy composition;
- existing service and integration tests retain their business inputs and
  assertions;
- targeted hydration, tracking, dispatch, reconciliation, synchronization,
  coordinator, composition, and batch tests pass;
- Ruff and Black pass for touched paths;
- `uv run python -m pytest -q` reports zero failures;
- `git diff --check` passes;
- trading decisions, intent transitions, broker calls, and database writes are
  behaviorally unchanged.

`mypy` is the executable test for structural signature compatibility; pytest is
the executable test for unchanged runtime behavior. No test of file layout or AST
structure is added.
