# Portfolio snapshot and migration fix report

## Scope

This change closes the snapshot collection and schema migration correctness gaps found while verifying the trading summary milestone. It does not change rolling-window baseline selection: the service still uses the nearest common snapshot at or before the requested boundary, and the API's `from` value remains the authoritative start of the discrete approximation.

## Snapshot collection

- Collection now uses one stable PostgreSQL advisory lock for the whole collector. Runs in adjacent minute buckets cannot overlap and build cumulative P&L from the same predecessor.
- A run checks its exact bucket before broker reads. A clock rollback into an existing bucket returns that run; a rollback into an unseen bucket older than the latest run returns the latest run. Neither case writes another snapshot.
- Each account portfolio read is bracketed by local start and completion timestamps. Operations are read through the completion timestamp. If an executed deposit or withdrawal occurred after the run timestamp and no later than account-read completion, that account is rejected with a safe error. This check also applies to the first snapshot so a transfer cannot be embedded in its initial value and subtracted during the next run.
- Persisted monetary values are checked before the atomic run write. Total value and free cash must be finite, non-negative, within `NUMERIC(28, 9)` range, and exactly representable at scale 9. Cumulative P&L has the same finite/range/scale checks and may be negative. An invalid account is skipped without rolling back valid accounts.
- When the repository reports that another writer already owns a run bucket, the collector returns the persisted winner with `saved=0` and `skipped=true`.

The broker API does not expose an authoritative portfolio snapshot timestamp. Bracketing rejects cash movements visible through `get_operations` during the uncertain interval, but it cannot prove absolute consistency when the broker delays operation visibility beyond the account read. A stronger guarantee would require a broker-provided snapshot timestamp or a separately specified delayed-finalization protocol.

## Schema migration

- PostgreSQL upgrade takes `ACCESS EXCLUSIVE` on `portfolio_snapshots` before checking that it is empty.
- PostgreSQL downgrade locks `portfolio_snapshots` and `portfolio_snapshot_runs` together, in that fixed order, before either empty check.
- SQLite keeps the existing migration path because its locking syntax differs and its test/development migration runs are single-process.
- The migration command escapes percent characters before placing the configured URL into Alembic's `ConfigParser`, while Alembic still receives the original URL after interpolation.

## Verification scenarios

- first and follow-up snapshots reject a cash movement after the shared run timestamp;
- invalid amount, storage overflow, and excess scale in one account preserve a valid account in the same run;
- existing and unseen stale buckets return persisted runs without rewriting history;
- different minute buckets contend on the same PostgreSQL advisory lock;
- concurrent inserts during upgrade and downgrade are observed by the empty-table guards instead of being dropped;
- PostgreSQL head migration preserves the expected columns, checks, unique constraints, and indexes;
- an empty PostgreSQL schema supports downgrade to `0001_baseline` and upgrade back to head;
- a percent-encoded database URL reaches Alembic unchanged.
