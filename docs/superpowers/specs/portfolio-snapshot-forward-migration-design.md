# Portfolio Snapshot Forward Migration Design

## Context

The deployed PostgreSQL volume is stamped at `0001_baseline`. Its
`portfolio_snapshots` table uses the previous account-summary shape, contains
zero rows, and `portfolio_snapshot_runs` does not exist. The source tree already
models the replacement run/snapshot schema, but the checked-in baseline was
edited in place and therefore cannot upgrade the deployed database.

## Decision

Restore the portfolio snapshot part of `0001_baseline` to the schema that was
originally deployed and add `0002_portfolio_snapshot_runs` as a transactional
forward migration. The migration requires the old snapshot table to be empty,
drops and recreates it in the new shape, and creates
`portfolio_snapshot_runs`. It does not switch, recreate, or delete the existing
Docker volume.

The empty-table precondition is intentional. The old and new rows have
different semantics, and guessing `account_id`, run grouping, and minute-level
deduplication for historical rows could silently corrupt reporting. A database
with old rows must stop with an actionable error rather than discard or invent
data.

## Schema Transition

`0001_baseline` retains the previous `portfolio_snapshots` columns:
`user_broker_id`, valuation and P&L totals, `currency`, timestamps, and `id`.

`0002_portfolio_snapshot_runs` performs these operations in one transaction:

1. Count rows in the old `portfolio_snapshots` table and abort if the count is
   non-zero.
2. Drop the old `portfolio_snapshots` table.
3. Create `portfolio_snapshot_runs` with a unique minute bucket and captured-at
   index.
4. Create the new `portfolio_snapshots` table with `run_id`, `account_id`,
   cumulative P&L, minute bucket, foreign keys, checks, uniqueness, and indexes.

Downgrade is allowed only while both new tables are empty. It then recreates the
old table exactly; otherwise it aborts to prevent data loss.

## Verification

An automated migration test first creates the deployed `0001` shape and
asserts that upgrading to head produces schema parity with SQLAlchemy metadata
and revision `0002_portfolio_snapshot_runs`. A second test verifies the
non-empty-table safety guard. Existing fresh-database parity tests must expect
revision `0002_portfolio_snapshot_runs`.

Before touching the deployed volume, run the migration tests against an
isolated PostgreSQL database. Then apply Alembic head through the Compose
migrations service, confirm the revision and reflected columns, start the
snapshot worker, and check backend health and the trading-summary endpoint.
