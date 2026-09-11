# Standard-ingress Worker pending-tail replay plan

**Goal:** append only the pending production Worker tail to legacy Core tables
through the same input used by the running Worker.

```text
Worker SQLite outbox_events (read-only)
  -> AutomationEvent
  -> count-limited chunks
  -> POST /internal/automaton/events
  -> existing Core service and repositories
```

The replay process does not open the Core database, merge ORM rows, reconstruct
positions, or implement a second set of business rules. Core performs normal
sequence, revision and idempotency checks. Any failure or incomplete per-
automation acknowledgement stops replay. Worker SQLite is never updated.

The prior database-transfer stage copied accepted legacy Core projections into
PostgreSQL. It did not copy the complete Worker-owned decision, intent,
execution, lot, allocation and audit history into normalized v2 facts. ACKed
legacy outbox rows are deleted by normal Worker operation. Therefore this tool
is only a pending-tail replay and does not complete milestone `0.2.1.4`.
Event IDs, sequences, revisions, timestamps and metadata are sent unchanged.

- default command is dry-run;
- `--apply` publishes with `--chunk-size` (default `100`);
- Compose profile `history-replay` mounts Worker data read-only;
- stop the production Worker before `--apply` and keep it stopped through the
  maintenance decision;
- complete Worker-history-to-v2 migration remains `0.2.1.4`;
- repository/API cutover `0.2.1.5` remains blocked;
- legacy cleanup remains `0.2.1.6` and requires separate approval;
- no Git commit is created.
