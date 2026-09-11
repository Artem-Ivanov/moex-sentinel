# Decoupled Database Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move authoritative Core persistence from its container-local SQLite
volume to an independently deployed PostgreSQL 16 runtime while preserving all
existing data and keeping Worker autonomous with in-memory hot state plus a
local durable SQLite recovery store.

**Architecture:** Core remains the only authoritative database client and uses
repository ports backed by SQLAlchemy. Worker never receives a PostgreSQL DSN;
it publishes typed idempotent facts through Core and keeps only operational
recovery data locally. Database schema migration, legacy data transfer and Core
startup have independent container lifecycles.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy 2, Alembic,
`psycopg` 3, PostgreSQL 16 Alpine, SQLite, Docker Compose, pytest, Ruff, Black,
mypy, uv.

## Global Constraints

- No Git commits are created; every task ends with a diff-review checkpoint.
- PostgreSQL Server, initialization state and database files never enter
  `moex-sentinel-python-base`, backend or Worker images.
- `psycopg` is a client dependency and may be installed in the Python base.
- PostgreSQL data uses a pre-created external Docker volume. Application image
  rebuilds never own volume deletion.
- Core is the only runtime process with the authoritative PostgreSQL DSN.
- Worker accepts only `sqlite:///...` for `AUTOMATON_DATABASE_URL`.
- Worker market snapshots, indicators and current calculation inputs remain in
  memory; SQLite contains only recovery/outbox state.
- Core startup never runs Alembic. Schema migration is an explicit one-shot job.
- Existing Core and Worker SQLite files are retained unchanged until migration
  and rollback acceptance are complete.
- Schema rollout follows `expand -> migrate -> contract`; this plan performs
  expand, verified Core data migration and runtime cutover, but no destructive
  contract step.
- Existing test business conditions are not weakened. Interface-only failures
  are updated without changing their DoD; a required DoD change stops the task
  for review.
- No new tests assert only file/folder structure. Docker tests inspect parsed
  Compose behavior or run containers; migration behavior is tested end-to-end,
  not by unit-testing migration source text.
- DTOs introduced by this plan are Pydantic models in the domain package. ORM
  models remain SQLAlchemy infrastructure types.
- Every persisted business or migration record has an explicit creation time.
- Ruff and Black use the project configuration and a 120-character line limit.
- Every task runs this regression gate after its focused tests:

```bash
uv run ruff check --config pyproject.toml --fix .
uv run black --config pyproject.toml .
uv run pytest
uv run mypy
git diff --check
```

Decision recorded on 2026-08-11: the first complete mypy run exposed 306
pre-existing errors in 62 files while focused mypy for Task 1 passed. The user
approved continuing the CRIT PostgreSQL sequence. Subtask `0.4.1.2` was closed
during final Task 8 acceptance without weakening strict settings or reducing
package scope; the final gate reports zero issues in 200 source files.

## Scope decomposition

This plan implements storage-runtime milestone `0.4` and leaves the normalized
trading-fact work in its already approved sequence:

1. this plan: PostgreSQL runtime, explicit schema job, Core legacy lift-and-shift,
   runtime cutover and Worker storage isolation;
2. `0.2.1.2`: normalized Core trading fact tables on PostgreSQL;
3. `0.2.1.3`: typed fact ingress and unified Worker outbox;
4. `0.2.1.4`: Worker trading-history reconciliation into normalized Core facts;
5. `0.2.1.5`: final repository/API cutover and later legacy cleanup.

Worker's existing SQLite recovery database is preserved rather than copied into
PostgreSQL by this plan. Its durable business facts are migrated only after the
target fact tables and ingress contract exist in `0.2.1.2-0.2.1.4`.

## Target file map

### Configuration and database adapters

- Modify `pyproject.toml` and `uv.lock`: add the PostgreSQL client dependency and
  integration marker configuration.
- Modify `src/moex_sentinel/config.py`: validate Core database/pool settings.
- Modify `src/moex_sentinel/storage/database.py`: support PostgreSQL and retain
  SQLite only for tests and legacy migration sources.
- Modify `src/moex_sentinel/storage/types.py`: preserve aware UTC timestamps on
  PostgreSQL and current SQLite round-trip behavior.
- Create `src/moex_sentinel/storage/schema_revision.py`: read and compare the
  applied Alembic revision without mutating the schema.

### Schema execution

- Modify `alembic/env.py`, `alembic.ini` and the latest compatible migration
  definitions: make the existing schema chain executable on SQLite and
  PostgreSQL.
- Create `src/moex_sentinel/migrations/schema.py`: explicit schema-upgrade CLI.
- Modify `src/moex_sentinel/entrypoint.py`: serve HTTP without applying Alembic.
- Create `docker/migrations.Dockerfile`: one-shot schema/data migration runtime.

### Runtime containers

- Modify `compose.yml`: add the pinned upstream PostgreSQL database image and
  migrations service, external database
  volume, PostgreSQL Core DSN and persistent Worker recovery volume.
- Modify `.env.example`: document variable names and safe development template values.
- Modify `tests/test_compose_config.py`: assert parsed service contracts and
  absence of embedded secret values.

### Legacy Core data transfer

- Create `src/moex_sentinel/domain/database_transfer.py`: immutable Pydantic
  snapshot, plan, issue and report DTOs.
- Create `src/moex_sentinel/services/database_transfer.py`: deterministic pure
  comparison/planning logic.
- Create `src/moex_sentinel/storage/repositories/database_transfer.py`: separate
  SQLite source reader and PostgreSQL target reader/writer adapters.
- Create `src/moex_sentinel/usecases/database_transfer.py`: dry-run/apply
  orchestration.
- Create `src/moex_sentinel/migrations/database_transfer.py`: safe CLI that never
  prints row values or connection settings.

### Acceptance and documentation

- Create `tests/integration/postgresql/conftest.py`: externally supplied
  PostgreSQL test connection fixture.
- Create focused PostgreSQL integration tests under
  `tests/integration/postgresql/`.
- Modify `README.md`, `AGENT_BRIEF.md`, `docs/development.md`,
  `docs/phase-0-trading-service-refactor.md` and
  `docs/phase-1-implementation-plan.md`: startup, migration, rollback and remote
  Worker operations.

---

### Task 1 (`0.4.1` CRIT): PostgreSQL-capable Core engine without runtime cutover

#### Subtask `0.4.1.1` (MAJOR): Existing global Ruff gate debt

**Status:** complete; Ruff passes globally and the full regression remains green.

The first mandatory global gate exposed pre-existing Ruff violations outside the
database files. Resolve them as semantic-preserving formatting/import/exception
style changes before accepting Task 1. Do not change assertions, trading
thresholds, decision branches or test DoD. The subtask is complete only when the
same global Ruff command exits successfully and the full regression remains
green.

#### Subtask `0.4.1.2` (MAJOR): Existing full-project mypy gate debt

**Status:** complete during final milestone acceptance; strict mypy reports zero
issues in 200 source files.

The first full-project mypy run checks 191 source files and exposes existing
strict-typing failures outside the database scope. Classify the failures by root
cause and close them without weakening `strict = true`, reducing package scope,
adding broad ignores or changing business behavior. Preserve this as a separate
typed-contract refactor; Task 1 database acceptance may not claim the global
mypy gate until the subtask is complete.

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/moex_sentinel/config.py`
- Modify: `src/moex_sentinel/storage/database.py`
- Modify: `src/moex_sentinel/storage/types.py`
- Modify: `tests/storage/test_database.py`
- Test: `tests/storage/test_postgresql_database.py`

**Interfaces:**

- Produces:

```python
class Settings(BaseSettings):
    database_url: str
    database_pool_size: int
    database_max_overflow: int
    database_pool_timeout_seconds: float

def create_database_engine(
    database_url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout_seconds: float = 5.0,
) -> Engine: ...
```

- Preserves: `create_session_factory()` and `session_scope()` signatures.
- Consumes later: Task 2 schema CLI, Task 4 PostgreSQL repositories and Core
  composition.

- [x] **Step 1: Add failing configuration and engine tests**

Replace the old “reject PostgreSQL” condition with these behavioral cases:

```python
def test_settings_accepts_postgresql_and_pool_configuration() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://db-host/sentinel",
        database_pool_size=7,
        database_max_overflow=3,
        database_pool_timeout_seconds=2.5,
    )

    assert settings.database_pool_size == 7
    assert settings.database_max_overflow == 3
    assert settings.database_pool_timeout_seconds == 2.5


def test_settings_rejects_unsupported_database_dialect() -> None:
    with pytest.raises(ValueError, match="SQLite or PostgreSQL"):
        Settings(database_url="mysql://db-host/sentinel")
```

Add a PostgreSQL engine test using a monkeypatched `create_engine` so the unit
test proves pool settings and `pool_pre_ping=True` are applied without opening a
network connection. Retain the existing real SQLite PRAGMA tests unchanged.

- [x] **Step 2: Run focused tests and verify the expected failure**

Run:

```bash
uv run pytest tests/storage/test_database.py tests/storage/test_postgresql_database.py -q
```

Expected: PostgreSQL acceptance/pool tests fail because Core still rejects every
non-SQLite URL.

- [x] **Step 3: Add the PostgreSQL client and generalize engine creation**

Add this project dependency and refresh the lock:

```toml
"psycopg[binary]>=3.2,<4",
```

Use `sqlalchemy.engine.make_url(database_url).get_backend_name()` to select the
dialect. Only the SQLite branch creates a parent directory and installs PRAGMA
listeners. The PostgreSQL branch calls `create_engine` with:

```python
{
    "pool_pre_ping": True,
    "pool_size": pool_size,
    "max_overflow": max_overflow,
    "pool_timeout": pool_timeout_seconds,
}
```

Reject every backend other than `sqlite` and `postgresql`. Validate pool size as
positive, overflow as non-negative and timeout as positive in `Settings`.

- [x] **Step 4: Make `UTCDateTime` dialect-safe**

Use `DateTime(timezone=True)` as the logical implementation. Continue storing a
naive UTC value for SQLite, but bind and return aware UTC values for PostgreSQL.
Keep the current rejection of naive application datetimes.

- [x] **Step 5: Run focused tests, then the approved regression gate**

Expected: existing SQLite tests remain green; PostgreSQL configuration tests pass
without changing runtime Compose yet.

- [x] **Step 6: Review checkpoint without commit**

Inspect `git diff` and verify Task 1 changed no repository, usecase, Compose or
Worker behavior.

---

### Task 2 (`0.4.2` CRIT): Dialect-safe Alembic and explicit schema job

**Files:**

- Modify: `alembic/env.py`
- Modify: `alembic.ini`
- Modify: `alembic/versions/a0f211000001_add_user_broker_reference_shadow_schema.py`
- Create: `src/moex_sentinel/storage/schema_revision.py`
- Create: `src/moex_sentinel/migrations/schema.py`
- Modify: `src/moex_sentinel/entrypoint.py`
- Modify: `pyproject.toml`
- Test: `tests/storage/test_schema_revision.py`
- Test: `tests/migrations/test_schema_command.py`
- Modify: `tests/test_compose_config.py`

**Interfaces:**

- Produces:

```python
def current_schema_revision(engine: Engine) -> str | None: ...
def expected_schema_revision(config_path: str = "alembic.ini") -> str: ...
def schema_is_compatible(engine: Engine, expected_revision: str) -> bool: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```

- Console entrypoint: `moex-migrate-schema`.
- Preserves: backend command `python -m moex_sentinel.entrypoint`, now serving
  HTTP only.

- [x] **Step 1: Write failing schema-command and revision tests**

Test that:

1. a database without `alembic_version` returns `None`;
2. the expected head comes from Alembic's script directory;
3. compatibility is true only for the expected revision;
4. `moex_sentinel.migrations.schema.main()` calls `command.upgrade(..., "head")`
   and returns `0`;
5. a migration exception returns `1` with a stable safe error code and does not
   echo the database URL;
6. calling `entrypoint.main()` starts Uvicorn without invoking any schema
   mutation.

- [x] **Step 2: Run focused tests and verify failure**

```bash
uv run pytest tests/storage/test_schema_revision.py tests/migrations/test_schema_command.py tests/test_compose_config.py -q
```

Expected: new symbols and console command are absent; entrypoint still performs
an implicit upgrade.

- [x] **Step 3: Make Alembic select behavior by bound dialect**

In online mode, execute PRAGMA statements and set `render_as_batch=True` only
when `connection.dialect.name == "sqlite"`. PostgreSQL receives no PRAGMA and
uses ordinary ALTER operations. In offline mode derive the same flag from the
configured URL.

Add `postgresql_where=sa.text("external_account_id IS NOT NULL")` to the latest
partial unique index while retaining `sqlite_where`. Do not create a new revision
for this portability correction because PostgreSQL has not yet accepted this
revision and existing SQLite structure is unchanged.

- [x] **Step 4: Implement explicit migration and schema-readiness utilities**

`schema.main()` reads `Settings`, assigns its database URL to an Alembic
`Config`, runs `upgrade(head)` and emits only one of these safe JSON results:

```json
{"status":"SCHEMA_UPGRADED"}
{"error":"SCHEMA_MIGRATION_FAILED"}
```

Remove all Alembic imports and upgrade calls from the backend entrypoint. Register
`moex-migrate-schema = "moex_sentinel.migrations.schema:main"`.

- [x] **Step 5: Run focused tests, then the approved regression gate**

Expected: schema behavior passes on SQLite; PostgreSQL execution is accepted in
Task 3 against the real database container.

- [x] **Step 6: Review checkpoint without commit**

Verify startup now has no schema mutation and no existing migration revision ID
or SQLite migration path was removed.

---

### Task 3 (`0.4.3` CRIT): Independent database and migration containers

**Files:**

- Create: `docker/migrations.Dockerfile`
- Modify: `compose.yml`
- Modify: `.env.example`
- Modify: `tests/test_compose_config.py`
- Create: `tests/integration/postgresql/conftest.py`
- Create: `tests/integration/postgresql/test_schema_runtime.py`
- Modify: `pyproject.toml`

**Interfaces:**

- Compose services: `database`, `migrations`, `backend`,
  `trading-automaton`, `frontend`, `python-base`.
- External volume in this task: `postgres-data`. Worker storage remains on the
  existing Compose-managed `automaton-data` volume until Task 6 verifies and
  externalizes it without data loss.
- Migration invocation:

```bash
docker compose --profile migrations run --rm migrations
```

- Test input: `POSTGRES_TEST_DATABASE_URL` supplied by the acceptance shell,
  never hard-coded in test source.

- [x] **Step 1: Replace Compose expectations with failing behavioral assertions**

Parse `compose.yml` and assert:

- `database` uses the pinned `postgres:16-alpine` image and has a healthcheck;
- database data mounts only at `/var/lib/postgresql/data` from an external volume;
- `migrations` uses profile `migrations`, waits for database health and runs
  `moex-migrate-schema`;
- migrations wait for database health, while backend remains on its existing
  SQLite volume until the verified Task 5 cutover;
- Worker has no PostgreSQL URL and retains `AUTOMATON_DATABASE_URL` pointing to
  `/app/data/trading_automaton.db`;
- no Compose value contains a literal credential; environment variable names
  such as `POSTGRES_PASSWORD` are allowed only when their values are `${...}`
  substitutions;
- the database service does not build from the project Python base; the migration
  service uses the Python base and contains no database server.

- [x] **Step 2: Run focused Compose tests and verify failure**

```bash
uv run pytest tests/test_compose_config.py -q
```

Expected: services and external volume contracts are absent.

- [x] **Step 3: Add the database and migration images to staged Compose topology**

`docker/migrations.Dockerfile` copies `pyproject.toml`, `alembic.ini`, `alembic/`
and `src/`, installs the project without dependency resolution and runs as the
existing non-root application user:

```dockerfile
CMD ["moex-migrate-schema"]
```

Define an external PostgreSQL volume with a configurable stable name. Preserve
the old Core SQLite volume declaration without deleting it so it remains
available to Task 5's transfer job and rollback. Convert Worker storage to a
stable external volume only after its existing file has been copied and verified
in Task 6. Do not change backend `DATABASE_URL` or its legacy volume in this
task; PostgreSQL becomes authoritative only in Task 5 after transfer acceptance.

- [x] **Step 4: Add a real PostgreSQL schema acceptance test**

The integration test obtains `POSTGRES_TEST_DATABASE_URL`, connects with
`create_database_engine`, and asserts:

```python
assert current_schema_revision(engine) == expected_schema_revision()
assert schema_is_compatible(engine, expected_schema_revision())
```

It also inserts and reads one timezone-aware UTC timestamp through
`UTCDateTime`. Mark the test `postgresql` and skip it with an explicit reason only
when the external URL is not supplied.

- [x] **Step 5: Build and run database acceptance**

Create the external volume explicitly, pull the pinned PostgreSQL image, build
the Python base and migrations images, start only `database`, execute the
migration job, and run the PostgreSQL-marked test with the local test DSN
supplied from the environment.

Expected: migration exits `0`; PostgreSQL health is healthy; revision equals
Alembic head; timestamp round-trip is aware UTC.

- [x] **Step 6: Run the global regression gate**

Expected: unit regression may skip the externally configured PostgreSQL test;
the explicit acceptance run in Step 5 must pass and its result must be recorded
in the task checkpoint.

- [x] **Step 7: Review checkpoint without commit**

Use `docker inspect`/Compose configuration to verify application layers contain
no PostgreSQL data path and recreate an application container without touching
the external database volume.

Checkpoint recorded on 2026-08-11: the real PostgreSQL migration initially
exposed a dialect-specific legacy foreign-key name in revision
`e4f82b6c1a30`. The existing revision now resolves that legacy constraint from
database metadata for PostgreSQL while preserving SQLite batch naming. The
migration job reaches Alembic head, the explicit PostgreSQL integration test
passes before and after a database-container restart, focused tests report
`22 passed`, focused mypy reports zero issues, and the full regression reports
`589 passed, 1 skipped` (the external PostgreSQL test is the explicit skip).
The Core backend still uses its legacy SQLite volume by design until Task 5.

---

### Task 4 (`0.4.4` CRIT): Deterministic Core SQLite-to-PostgreSQL transfer

**Files:**

- Create: `src/moex_sentinel/domain/database_transfer.py`
- Create: `src/moex_sentinel/services/database_transfer.py`
- Create: `src/moex_sentinel/storage/repositories/database_transfer.py`
- Create: `src/moex_sentinel/usecases/database_transfer.py`
- Create: `src/moex_sentinel/migrations/database_transfer.py`
- Modify: `pyproject.toml`
- Test: `tests/services/test_database_transfer_service.py`
- Test: `tests/usecases/test_database_transfer_usecase.py`
- Test: `tests/storage/test_database_transfer_repository.py`
- Test: `tests/test_database_transfer_cli.py`

**Interfaces:**

- Domain DTOs:

```python
class DatabaseTableSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    table_name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]


class CoreDatabaseSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_revision: str
    captured_at: datetime
    tables: tuple[DatabaseTableSnapshot, ...]


class DatabaseTransferIssue(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str
    table_name: str
    row_key: tuple[str, ...] = ()


class DatabaseTransferPlan(BaseModel):
    model_config = ConfigDict(frozen=True)
    rows_to_insert: tuple[DatabaseTableSnapshot, ...]
    source_counts: dict[str, int]
    target_counts_before: dict[str, int]


class DatabaseTransferReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    mode: Literal["DRY_RUN", "APPLY"]
    clean: bool
    source_revision: str
    target_revision: str
    source_counts: dict[str, int]
    target_counts: dict[str, int]
    inserted_counts: dict[str, int]
    issues: tuple[DatabaseTransferIssue, ...]
```

- Ports:

```python
class DatabaseSnapshotReaderPort(Protocol):
    def read_snapshot(self, table_names: tuple[str, ...]) -> CoreDatabaseSnapshot: ...


class DatabaseSnapshotWriterPort(Protocol):
    def apply(self, plan: DatabaseTransferPlan, *, migrated_at: datetime) -> None: ...


class MigrateCoreDatabaseUsecase:
    def execute(self, *, apply: bool) -> DatabaseTransferReport: ...
```

- CLI entrypoint: `moex-migrate-core-database`.

- [x] **Step 1: Write pure failing planner tests**

Cover exact cases:

- empty target produces one insert for every source row;
- identical target rows produce zero inserts and a clean idempotent plan;
- the same primary key with different values produces
  `TARGET_ROW_CONFLICT` and blocks apply;
- missing target table produces `TARGET_TABLE_MISSING`;
- source and target revision mismatch produces `SCHEMA_REVISION_MISMATCH`;
- row ordering is deterministic by table dependency order and serialized primary
  key;
- reports contain table names/counts but never row values.

- [x] **Step 2: Run service tests and verify failure**

```bash
uv run pytest tests/services/test_database_transfer_service.py -q
```

Expected: domain models and planner do not exist.

- [x] **Step 3: Implement Pydantic DTOs and pure planning service**

`DatabaseTransferService.plan(source, target, *, mode)` compares only tables
declared in `moex_sentinel.storage.models.Base.metadata.sorted_tables`. It
serializes composite row keys to strings for safe reporting and never copies a
conflicting row.

- [x] **Step 4: Write failing adapter and usecase tests**

Build a temporary SQLite source and target with representative parent/child,
JSON, Decimal and aware timestamp data. Assert source and target use distinct
engines/sessions. Assert apply uses one target transaction and rollback leaves
the target unchanged after any insert failure.

- [x] **Step 5: Implement source/target adapters and usecase**

The source adapter is read-only. The target adapter inserts in
`Base.metadata.sorted_tables` order, never disables foreign keys, and resets
PostgreSQL-owned integer sequences after explicit legacy IDs are copied. The
usecase performs:

```text
source snapshot -> target snapshot -> pure plan -> optional atomic apply
-> target re-read -> verified report
```

- [x] **Step 6: Implement a safe dry-run-by-default CLI**

The CLI accepts a local `--source-sqlite-path` and uses Core `DATABASE_URL` for
the target. It refuses a non-SQLite source or non-PostgreSQL target. `--apply` is
required for writes. Output is stable JSON containing only report fields above;
exceptions emit `CORE_DATABASE_TRANSFER_FAILED` without paths, DSNs or row
values.

- [x] **Step 7: Run focused tests, then the global regression gate**

Expected: dry-run cannot write; apply is atomic; rerun inserts zero rows; conflict
is explicit; all DTOs are Pydantic.

- [x] **Step 8: Review checkpoint without commit**

Verify no source connection begins a write transaction, no source file is
deleted/renamed and no connection setting is logged or rendered.

Checkpoint recorded on 2026-08-11: source snapshots use a dedicated read-only
SQLite connection, target apply uses one transaction, foreign-key failure rolls
back the complete plan, dry-run never calls the writer, and apply is accepted
only after a second target snapshot is identical to the source. Reports contain
only revisions, table counts and safe ordinal conflict references. Focused tests
report `16 passed`, focused mypy reports zero issues, Ruff/Black pass, and the
full regression reports `605 passed, 1 skipped`. No source file operation or
connection value is rendered by the CLI.

---

### Task 5 (`0.4.5` CRIT): PostgreSQL transfer acceptance and Core cutover

**Large-database amendment:** the accepted chunked migration design and TDD
implementation plan are:

- `docs/superpowers/specs/2026-08-12-chunked-core-database-transfer-design.md`
- `docs/superpowers/plans/2026-08-12-chunked-core-database-transfer.md`

The first real apply committed the target rows but its full in-memory
post-apply verification exited with code 137. Core/Worker remain stopped. Task
5 continues through restartable chunk verification; committed identical rows
must be reused rather than deleted or copied again.

**Mandatory maintenance barrier:** the final transfer is an offline cutover,
not a live copy. Before capturing the immutable SQLite source copy, disable new
trading intents, let Worker finish its current iteration, resolve or explicitly
record every in-flight broker order, flush pending Worker facts/audit to Core,
then stop `trading-automaton` followed by `backend`. Run dry-run and apply only
from that read-only copy. Start Core on PostgreSQL and verify health/data before
starting Worker; trading resumes only through the existing manual resume flow.
Core/Worker writes must not run concurrently with the final snapshot or apply.

**Files:**

- Modify: `compose.yml`
- Modify: `.env.example`
- Modify: `src/moex_sentinel/api/app.py`
- Modify: `src/moex_sentinel/views/health.py`
- Modify: `tests/api/test_health.py`
- Modify: `frontend/src/api/health.ts`
- Modify: `frontend/src/api/health.spec.ts`
- Modify: `frontend/src/components/HealthStatus.spec.ts`
- Create: `tests/integration/postgresql/test_core_data_transfer.py`
- Create: `tests/integration/postgresql/test_core_runtime.py`

**Interfaces:**

- Health keeps `/api/health` and adds a non-secret schema result:

```python
class HealthResponse(BaseModel):
    status: Literal["ok", "error"]
    service: Literal["backend"]
    version: str
    database: Literal["ok", "error"]
    schema: Literal["compatible", "incompatible"]
```

- `create_app()` receives an injectable schema checker for unit tests while
  production composition uses `schema_is_compatible`.

- [x] **Step 1: Write failing health and migration acceptance tests**

For PostgreSQL, health must return `503` with `schema="incompatible"` when
connectivity succeeds but the Alembic revision is absent or not current. It must
not reveal the actual revision or database error. SQLite API tests retain their
existing lightweight behavior and report a compatible test/legacy schema after
the connectivity check, so repository/service tests do not require Alembic.

The PostgreSQL transfer test creates a source SQLite database at current head,
inserts a minimal consistent graph spanning configuration, user-broker shadow
reference data, instruments and one legacy automation, then proves:

1. dry-run leaves PostgreSQL empty;
2. apply transfers the graph;
3. every source/target table count matches;
4. foreign keys and timestamp order remain valid;
5. a second apply inserts zero rows;
6. the next PostgreSQL-generated integer key exceeds the migrated maximum.

- [x] **Step 2: Run focused tests and verify failure**

```bash
uv run pytest tests/api/test_health.py -q
uv run pytest -m postgresql tests/integration/postgresql/test_core_data_transfer.py -q
```

Expected: schema status and transfer command are not integrated yet.

- [x] **Step 3: Integrate schema compatibility into readiness**

Keep database connectivity and schema compatibility as separate checks. Core may
start as a process with an incompatible schema, but `/api/health` remains `503`;
therefore frontend and Worker Compose dependencies cannot become healthy before
the explicit migration succeeds. Extend the existing frontend health DTO with
the required `schema` field; do not add client-side business validation.

- [x] **Step 4: Add a read-only legacy volume to the transfer job**

Expose the existing `sentinel-data` volume only to a migration-profile transfer
service at `/legacy:ro`. Do not mount it in backend after cutover. Preserve the
volume declaration and document its resolved pre-cutover name before any Compose
change.

Checkpoint: the existing rollback volume resolves to
`moex-sentinel_sentinel-data`; it remains untouched and is mounted read-only only
by the `transfer` profile. The staged backend configuration has no legacy volume
mount.

- [x] **Step 5: Perform the real staged local transfer**

Before changing Core's runtime DSN:

1. stop writes to the current Core container;
2. make a file-level copy of the SQLite database without deleting the original;
3. record current Alembic revision and safe per-table counts;
4. run transfer dry-run;
5. run transfer apply;
6. run the idempotent apply again;
7. compare source and target counts and relationship checks;
8. switch backend `DATABASE_URL` to PostgreSQL only after every check passes.

No command in this step removes a container volume or SQLite file.

- [x] **Step 6: Verify Core against PostgreSQL**

Start backend and frontend, verify health is `200`, then exercise existing
read-only user-broker, instrument, automation, operations and analytics endpoints.
Expected: responses are semantically unchanged and Core logs contain no DSN.

- [x] **Step 7: Run focused PostgreSQL tests and the global regression gate**

Expected: all Core tests pass; real PostgreSQL schema/transfer/runtime acceptance
passes; old SQLite source remains available for rollback.

- [x] **Step 8: Review checkpoint without commit**

Classify every regression failure as interface adaptation or potential DoD
change. Stop and request review for the latter.

Partial checkpoint recorded on 2026-08-11: health distinguishes connectivity
from schema compatibility, frontend contracts include the schema result, real
transfer/runtime acceptance passes in disposable PostgreSQL schemas, focused
PostgreSQL tests report `4 passed`, frontend reports `65 passed`, and the Python
regression reports `606 passed, 4 skipped`. Updated backend, frontend,
migration and transfer images build successfully. The maintenance barrier was
tested: Worker delivered its final facts/audit and stopped cleanly, then Core
stopped. The live copy/cutover in Step 5 was not executed because the legacy
database may contain a protected secret value that this session must not copy.
The original backend, frontend and Worker containers were restarted without
recreation and are healthy on the unchanged SQLite runtime. Steps 5-8 remain
open; Task 6 must not begin before an accepted live cutover.

Completion checkpoint recorded on 2026-08-12: the PostgreSQL target volume was
recreated, migrated to `a0f211000001`, and the stopped legacy SQLite snapshot
was upgraded on a disposable working copy. Chunked transfer completed exact
streaming verification of every known table without OOM. Independent apply and
dry-run reruns are clean, insert zero rows and report equal source/target
counts. Core runs healthy on PostgreSQL without a legacy volume mount; frontend
and Worker were started only after read-only Core acceptance. Ruff/Black pass,
Python reports `620 passed, 4 skipped`, real isolated PostgreSQL transfer
acceptance reports `1 passed`, frontend reports `74 passed`, and its production
build succeeds. Broker SDK calls remain unauthenticated because the connection
value is intentionally a placeholder; migration and local service contracts
are healthy. No commit was created.

---

### Task 5.1 (`0.4.5.1` MINOR): Editable broker settings UI

**Design:**

- `docs/superpowers/specs/2026-08-11-broker-settings-edit-design.md`

**Implementation plan:**

- `docs/superpowers/plans/2026-08-11-broker-settings-edit.md`

**Scope:**

This is an independent UI checkpoint before live Core cutover resumes. It does
not transfer Core data, recreate a runtime container, change broker
persistence, or alter trading behavior.

- [x] One reusable form serves broker creation and editing.
- [x] Adapter code, provider, environment and contour are immutable in edit mode.
- [x] Mutable settings are sent as one complete replacement through the existing
  `PUT /api/brokers/{broker_id}` contract.
- [x] Core remains the validation owner and frontend displays returned field
  errors without adding business validation.
- [x] A single click selects and highlights a broker row; editing opens only by
  double click, with no separate edit button on the page or row.
- [x] Focused frontend tests, complete frontend suite, production build and
  global Python regression pass.
- [x] Review checkpoint completes without commit, Core cutover or volume
  recreation.

Checkpoint recorded on 2026-08-11: focused form/view/API acceptance reports
`16 passed`; complete frontend acceptance reports `74 passed`; Vue TypeScript
typecheck and the production Vite build exit `0`. Ruff is clean, Black leaves
all `353` Python files unchanged, and the global Python regression reports
`614 passed, 4 skipped`. The four skips are PostgreSQL acceptance tests whose
external `POSTGRES_TEST_DATABASE_URL` is absent. No backend, persistence,
migration, broker adapter, strategy, running container or volume was changed by
this UI checkpoint.

---

### Task 6 (`0.4.6` MAJOR): Worker autonomy and persistent recovery-volume acceptance

**Files:**

- Modify: `compose.yml`
- Modify: `src/trading_automaton/config.py`
- Modify: `tests/trading_automaton/test_config.py`
- Modify: `tests/trading_automaton/test_streaming_composition.py`
- Modify: `tests/trading_automaton/storage/test_worker_database.py`
- Create: `tests/integration/test_worker_storage_isolation.py`

**Interfaces:**

- Preserves `AutomatonSettings.database_url` as SQLite-only.
- Preserves `build_streaming_runtime(settings)` and all strategy behavior.
- Produces no PostgreSQL import, DSN or repository in `trading_automaton`.

- [x] **Step 1: Add failing isolation and recovery tests**

Assert:

- PostgreSQL Worker URLs are rejected;
- runtime composition opens only the supplied local SQLite engine;
- `HotMarketDataCacheService` and position state are not persisted as market
  snapshots in SQLite;
- a pending outbox/order recovery row survives repository disposal and recreation;
- an acknowledged terminal row follows existing compaction rules;
- failure to write durable intent prevents broker dispatch and leads to `HOLD`;
- Worker restart restores recovery/outbox state before a manual resume can trade.

- [x] **Step 2: Run focused Worker tests and inspect current behavior**

```bash
uv run pytest tests/trading_automaton/test_config.py tests/trading_automaton/storage/test_worker_database.py tests/integration/test_worker_storage_isolation.py -q
```

Expected: most runtime isolation behavior may already pass; only missing
persistence/restart guarantees are implemented. Do not refactor strategy logic.

- [x] **Step 3: Close only demonstrated Worker durability gaps**

Keep hot caches unchanged. If a gap exists, add the smallest repository/service
change needed to preserve intent-before-side-effect and recovery-before-resume.
Do not introduce PostgreSQL, Redis, new decisions or changed thresholds.

- [x] **Step 4: Give the existing Worker volume a stable external identity**

Resolve the current Compose-managed `automaton-data` volume. The user-approved
local transition reuses that physical volume rather than copying 2.3 GiB to a
second Docker volume: stop only Worker, checkpoint WAL, verify SQLite integrity,
create a file-level backup outside Docker storage, then declare the same physical
volume external with an explicit configurable name. No volume is deleted or
replaced.

- [x] **Step 5: Recreate only the Worker container and verify persistence**

After recreation, verify pending outbox/recovery rows and cached commands are
restored. In-memory market state must start empty and hydrate normally. Worker
must connect to Core API and broker SDK only, never PostgreSQL.

- [x] **Step 6: Run all Worker tests and the global regression gate**

Expected: strategy/decision tests are unchanged semantically; full regression is
green.

- [x] **Step 7: Review checkpoint without commit**

Confirm no test condition or trading algorithm was weakened to accommodate the
storage transition.

Partial checkpoint recorded on 2026-08-11: the code/test-only portion was
verified independently of the blocked Core cutover. Focused acceptance reports
`37 passed`: Worker configuration and engine reject PostgreSQL, composition
opens only the configured local SQLite file, hot market snapshots are absent
from the schema, pending intent/outbox and restart markers survive engine
disposal, acknowledged outbox records compact without losing authoritative
state, and an unclean restart requires manual resume. A demonstrated streaming
gap was closed without changing strategy behavior: a typed durable-decision
persistence failure prevents SDK dispatch, moves affected automations to
`HOLD`, and terminates that broker runtime. Physical volume Steps 4-5 remain
blocked until Task 5 live Core cutover is accepted; no runtime volume was moved
or recreated in this checkpoint. Worker regression reports `333 passed`; the
global Python gate reports `614 passed, 4 skipped`, with only externally
configured PostgreSQL acceptance tests skipped. Ruff and focused mypy checks
are clean. Step 7 remains open because the physical storage transition has not
yet occurred.

Completion checkpoint recorded on 2026-08-12: the existing physical volume
`moex-sentinel_automaton-data` was retained and is now an explicitly named
external Compose volume. Only Worker was stopped and recreated. Before the
transition, WAL checkpoint returned no pending frames, SQLite integrity was
`ok`, and a file-level backup was created outside Docker storage. Durable
aggregate state is identical before and after recreation: 165 broker intents,
6 cached automations and 0 pending outbox events. The recreated Worker mounts
the same volume at `/app/data`, reports SQLite as its only database backend,
receives no Core/PostgreSQL database environment variable and reconnects to Core
through HTTP. Focused runtime/configuration acceptance reports `17 passed`; the
complete Worker regression reports `337 passed`. Ruff is clean and Black leaves
all 353 Python files unchanged. Broker sandbox calls remain unauthenticated
because the configured token is intentionally a placeholder; this does not
affect storage recovery acceptance. No strategy behavior, test condition,
backend, frontend, PostgreSQL container or data volume was changed, and no commit
was created.

---

### Task 7 (`0.4.7` MAJOR): Deployment, rollback and remote Worker documentation

**Files:**

- Modify: `README.md`
- Modify: `AGENT_BRIEF.md`
- Modify: `docs/development.md`
- Modify: `docs/phase-0-trading-service-refactor.md`
- Modify: `docs/phase-1-implementation-plan.md`
- Test: `tests/test_documentation.py`

**Interfaces:**

- Documents the same commands and environment names implemented by Tasks 1-6.
- Does not introduce a second deployment path or automatic migration behavior.

- [x] **Step 1: Add failing documentation-contract assertions**

Check that project documentation names:

- the external PostgreSQL and Worker volumes;
- the explicit schema migration command;
- Core transfer dry-run/apply/idempotent rerun;
- backup and rollback order;
- schema compatibility health behavior;
- Core-only PostgreSQL access;
- remote Worker requirements and local SQLite recovery responsibility;
- prohibition on `docker compose down -v` in normal operations.

- [x] **Step 2: Run documentation tests and verify failure**

```bash
uv run pytest tests/test_documentation.py -q
```

Expected: existing README/brief still describe SQLite Core and automatic startup
migrations.

- [x] **Step 3: Update operational documentation**

Provide separate sections for:

1. first local PostgreSQL setup;
2. normal application rebuild/restart;
3. explicit schema expand;
4. legacy Core dry-run and apply;
5. rollback to untouched SQLite and previous backend image;
6. PostgreSQL volume backup/restore ownership;
7. deployment with Worker on another host;
8. future `0.2.1.2-0.2.1.5` fact migration sequence.

Mark `0.4.1-0.4.7` complete only after Task 8 acceptance; keep normalized fact
milestones open.

- [x] **Step 4: Run documentation tests and the global regression gate**

Expected: documentation matches actual Compose and commands; no contradictory
statement says Core still uses SQLite in the target runtime.

- [x] **Step 5: Review checkpoint without commit**

Run an incomplete-marker/contradiction scan over the modified documents and
verify no runtime values or connection details were copied into documentation.

Completion checkpoint recorded on 2026-08-12: README, AGENT_BRIEF, development
runbook and phase documents now describe the active Core PostgreSQL runtime,
explicit schema job, restartable chunked legacy transfer, backup/rollback order,
external PostgreSQL and Worker volumes, Core-only PostgreSQL access and remote
Worker recovery responsibility. Normal image rebuild is separated from schema
migration and transfer. The documents explicitly prohibit
`docker compose down -v` in normal operations and keep the normalized fact
sequence `0.2.1.2–0.2.1.5` open after final milestone acceptance. The new
documentation contract passed RED then GREEN; documentation acceptance reports
`3 passed`. Ruff is clean, Black leaves all 353 Python files unchanged, Compose
configuration validation exits `0`, and the complete Python regression reports
`622 passed, 4 skipped`. The four skips require an external PostgreSQL test DSN;
no test DoD or trading behavior changed. A targeted contradiction/incomplete
marker scan found no stale Core-SQLite runtime claim; only the intentional legacy
transfer/rollback references remain. No runtime value was copied into the
documents and no commit was created.

---

### Task 8 (`0.4.8` CRIT): Final acceptance and rollback rehearsal

**Files:**

- Modify only if a verified interface defect is found in Tasks 1-7.
- Record status in `docs/phase-0-trading-service-refactor.md` after every gate
  passes.

**Interfaces:**

- Accepts the complete runtime topology from Tasks 1-7.
- Produces the stable prerequisite for milestone `0.2.1.2`.

- [x] **Step 1: Start from stopped application containers with retained volumes**

Verify the old Core SQLite and Worker SQLite volumes/files exist. Verify the
external PostgreSQL and Worker volumes are named explicitly. Do not delete any
volume.

- [x] **Step 2: Rebuild images without touching data**

Pull the pinned database image, then build Python base, migrations, backend,
Worker and frontend images.
Recreate application containers. Expected: PostgreSQL and Worker durable data
remain present.

- [x] **Step 3: Run schema and transfer idempotency checks**

Run schema migration twice and Core data transfer dry-run/apply twice. Expected:
second runs report no new schema/data effects and all counts stay equal.

- [x] **Step 4: Run end-to-end runtime checks**

Verify:

- Core health reports database `ok` and schema `compatible`;
- frontend loads through the existing reverse proxy;
- brokers, instruments, open positions, recent operations and analytics data are
  available after transfer;
- Worker claims current automation state through Core;
- Worker writes/reloads its local outbox without PostgreSQL access;
- no application log contains a database connection string.

- [x] **Step 5: Rehearse non-destructive rollback**

Stop new Core writes, switch the previous backend image/configuration back to the
retained Core SQLite file and verify its pre-cutover control counts. Do not delete
PostgreSQL. Return to PostgreSQL only after proving rollback is available.

- [x] **Step 6: Run final quality and regression gates**

Run the global regression gate plus the explicit PostgreSQL integration suite and
Docker Compose configuration validation. Record exact pass/fail/skip counts and
classify warnings.

- [x] **Step 7: Mark milestone status**

Mark `0.4` complete only if all DoD checks pass. Set the next step to `0.2.1.2`
Core trading fact tables on PostgreSQL. Do not mark any later fact-ingress,
cross-database Worker migration or cutover milestone complete.

- [x] **Step 8: Final diff review without commit**

Review the complete diff for unrelated changes and protected runtime values.
Report changed files, migration evidence, data-preservation evidence, regression
counts and remaining risks. Create no commit.

**Completion checkpoint (2026-08-12):** both retained SQLite volumes and the
external PostgreSQL volume were verified without deletion. Application images
were rebuilt and the schema job completed twice. The upgraded disposable copy
of the legacy Core database and the current PostgreSQL target have equal table
counts. A dry-run against the already active target reports only two expected
mutable-row conflicts (`sync_state` timestamp backfill and
`automaton_heartbeats` post-cutover activity); the user explicitly accepted
isolated-schema idempotency as the non-destructive acceptance proof for this
post-cutover state. The isolated PostgreSQL suite reports `4 passed`.

Core, frontend and Worker were recreated on the final images: Compose reports
database/backend/frontend healthy, Core health reports database `ok` and schema
`compatible`, and Worker remains on its retained SQLite volume without a Core
DSN. The previous backend image was started against a disposable copy of the
untouched legacy SQLite; all rollback smoke endpoints returned HTTP 200 and the
copy checksum and control counts stayed unchanged. PostgreSQL runtime was then
restored without deleting any volume.

The final gates report strict mypy clean for 200 source files, Ruff clean, Black
clean for 345 files, `623 passed, 4 skipped` in the complete Python regression,
`4 passed` in the explicit PostgreSQL suite, and `74 passed` plus a successful
frontend production build. Compose configuration validation exits zero. No
commit was created. Milestone `0.4` is complete; the next open stage is
`0.2.1.2`.
