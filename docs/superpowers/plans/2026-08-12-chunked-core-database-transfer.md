# Chunked Core Database Transfer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace whole-database in-memory transfer with restartable 5,000-row chunk transfer and streaming verification.

**Architecture:** Source and target adapters expose deterministic table/chunk operations. The usecase coordinates schema validation, chunk comparison, one-transaction-per-chunk writes and streaming final verification without retaining a complete database snapshot.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy 2, SQLite, PostgreSQL 16, Alembic, pytest, UV.

## Global Constraints

- Core, frontend and trading automaton remain stopped until real transfer verification succeeds.
- Default chunk size is 5,000; tests use smaller explicit values.
- No row values, paths, DSNs or connection fields may be emitted by the CLI.
- Existing raw and working SQLite snapshots and legacy Docker volume remain unchanged.
- Tests may be extended for the new interface but their existing DoD semantics must not be weakened.
- No commits are created.

---

### Task 1: Chunk contracts and deterministic source pagination

**Files:**
- Modify: `src/moex_sentinel/domain/database_transfer.py`
- Modify: `src/moex_sentinel/storage/repositories/database_transfer.py`
- Modify: `tests/storage/test_database_transfer_repository.py`

**Interfaces:**
- Produce `DatabaseTableDescriptor`, `DatabaseRowChunk` Pydantic DTOs.
- Produce repository methods `describe_tables(table_names)`, `count_rows(table_name)` and `iter_chunks(table_name, chunk_size)`.

- [x] Write tests with more rows than the test chunk size and composite-key fixtures; assert stable complete coverage, no duplicates and chunks no larger than requested.
- [x] Run `uv run pytest tests/storage/test_database_transfer_repository.py -q` and verify RED because chunk contracts/methods do not exist.
- [x] Implement keyset pagination ordered by the complete primary key and reject a table without a primary key using a typed structural error.
- [x] Run the focused repository tests and verify GREEN.

### Task 2: Per-chunk comparison and transactional insert

**Files:**
- Modify: `src/moex_sentinel/services/database_transfer.py`
- Modify: `src/moex_sentinel/storage/repositories/database_transfer.py`
- Modify: `tests/services/test_database_transfer_service.py`
- Modify: `tests/storage/test_database_transfer_repository.py`

**Interfaces:**
- Produce `compare_chunk(source_chunk, target_rows)` returning rows to insert and safe issues.
- Produce `read_rows_by_keys(table_name, keys)` and `insert_chunk(chunk)`.

- [x] Write RED tests for absent, identical and conflicting target rows and for rollback of one failing chunk.
- [x] Implement comparison without rendering values and one transaction per inserted chunk.
- [x] Verify focused service/repository tests GREEN.

### Task 3: Restartable usecase and streaming verification

**Files:**
- Modify: `src/moex_sentinel/usecases/database_transfer.py`
- Modify: `src/moex_sentinel/domain/database_transfer.py`
- Modify: `tests/usecases/test_database_transfer_usecase.py`

**Interfaces:**
- `MigrateCoreDatabaseUsecase.execute(*, apply: bool, chunk_size: int = 5000) -> DatabaseTransferReport`.
- Ports expose descriptors, counts, chunk iteration, key reads, inserts and sequence reset.

- [x] Write RED tests proving dry-run never writes, apply commits each chunk, retry skips committed rows, conflicts stop, target-only rows fail verification and sequences reset only after clean verification.
- [x] Implement table-by-table orchestration and streaming exact verification with only one source/target chunk resident at once.
- [x] Verify focused usecase tests GREEN.

### Task 4: CLI and PostgreSQL integration acceptance

**Files:**
- Modify: `src/moex_sentinel/migrations/database_transfer.py`
- Modify: `tests/test_database_transfer_cli.py`
- Modify: `tests/integration/postgresql/test_core_data_transfer.py`

**Interfaces:**
- Add optional CLI `--chunk-size` with default `5000` and positive integer validation.
- Preserve dry-run-by-default and stable safe JSON report/error contracts.

- [x] Write RED CLI and real PostgreSQL tests using chunk size `2`, including idempotent rerun and target-only/conflict detection.
- [x] Wire chunk repositories/usecase and positive chunk-size validation.
- [x] Run focused CLI and PostgreSQL acceptance GREEN.

### Task 5: Regression and real offline transfer completion

**Files:**
- Modify: `docs/superpowers/plans/2026-08-10-decoupled-database-runtime.md`
- Modify: this plan with exact results.

- [x] Run Ruff and Black over `src tests`, then complete Python regression without changing DoD semantics.
- [x] Rebuild the migration image.
- [x] Run real dry-run, apply/resume and second idempotent apply against the current working snapshot.
- [x] Verify revisions, all per-table counts and streaming exact comparison; verify raw snapshot checksum is unchanged.
- [x] Build/start backend on PostgreSQL, verify health and read-only endpoints, then start frontend and Worker in that order.
- [x] Run real PostgreSQL integration tests and final project regression.
- [x] Record exact results, rollback locations and remaining skipped external tests; do not commit.

## Completion checkpoint — 2026-08-12

- Focused chunk unit acceptance: `22 passed`.
- Real isolated PostgreSQL transfer acceptance with chunk size `2`: `1 passed`.
- Real 1.8 GB snapshot apply/resume and independent dry-run both report
  `clean=true`, identical source/target counts, zero inserted rows on rerun and
  no issues. The two large tables contain `354066` and `2720195` rows.
- Raw rollback snapshot SHA-256 remains
  `aacd7f8cc0aa393b0853096209b8d8a47af11d1c242f6cdc920910634caccd72`.
- Core health reports `database=ok` and `schema=compatible`; Core has no legacy
  SQLite mount. Frontend, Core, PostgreSQL and Worker are running.
- Worker outbox is empty and all six automation states survived migration.
- External broker calls return `UNAUTHENTICATED` because the configured token
  is intentionally a placeholder; this is outside database migration DoD.
- Ruff and Black are clean. Python regression: `620 passed, 4 skipped`.
  Frontend: `74 passed`; production build exits `0`.
- Rollback assets: Docker volume `moex-sentinel_sentinel-data`, immutable raw
  snapshot `/private/tmp/moex-sentinel-core-cutover.fjbTLJ/core-raw.sqlite`, and
  upgraded working snapshot beside it. No commit was created.
