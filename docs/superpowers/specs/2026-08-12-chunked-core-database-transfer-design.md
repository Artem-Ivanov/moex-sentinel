# Chunked Core Database Transfer Design

## Context

The offline Core SQLite snapshot is approximately 1.8 GB and contains more
than three million audit/event rows. The current transfer loads the complete
source and target into memory. The initial apply committed all rows, but the
post-apply full-snapshot verification exhausted the transfer container memory
and exited with code 137.

Core, frontend and trading automaton remain stopped. The immutable raw SQLite
snapshot and its Alembic-upgraded working copy remain available for rollback
and transfer. PostgreSQL contains the rows committed by the first apply.

## Accepted architecture

Migration processes one table at a time and one deterministic chunk at a time.
The default chunk size is 5,000 rows. Rows are ordered by the complete primary
key; tables without a primary key are rejected with a safe structural issue.
The migration never loads an entire large table or database into memory.

The source repository exposes table metadata, counts and a keyset-paginated
chunk iterator. The PostgreSQL repository reads only rows for the source keys
in the current chunk. A service compares detached chunk DTOs and returns rows
to insert or safe conflict descriptors. The writer inserts one clean chunk in
one transaction and commits it before advancing.

## Restart and consistency rules

Every chunk is idempotent:

- an absent primary key is inserted;
- an existing identical row is skipped;
- an existing different row produces `TARGET_ROW_CONFLICT` and stops transfer;
- a target-only row is detected during final streaming verification;
- an interrupted run restarts from the first source chunk and cheaply skips
  identical rows until it reaches unfinished data.

Chunk commits intentionally replace whole-database atomicity. Safety comes from
the offline immutable source, deterministic ordering, exact row comparison,
idempotent restart and final verification. Core and Worker cannot start until
verification succeeds.

## Verification

Final verification is streaming and table-scoped. For every known table it
checks schema revision, column order, primary key, source/target counts and
exact values for every source primary key in chunks of 5,000. It also detects
target-only keys without materializing all keys in memory. PostgreSQL integer
sequences are advanced only after every table verifies successfully.

The final report preserves the existing safe contract: mode, clean flag,
revisions, per-table counts, inserted counts and issue codes. It never renders
row values, connection values or paths. Progress may contain only table name,
processed count, inserted count and conflict count.

## Failure behavior

A failed chunk rolls back only that chunk. Earlier verified chunks remain and
are accepted on retry. Driver, schema or conflict failures return the existing
stable CLI error boundary; tests exercise typed service/usecase errors without
requiring sensitive output. No destructive cleanup of PostgreSQL is required
for restart.

## Acceptance

- A test dataset larger than one chunk proves deterministic pagination.
- A second run inserts zero rows.
- A failure after one committed chunk resumes without duplication.
- A differing target row blocks the run and preserves its value.
- Target-only rows fail final verification.
- Repository tests prove no query returns more than the configured chunk size.
- PostgreSQL transfer acceptance passes with a small chunk size.
- The real 1.8 GB working snapshot completes without OOM.
- Source/target counts and exact streaming comparison pass.
- Core starts healthy on PostgreSQL before Worker is restarted.

