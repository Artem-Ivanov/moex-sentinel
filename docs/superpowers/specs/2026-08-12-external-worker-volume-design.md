# External Worker Recovery Volume Design

## Goal

Make the existing Worker recovery SQLite storage independent from the Compose
project lifecycle and application-image rebuilds without changing its data,
schema, trading decisions or runtime interfaces.

## Accepted design

Reuse the existing Docker volume `moex-sentinel_automaton-data`. Compose keeps
the logical mount name `automaton-data`, declares it external and resolves it to
the stable physical name through
`AUTOMATON_VOLUME_NAME`, with the current volume name as the safe default.

No database copy is required: the physical source and target are the same
volume. Before the Compose transition, only Worker is stopped, SQLite WAL is
checkpointed, database integrity is checked, and a file-level backup is placed
outside the Docker volume.

## Runtime boundary

- Worker continues to use `sqlite:////app/data/trading_automaton.db`.
- The volume contains only Worker recovery, intent, outbox and cached command
  state; hot market data remains in memory.
- Worker receives no PostgreSQL DSN and has no authoritative Core database
  adapter.
- Core PostgreSQL and the legacy Core SQLite volume are not changed.
- Recreating Worker or rebuilding its image must not create, replace or delete
  the external recovery volume.

## Acceptance

1. Parsed Compose configuration exposes `automaton-data` as an external volume
   with a stable configurable physical name.
2. SQLite reports successful checkpoint and integrity checks before recreation.
3. A file-level backup exists outside the volume before recreation.
4. Only Worker is recreated, and its mount resolves to the original physical
   volume.
5. Durable aggregate state is unchanged across recreation; hot state is allowed
   to hydrate again.
6. Worker storage isolation and complete Python regression remain green without
   weakening trading test conditions.

## Rollback

Stop Worker, restore the previous Compose declaration, and recreate only Worker.
The external declaration does not modify the volume contents, so the same
physical volume remains available. The file-level backup is an additional
recovery artifact and is not restored unless integrity verification fails.
