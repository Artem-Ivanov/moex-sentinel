# Initial bootstrap readiness — implementation acceptance

Implemented on 2026-09-08 against the accepted clean-slate bootstrap design.

## Changes

- `src/trading_automaton/services/streaming_runtime_coordinator.py`: claimed
  bootstrap commands no longer generate activation facts immediately. Persisted
  bootstrap HOLD commands join existing broker bundles even after restart with
  no new claims. Runtime state is gated by authoritative Core status: HOLD
  stays HOLD even when the local pending bootstrap group has speculatively set
  IN_WORK; initial CLOSED without an active intent, missing and other unapproved
  statuses are excluded. An already adopted CLOSED automation with an existing
  active/uncertain intent remains assigned as CLOSED for normal reconciliation,
  preserving recovery supervision without enabling market ticks.
- `src/trading_automaton/services/broker_tick_preparation.py`: bootstrap HOLD
  commands use a separate preparation branch. It requires fresh matching
  portfolio quantity/average/currency, a finite positive non-crossed consistent
  order book, available API limit-order trading, successful candle/cash loading
  and a current commission schedule. Market freshness is rechecked immediately
  before ensure with the same two-second limit used by the existing scheduler.
  Missing prerequisites, broker errors and bootstrap validation errors leave
  HOLD without activation. Normal hydration and uncertain-intent reconciliation
  never run for bootstrap HOLD, preventing premature recovery rows, lots and
  audit facts. Already active commands retain their existing preparation path.
- `src/trading_automaton/composition.py`: the existing PositionBootstrapService
  is injected into broker preparation instead of the coordinator.
- Corresponding coordinator, preparation and composition test files cover
  restart, readiness failures, active peers, delayed ACK, closed/missing Core
  records, finite price validation and real SQLite bootstrap/recovery behavior.

No repository, ingress, broker-runtime, batch-runtime or wire-contract changes
were made by this task. No commit/index operation or live broker API call ran.

## Verification

TDD RED: initial readiness/coordinator cases produced 12 failures; speculative
local IN_WORK before ACK produced one failure; CLOSED/missing/IN_QUEUE cases
produced six failures; non-finite order-book price reproduced InvalidOperation.
The real SQLite bootstrap ACK followed by an UNCERTAIN intent and Core CLOSED
reproduced loss of the recovery bundle, then passed with the CLOSED command
preserved and no additional intent created.
Each set passed after its scoped production correction.

Final normal project invocation:

```bash
.venv/bin/pytest -q \
  tests/trading_automaton/services/test_broker_tick_preparation_service.py \
  tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py \
  tests/trading_automaton/test_streaming_composition.py \
  tests/trading_automaton/services/test_broker_runtime_service.py \
  tests/trading_automaton/services/test_position_state_hydration_service.py --tb=short
```

Result: **49 passed**, 16 existing SDK deprecation warnings, exit 0.
The real SQLite test verifies exactly one BROKER_POSITION_BOOTSTRAP lot, an
unchanged four-fact sequence across retries, no broker intent and unavailable
hydrated trading state until fact acknowledgement.

Ruff passes for all six changed Python files. Black check reports all six
unchanged. The environment has no `.venv/bin/mypy`; no dependencies were added
for that optional check. Full-project and PostgreSQL acceptance are owned by
the root task.

## Boundaries

The two-second order-book limit is currently a scheduler constructor default,
not a shared configuration constant. This task uses the same limit without
changing active-automation scheduling. Recovery ledger validation and atomic
fact creation remain in PositionBootstrapService/repository; Core ACK remains
the authority for entering trading.
