# Initial position bootstrap readiness

Scope: initial adopted broker positions only. Existing IN_WORK preparation,
typed contracts and the atomic four-fact bootstrap transaction remain intact.

The coordinator sends persisted HOLD commands carrying a bootstrap snapshot to
the existing broker runtime, including after restart with no newly claimed
commands. It does not create bootstrap activation facts itself.

Broker tick preparation treats those commands separately from regular commands.
Before calling PositionBootstrapService.ensure it requires a freshly loaded
broker portfolio matching the adopted quantity, average price and currency;
a consistent, non-crossed, positive order book no older than the existing
two-second trading limit; an available trading status permitting API limit
orders; successful candle-history and cash loading; and a current commission
profile. Recovery/ledger validation and creation remain inside the existing
atomic bootstrap repository transaction.

Bootstrap HOLD commands never enter ordinary uncertain-intent reconciliation
or position hydration: those paths can create recovery rows, reconciled lots
and audit facts before bootstrap. Preparation failure leaves the command HOLD
and creates no activation group. Regular commands in the same bundle continue
through their existing preparation path. Repeated ensure calls use repository
idempotency; no in-memory completion flag is trusted across restart.

After Core ACK and status synchronization produce IN_WORK, ordinary hydration
resumes. BrokerRuntimeService already excludes HOLD from market ticks and
hydration already excludes commands with pending facts.

Files: streaming_runtime_coordinator.py, broker_tick_preparation.py,
composition.py and their existing tests under tests/trading_automaton.

Tests: HOLD survives restart/no-claim iterations; no coordinator activation;
missing/stale/invalid market, absent trading permission, portfolio mismatch,
commission failure and validation failure produce no bootstrap activation;
ready preparation calls ensure and skips normal hydration/reconciliation;
retries are safe; existing IN_WORK commands still prepare alongside HOLD;
composition wires bootstrap into preparation. Existing broker-runtime HOLD
filter and pending-fact hydration regression tests verify the execution gate.
