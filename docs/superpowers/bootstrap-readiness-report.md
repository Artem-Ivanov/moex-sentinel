# Initial bootstrap readiness — implementation acceptance

Implemented on 2026-09-08 against the accepted clean-slate bootstrap design.
Readiness amended on 2026-09-12 to separate position accounting from permission
to submit new orders. Historical verification below describes the original
2026-09-08 implementation; the current change has separate evidence below.

## Current accounting readiness — 2026-09-12

An unfinished authoritative bootstrap (`last_sequence_number=0`) remains part
of the Worker command in either HOLD or IN_QUEUE. Resuming before bootstrap
completion must not discard the existing broker-position snapshot. Core accepts
the complete cycle/lot/audit/activation quartet from either pending state, with
the same exact snapshot, sequence, revision and replay checks. Completed
bootstrap metadata does not re-enable bootstrap after resume; ordinary runtime
HOLD and terminal CLOSED retain their lifecycle guards.

Worker obtains a fresh portfolio response and checks quantity, average price
and currency against the authoritative snapshot before adoption. Quote, candle,
cash and commission readiness belong to subsequent trading preparation and do
not block recording an already existing position. Quartet delivery and ACK
still precede normal decisions; bootstrap creates no broker intent, order or
execution. IN_WORK means the position has been adopted, not that its market is
currently open. Analytics outages, stale/invalid books and unavailable API
trading continue to prevent market ticks and new orders after ACK.

A legacy RECONCILED lot can be replaced atomically by the authoritative
bootstrap lot only when it is the sole lot, its original and remaining quantity
and entry price match the snapshot, its commission is zero and no trade intent
exists. Executed, partially consumed or conflicting ledgers are not replaced.
Rollback preserves the previous lot if quartet persistence fails. A previously
rejected speculative activation uses the existing Core reconciliation and
re-claim path before the corrected quartet is created.

Evidence: isolated Core/Worker SQLite tests cover legacy IN_WORK/revision3/
sequence1 recovery through rejection, re-claim, quartet and ACK; matching-lot
adoption and rollback; HOLD/IN_QUEUE routing; and real Analytics runtime with
unavailable, stale, closed and crossed market data, with no tick/order/execution.
The selected eight-file suite passed **114 tests**. RED evidence is recorded in
`develop/reports/trading-audit-2026-09-12/bootstrap-fix-red.txt`,
`bootstrap-runtime-fix-red.txt` and `bootstrap-accounting-fix-red.txt`; GREEN in
`bootstrap-fix-green.txt` in that same directory.

## Original changes — 2026-09-08 (historical)

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
