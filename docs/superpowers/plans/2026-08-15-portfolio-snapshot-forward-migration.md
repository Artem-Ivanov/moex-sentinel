# Portfolio Snapshot Forward Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the existing PostgreSQL volume from the deployed portfolio snapshot schema to the run-based schema without switching volumes or losing data.

**Architecture:** Keep `0001_baseline` faithful to the deployed schema and introduce a transactional `0002_portfolio_snapshot_runs` migration. Guard destructive shape replacement with explicit empty-table checks, verify on isolated PostgreSQL, and only then run the same Alembic upgrade against the current Compose database.

**Tech Stack:** Python 3.13, Alembic, SQLAlchemy 2, PostgreSQL 16, pytest, Docker Compose

## Global Constraints

- Do not switch, recreate, or delete the current Docker database volume.
- Abort rather than infer historical data when the old snapshot table is non-empty.
- Do not read or print database rows or connection secrets during verification.
- Upgrade and downgrade schema changes must be transactional on PostgreSQL.

---

### Task 1: Encode the deployed baseline and failing upgrade tests

**Files:**
- Modify: `alembic/versions/0001_baseline.py`
- Modify: `tests/migrations/test_baseline_schema.py`
- Create: `tests/integration/postgresql/test_portfolio_snapshot_schema_migration.py`

**Interfaces:**
- Consumes: Alembic revision `0001_baseline` and `Base.metadata`.
- Produces: a reproducible old schema and tests that require head revision `0002_portfolio_snapshot_runs`.

- [ ] **Step 1: Restore the old snapshot DDL in `0001_baseline.py`**

  Define `portfolio_snapshots` with `user_broker_id`, `total_value`,
  `free_cash`, `realized_pnl`, `unrealized_pnl`, `net_pnl`, `currency`,
  `captured_at`, `created_at`, and `id`, matching the reflected deployed schema.

- [ ] **Step 2: Write a PostgreSQL upgrade test**

  Upgrade an isolated database to revision `0001_baseline`, assert the old
  columns, upgrade to `head`, and assert the run table, new snapshot columns,
  constraints, indexes, metadata parity, and new revision.

- [ ] **Step 3: Write a non-empty-table guard test**

  Insert a synthetically generated broker and old snapshot row at revision
  `0001_baseline`, then assert that upgrading to head raises the migration's
  data-preservation error and leaves revision/schema unchanged.

- [ ] **Step 4: Run tests and verify RED**

  Run: `uv run pytest -q tests/migrations/test_baseline_schema.py tests/integration/postgresql/test_portfolio_snapshot_schema_migration.py`

  Expected: failure because revision `0002_portfolio_snapshot_runs` does not yet exist.

### Task 2: Implement the forward migration

**Files:**
- Create: `alembic/versions/0002_portfolio_snapshot_runs.py`
- Modify: `tests/test_schema_revision.py`

**Interfaces:**
- Consumes: the exact old `portfolio_snapshots` schema from revision `0001_baseline`.
- Produces: Alembic revision `0002_portfolio_snapshot_runs`, matching `Base.metadata` at head.

- [ ] **Step 1: Add the empty-table guards**

  Use the current Alembic connection to count old/new snapshot rows. Raise
  `RuntimeError` with an actionable message before any destructive DDL when a
  guarded table contains data.

- [ ] **Step 2: Add upgrade DDL**

  Drop the empty old table, create `portfolio_snapshot_runs`, then create the
  replacement `portfolio_snapshots` table with the same columns, constraints,
  foreign keys, and indexes as `Base.metadata`.

- [ ] **Step 3: Add guarded downgrade DDL**

  Reject downgrade when either new table contains rows. Otherwise drop both new
  tables and recreate the old snapshot table exactly.

- [ ] **Step 4: Update exact revision expectations**

  Replace assertions of `0001_baseline` as head with
  `0002_portfolio_snapshot_runs`, while preserving tests that explicitly target
  the baseline revision.

- [ ] **Step 5: Run focused tests and verify GREEN**

  Run the migration unit and PostgreSQL integration tests; all must pass.

### Task 3: Verify and apply to the existing volume

**Files:**
- No source files.

**Interfaces:**
- Consumes: tested Alembic head `0002_portfolio_snapshot_runs`.
- Produces: the current Compose database at the new revision with healthy services.

- [ ] **Step 1: Run quality checks**

  Run Ruff, Black check, migration tests, the full backend suite, and frontend
  tests/build. Confirm every command exits successfully.

- [ ] **Step 2: Reconfirm the live precondition**

  Query only the Alembic revision and `count(*)` from `portfolio_snapshots`.
  Require revision `0001_baseline` and zero rows.

- [ ] **Step 3: Build and run the Compose migrations service**

  Build the migrations image so it contains `0002`, then run Alembic upgrade to
  head against the existing Compose database. Do not run `down`, remove volumes,
  or change volume environment variables.

- [ ] **Step 4: Verify schema and services**

  Confirm revision `0002_portfolio_snapshot_runs`, required tables/columns,
  healthy backend, running snapshot worker, and a successful local
  `/api/trading/summary` response.

- [ ] **Step 5: Report the exact outcome**

  Report the applied revision, preserved volume, test results, and any remaining
  operational caveat without exposing connection secrets or row contents.
