# User Broker Reference Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build milestone `0.2.1.1`: a code-owned broker API registry, shadow `user_brokers` and user-broker-scoped reference tables, plus a safe idempotent migration of existing connection and catalog data.

**Architecture:** The current application continues reading legacy `brokers` tables until the later cutover milestone. New final domain contracts and `_v2` shadow ORM tables are introduced beside the legacy schema. A service-owned migration transforms a read-only legacy snapshot into a validated plan and a repository applies that plan atomically; a CLI defaults to dry-run and never emits connection setting values.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy 2, Alembic, SQLite, pytest, mypy, Ruff, Black, uv.

## Global Constraints

- Follow the approved design in `docs/superpowers/specs/2026-08-10-trading-data-schema-migration-design.md`.
- Do not switch production repositories or HTTP routes away from legacy tables in this milestone.
- Do not drop, rename, mutate, or truncate any legacy table.
- Available broker APIs are declared only in code; no broker/provider catalog table is created.
- One `user_broker` represents zero-or-one external account while `DRAFT` and exactly one account before trading is allowed.
- Sandbox preparation state and requested funding amount are transient UI/action data; the target schema must not persist them.
- Settings remain plaintext for the MVP but must never appear in logs, error text, migration reports, or CLI output.
- All new transport and domain DTOs are Pydantic models.
- New services use constructor DI and depend on Protocol ports, not concrete repositories.
- Alembic files do not receive isolated migration-structure tests; behavior is verified through models, repositories and the migration service.
- Existing test business conditions and assertions are not weakened. Interface-only fixtures may be adapted when the new interface is introduced.
- After every task, run its targeted tests and the full `uv run python -m pytest -q` regression.
- Apply Ruff and Black with the project configuration to every touched Python path.
- Do not create Git commits.

---

### Task 1: Code-owned broker API registry and final domain contracts

**Files:**

- Create: `src/moex_sentinel/domain/user_brokers.py`
- Create: `src/moex_sentinel/adapters/broker_api_registry.py`
- Create: `src/moex_sentinel/adapters/tinvest/api_module.py`
- Test: `tests/adapters/test_broker_api_registry.py`
- Test: `tests/domain/test_user_brokers.py`

**Interfaces:**

- Consumes: `sentinel_contracts.base.LegacyPositionalModel`, Pydantic `BaseModel`, `ConfigDict`, `Field`.
- Produces:
  - `UserBrokerState`;
  - `UserBrokerDraft`, `UserBroker`, `BrokerApiDescriptor`, `BrokerApiFieldDescriptor`;
  - `BrokerApiModule` Protocol;
  - `BrokerApiRegistry.list()`, `get(api_slug)`, `validate_settings(api_slug, value)`;
  - `TInvestApiModule` and `TInvestSettings`.

- [ ] **Step 1: Write failing domain and registry tests**

Add tests that prove:

```python
def test_registry_exposes_tinvest_by_stable_slug() -> None:
    registry = BrokerApiRegistry((TInvestApiModule(),))

    module = registry.get("t_invest")

    assert registry.list() == (module.descriptor,)
    assert module.descriptor.api_slug == "t_invest"
    assert module.descriptor.environments == ("TEST",)
    assert module.default_fqdn("TEST") == "sandbox-invest-public-api.tbank.ru:443"


def test_registry_rejects_unknown_slug_without_repeating_settings() -> None:
    registry = BrokerApiRegistry((TInvestApiModule(),))

    with pytest.raises(BrokerApiNotFoundError) as caught:
        registry.validate_settings("missing", {"token": "synthetic-token"})

    assert "synthetic-token" not in str(caught.value)


def test_tinvest_settings_validate_connection_token_only() -> None:
    result = BrokerApiRegistry((TInvestApiModule(),)).validate_settings(
        "t_invest",
        {"token": "synthetic-token"},
    )

    assert result["token"] == "synthetic-token"
```

Domain tests must also prove that `ACTIVE` requires `external_account_id`, `DRAFT`
allows it to be absent, and all DTOs reject unknown fields.

- [ ] **Step 2: Run the new tests and confirm RED**

Run:

```bash
uv run python -m pytest -q \
  tests/domain/test_user_brokers.py \
  tests/adapters/test_broker_api_registry.py
```

Expected: collection/import failure because the new modules do not exist.

- [ ] **Step 3: Implement final Pydantic domain contracts**

In `domain/user_brokers.py`, define the exact public shapes:

```python
class UserBrokerState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class BrokerApiFieldDescriptor(LegacyPositionalModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    required: bool
    value_type: str


class BrokerApiDescriptor(LegacyPositionalModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    api_slug: str
    display_name: str
    environments: tuple[str, ...]
    fields: tuple[BrokerApiFieldDescriptor, ...]


class UserBrokerDraft(LegacyPositionalModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    api_slug: str
    display_name: str
    environment: str
    fqdn: str
    settings: dict[str, object]
    external_account_id: str | None
    state: UserBrokerState


class UserBroker(UserBrokerDraft):
    id: str
    created_at: datetime
    updated_at: datetime
```

Use a Pydantic model validator on `UserBrokerDraft` so `ACTIVE` without a
non-blank account ID raises `ValidationError`. Do not inspect adapter-specific
settings in this DTO.

- [ ] **Step 4: Implement the registry and T-Invest module**

Define this consumer boundary in `adapters/broker_api_registry.py`:

```python
class BrokerApiModule(Protocol):
    descriptor: BrokerApiDescriptor

    def default_fqdn(self, environment: str) -> str: ...

    def validate_settings(self, value: object) -> dict[str, object]: ...


class BrokerApiRegistry:
    def __init__(self, modules: tuple[BrokerApiModule, ...]) -> None: ...

    def list(self) -> tuple[BrokerApiDescriptor, ...]: ...

    def get(self, api_slug: str) -> BrokerApiModule: ...

    def validate_settings(self, api_slug: str, value: object) -> dict[str, object]: ...
```

Constructor validation rejects duplicate slugs. `get` raises
`BrokerApiNotFoundError(api_slug)` without accepting or formatting settings.

In `adapters/tinvest/api_module.py`, use:

```python
class TInvestSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1)
```

`validate_settings` returns JSON-compatible values using
`model_dump(mode="json")`. `default_fqdn` accepts only `TEST` and raises a typed
`BrokerApiEnvironmentError` for any other value. The slug is `t_invest`.

- [ ] **Step 5: Run targeted and full regression**

Run:

```bash
uv run python -m pytest -q \
  tests/domain/test_user_brokers.py \
  tests/adapters/test_broker_api_registry.py
uv run python -m pytest -q
```

Expected: new tests pass and the full suite has zero failures.

---

### Task 2: Shadow reference schema and Alembic revision

**Files:**

- Create: `src/moex_sentinel/storage/models/user_brokers_v2.py`
- Create: `src/moex_sentinel/storage/models/reference_data_v2.py`
- Create: `src/moex_sentinel/storage/models/data_migration.py`
- Modify: `src/moex_sentinel/storage/models/__init__.py`
- Create: `alembic/versions/a0f211000001_add_user_broker_reference_shadow_schema.py`
- Test: `tests/storage/test_user_broker_reference_models.py`

**Interfaces:**

- Consumes: domain enum values from Task 1 and existing `Base`,
  `TimestampMixin`, `UuidPrimaryKeyMixin`, `UTCDateTime`.
- Produces: shadow ORM tables `user_brokers_v2`, `broker_instruments_v2`,
  `instrument_strategy_defaults_v2`, `instrument_sync_state_v2`,
  `data_migration_runs`, `legacy_id_map`.

- [ ] **Step 1: Write failing model behavior tests**

Cover these constraints through SQLAlchemy behavior, not by inspecting Alembic
source text:

```python
NOW = datetime(2026, 8, 10, tzinfo=UTC)


def user_broker_model(
    record_id: str,
    *,
    state: str = "ACTIVE",
    external_account_id: str | None = "synthetic-account",
) -> UserBrokerV2Model:
    return UserBrokerV2Model(
        id=record_id,
        api_slug="t_invest",
        display_name=f"connection-{record_id}",
        environment="TEST",
        fqdn="sandbox-invest-public-api.tbank.ru:443",
        settings={"token": "synthetic-token"},
        external_account_id=external_account_id,
        state=state,
        created_at=NOW,
        updated_at=NOW,
    )


def instrument_model(
    record_id: str,
    user_broker_id: str,
    external_instrument_id: str = "synthetic-instrument",
) -> BrokerInstrumentV2Model:
    return BrokerInstrumentV2Model(
        id=record_id,
        user_broker_id=user_broker_id,
        external_instrument_id=external_instrument_id,
        external_identifiers={},
        ticker="SYNTH",
        name="Synthetic instrument",
        instrument_type="SHARE",
        class_code="TEST",
        currency="RUB",
        lot_size=1,
        min_price_increment=Decimal("0.01"),
        api_trade_available=True,
        is_active=True,
        is_selected=False,
        first_seen_at=NOW,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def test_active_user_broker_requires_account_and_unique_scope(session: Session) -> None:
    session.add(user_broker_model("scope-1", external_account_id=None))
    with pytest.raises(IntegrityError):
        session.flush()


def test_same_external_instrument_is_isolated_by_user_broker(session: Session) -> None:
    session.add_all(
        [
            user_broker_model("scope-1", external_account_id="account-1"),
            user_broker_model("scope-2", external_account_id="account-2"),
        ]
    )
    session.add_all(
        [
            instrument_model("instrument-1", "scope-1", "uid"),
            instrument_model("instrument-2", "scope-2", "uid"),
        ]
    )
    session.flush()


def test_reference_rows_cannot_cross_missing_user_broker_scope(session: Session) -> None:
    session.add(instrument_model("instrument-1", "missing"))
    with pytest.raises(IntegrityError):
        session.flush()
```

Also test uniqueness of `(user_broker_id, external_instrument_id)`, one sync-state
row per user broker, and one instrument default row per scoped instrument.

- [ ] **Step 2: Run the new model tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/storage/test_user_broker_reference_models.py
```

Expected: import failure for missing target models.

- [ ] **Step 3: Implement shadow ORM models**

`UserBrokerV2Model` maps `user_brokers_v2` with:

```text
id UUID PK
api_slug VARCHAR(64) NOT NULL
display_name VARCHAR(120) NOT NULL
environment VARCHAR(8) NOT NULL CHECK TEST|PROD
fqdn VARCHAR(255) NOT NULL
settings JSON NOT NULL
external_account_id VARCHAR(128) NULL
state VARCHAR(16) NOT NULL
created_at UTC NOT NULL
updated_at UTC NOT NULL
```

Add checks for the enum values and
`state != 'ACTIVE' OR external_account_id IS NOT NULL`. Add a partial unique index
over `(api_slug, environment, external_account_id)` where the account is non-null.

`BrokerInstrumentV2Model` maps `broker_instruments_v2` and contains the exact
catalog fields in the design, including `external_identifiers` JSON and a foreign
key to `user_brokers_v2.id`. Its unique key is
`(user_broker_id, external_instrument_id)`.

`InstrumentStrategyDefaultV2Model` maps
`instrument_strategy_defaults_v2` with `user_broker_id`, `instrument_id`,
`settings` JSON and timestamps. Use a composite uniqueness constraint on scope and
instrument.

`InstrumentSyncStateV2Model` maps `instrument_sync_state_v2`, uses
`user_broker_id` as its primary key, and contains status, attempt/success times,
safe error and timestamps.

`DataMigrationRunModel` stores run UUID, phase, mode, state, safe count/report JSON
and start/finish times. `LegacyIdMapModel` stores unique
`(migration_name, source_kind, source_id, target_kind, target_id)` mappings. The
full key permits one legacy source to map to multiple account-scoped targets and
rejects an exact duplicate mapping. Neither table stores connection settings.

Implementation finding `0.2.1.1.1` (`MAJOR`): the earlier three-column unique key
could not represent one legacy broker copied to multiple `user_broker` records.
DoD is the behavioral model test that accepts two different targets for one
source and rejects a repeated full mapping, followed by the complete regression.

- [ ] **Step 4: Add the matching Alembic revision**

Create revision `a0f211000001` with `down_revision = "f2c0fd3e0d31"`. Its upgrade
creates the six shadow/support tables and their indexes. Its downgrade drops only
those six tables in reverse foreign-key order. It does not read or mutate legacy
rows.

Do not add a dedicated test for the revision file. Validate the same constraints
through ORM behavior and execute the migration manually in Task 6.

- [ ] **Step 5: Run targeted and full regression**

Run:

```bash
uv run python -m pytest -q tests/storage/test_user_broker_reference_models.py
uv run python -m pytest -q
```

Expected: target model tests pass and legacy tests remain unchanged and green.

---

### Task 3: Shadow repositories for user brokers and scoped catalog

**Files:**

- Create: `src/moex_sentinel/storage/repositories/user_brokers_v2.py`
- Create: `src/moex_sentinel/storage/repositories/reference_catalog_v2.py`
- Modify: `src/moex_sentinel/storage/repositories/__init__.py`
- Test: `tests/storage/test_user_broker_v2_repository.py`
- Test: `tests/storage/test_reference_catalog_v2_repository.py`

**Interfaces:**

- Consumes: `UserBroker`, `UserBrokerDraft`, shadow models from Task 2.
- Produces:
  - `UserBrokerShadowRepository.list/get/create/replace/disable`;
  - `ReferenceCatalogShadowRepository.reconcile/list/get/set_selected`;
  - repository-neutral not-found, duplicate and constraint errors in
    `domain/user_brokers.py`.

- [ ] **Step 1: Write failing repository behavior tests**

Tests must prove:

- create/get/list returns detached Pydantic records;
- settings round-trip without appearing in exceptions;
- replacement is atomic;
- `disable` changes state without deleting history;
- duplicate non-null external account maps to a domain duplicate error;
- catalog reconciliation is isolated by `user_broker_id`;
- missing catalog entries are marked inactive rather than deleted;
- selection remains set after a later reconciliation.

Use only synthetic local settings in tests.

- [ ] **Step 2: Run repository tests and confirm RED**

Run:

```bash
uv run python -m pytest -q \
  tests/storage/test_user_broker_v2_repository.py \
  tests/storage/test_reference_catalog_v2_repository.py
```

Expected: import failure for missing repositories.

- [ ] **Step 3: Implement `UserBrokerShadowRepository`**

Expose exact methods:

```python
class UserBrokerShadowRepository:
    def list(self) -> list[UserBroker]: ...
    def get(self, user_broker_id: str) -> UserBroker: ...
    def create(self, draft: UserBrokerDraft, *, record_id: str | None = None) -> UserBroker: ...
    def replace(self, user_broker_id: str, draft: UserBrokerDraft) -> UserBroker: ...
    def disable(self, user_broker_id: str) -> UserBroker: ...
```

All writes use `session_scope`. `record_id` exists only for deterministic migration
IDs. Integrity errors map to typed domain errors without including statement
parameters or JSON settings.

- [ ] **Step 4: Implement `ReferenceCatalogShadowRepository`**

Use final domain records from `domain/instrument_catalog.py` only after adding
`user_broker_id`-compatible V2 record models in this module. Do not change the
legacy repository interface yet.

Expose:

```python
def reconcile(
    self,
    user_broker_id: str,
    snapshot: tuple[CatalogInstrumentDraft, ...],
    synchronized_at: datetime,
) -> CatalogReconciliationResult: ...

def list(self, user_broker_id: str, *, include_inactive: bool) -> tuple[CatalogInstrument, ...]: ...
def get(self, user_broker_id: str, instrument_id: str) -> CatalogInstrument: ...
def set_selected(self, user_broker_id: str, instrument_id: str, selected: bool) -> CatalogInstrument: ...
```

Map legacy `figi` into `external_identifiers={"figi": value}` at the migration
boundary. Normal runtime drafts may provide an empty identifier dictionary when
the API does not use FIGI.

- [ ] **Step 5: Run targeted and full regression**

Run:

```bash
uv run python -m pytest -q \
  tests/storage/test_user_broker_v2_repository.py \
  tests/storage/test_reference_catalog_v2_repository.py
uv run python -m pytest -q
```

Expected: all new repository tests and the complete suite pass.

---

### Task 4: Pure reference migration planner and fail-fast validation

**Files:**

- Create: `src/moex_sentinel/domain/reference_data_migration.py`
- Create: `src/moex_sentinel/services/reference_data_migration.py`
- Test: `tests/services/test_reference_data_migration_service.py`

**Interfaces:**

- Consumes: `BrokerApiRegistry`, immutable legacy snapshot DTOs and final V2
  target DTOs.
- Produces:
  - `LegacyReferenceSnapshot`;
  - `ReferenceMigrationPlan`;
  - `ReferenceMigrationReport`;
  - `ReferenceMigrationIssue`;
  - `ReferenceDataMigrationService.plan(snapshot)`.

- [ ] **Step 1: Write failing transformation tests**

Create fixtures and assertions for:

1. one enabled sandbox broker with one external account;
2. one broker referenced by two automation account IDs;
3. one unprovisioned broker with no trading history becoming `DRAFT`;
4. instrument duplication with deterministic target IDs for two accounts;
5. watchlist selection folded into target instrument selection;
6. strategy config converted to an instrument default JSON payload;
7. sync state re-scoped to each derived user broker;
8. unknown adapter, invalid settings, account ambiguity and conflicting selection
   producing a failed report without submitted settings in its text.

The deterministic ID test calls `plan` twice and compares complete plans.

- [ ] **Step 2: Run service tests and confirm RED**

Run:

```bash
uv run python -m pytest -q tests/services/test_reference_data_migration_service.py
```

Expected: import failure for missing migration DTOs/service.

- [ ] **Step 3: Implement migration DTOs**

All DTOs are frozen Pydantic models. `LegacyReferenceSnapshot` contains tuples of
legacy brokers, fields, automation account references, catalog rows, watchlist
rows, strategy configs and sync states. It never implements `__str__` that exposes
field values.

`ReferenceMigrationReport` contains only:

```python
class ReferenceMigrationReport(LegacyPositionalModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["DRY_RUN", "APPLY"]
    source_counts: dict[str, int]
    target_counts: dict[str, int]
    issues: tuple[ReferenceMigrationIssue, ...]
    legacy_to_target_ids: dict[str, tuple[str, ...]]
    excluded_legacy_fields: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.issues
```

Issue data is restricted to safe codes, source kind and UUID. It contains no
settings, endpoint, external account ID or submitted field value.
`excluded_legacy_fields` is exactly
`("initial_balance", "test_account_funded")`; it records the schema decision but
never reads or exposes the corresponding legacy values.

- [ ] **Step 4: Implement deterministic planning**

Use one fixed UUID namespace constant and UUIDv5 keys:

```text
user-broker:{legacy_broker_uuid}:{external_account_id-or-draft}
instrument:{target_user_broker_uuid}:{external_instrument_id}
strategy-default:{target_user_broker_uuid}:{external_instrument_id}
```

Rules:

- `TINVEST_SANDBOX/SANDBOX` maps explicitly to `t_invest/TEST`;
- settings are legacy connection fields excluding `fqdn`, `initial_balance` and
  `test_account_funded`;
- the registry validates the settings;
- the report lists `initial_balance` and `test_account_funded` as intentionally
  excluded deprecated field names without including their values;
- enabled plus account becomes `ACTIVE`; disabled plus account becomes
  `DISABLED`; no account and no trading references becomes `DRAFT`;
- account candidates are the union of the configured account and automation
  account IDs;
- a catalog row is copied once per derived user broker;
- `is_selected` is true when either the old catalog flag or an enabled watchlist
  row is true;
- mismatched provider/adapter/environment, invalid settings, a draft with trading
  references, missing instrument mapping, or conflicting duplicate source rows
  produces an issue and prevents apply.

The service never writes to storage.

- [ ] **Step 5: Run targeted and full regression**

Run:

```bash
uv run python -m pytest -q tests/services/test_reference_data_migration_service.py
uv run python -m pytest -q
```

Expected: planner tests and full regression pass.

---

### Task 5: Atomic source reader, plan writer, use case and safe CLI

**Files:**

- Create: `src/moex_sentinel/storage/repositories/reference_data_migration.py`
- Create: `src/moex_sentinel/usecases/reference_data_migration.py`
- Create: `src/moex_sentinel/migrations/__init__.py`
- Create: `src/moex_sentinel/migrations/reference_data.py`
- Modify: `pyproject.toml`
- Test: `tests/storage/test_reference_data_migration_repository.py`
- Test: `tests/usecases/test_reference_data_migration_usecase.py`
- Test: `tests/test_reference_data_migration_cli.py`

**Interfaces:**

- Consumes: planner from Task 4 and target models from Task 2.
- Produces:
  - `ReferenceDataMigrationRepository.read_legacy_snapshot()`;
  - `ReferenceDataMigrationRepository.apply(plan, report, occurred_at)`;
  - `MigrateReferenceDataUsecase.execute(apply: bool)`;
  - console command `moex-migrate-reference-data`.

- [ ] **Step 1: Write failing repository, use-case and CLI tests**

Repository tests seed legacy Core tables and prove:

- the snapshot contains all required reference rows;
- `apply` writes all target rows in one transaction;
- a second identical apply creates no duplicate row and returns the same mapping;
- an injected target conflict rolls back every target write;
- no legacy row is updated or deleted.

Use-case tests prove dry-run never calls `apply` and a dirty report raises
`ReferenceMigrationBlockedError` before mutation.

CLI tests prove:

- no flag means dry-run;
- `--apply` performs a write only after a clean plan;
- stdout contains counts and issue codes only;
- stdout and stderr do not contain synthetic submitted settings.

- [ ] **Step 2: Run the new tests and confirm RED**

Run:

```bash
uv run python -m pytest -q \
  tests/storage/test_reference_data_migration_repository.py \
  tests/usecases/test_reference_data_migration_usecase.py \
  tests/test_reference_data_migration_cli.py
```

Expected: import/entrypoint failures for missing migration components.

- [ ] **Step 3: Implement the repository facade**

`read_legacy_snapshot` reads legacy tables through SQLAlchemy selects and returns
detached Pydantic DTOs. It sorts every entity by stable source ID.

`apply` opens one `factory.begin()` transaction, verifies `report.clean`, upserts
deterministic target IDs, writes `legacy_id_map`, and records a migration run with
safe counts. It rejects an existing target row whose non-secret business fields
conflict with the plan. It never places settings in an exception message.

- [ ] **Step 4: Implement use case orchestration**

Use exact consumer ports:

```python
class ReferenceMigrationRepositoryPort(Protocol):
    def read_legacy_snapshot(self) -> LegacyReferenceSnapshot: ...
    def apply(
        self,
        plan: ReferenceMigrationPlan,
        report: ReferenceMigrationReport,
        occurred_at: datetime,
    ) -> ReferenceMigrationReport: ...


class MigrateReferenceDataUsecase:
    def execute(self, *, apply: bool) -> ReferenceMigrationReport: ...
```

The use case asks the service to plan, raises
`ReferenceMigrationBlockedError(report)` when issues exist, returns dry-run report
without mutation, and otherwise calls repository `apply`.

- [ ] **Step 5: Implement the CLI with explicit apply opt-in**

Add:

```toml
[project.scripts]
trading-automaton = "trading_automaton.__main__:main"
moex-migrate-reference-data = "moex_sentinel.migrations.reference_data:main"
```

The CLI accepts `--database-url` and `--apply`. It defaults to the application
SQLite URL and DRY_RUN. Its renderer prints JSON containing only mode, clean,
source counts, target counts and safe issue codes/source UUIDs. Exit codes:

- `0`: clean dry run or successful apply;
- `2`: validation blocked migration;
- `1`: unexpected safe top-level failure.

Do not use application logging for raw snapshot or plan DTOs.

- [ ] **Step 6: Run targeted and full regression**

Run:

```bash
uv run python -m pytest -q \
  tests/storage/test_reference_data_migration_repository.py \
  tests/usecases/test_reference_data_migration_usecase.py \
  tests/test_reference_data_migration_cli.py
uv run python -m pytest -q
```

Expected: migration behavior tests and the complete regression pass.

---

### Task 6: Milestone acceptance and documentation

**Files:**

- Modify: `docs/phase-0-trading-service-refactor.md`
- Modify: `AGENT_BRIEF.md`
- Verify: all files touched in Tasks 1–5

**Interfaces:**

- Consumes: completed API registry, shadow schema and clean migration report.
- Produces: accepted `0.2.1.1` with evidence; no production cutover.

- [ ] **Step 1: Create disposable legacy data and execute Alembic**

Use a newly allocated temporary SQLite path. Upgrade first to the last legacy
revision and then to head:

```bash
MIGRATION_ACCEPTANCE_DIR="$(mktemp -d /tmp/moex-sentinel-0211.XXXXXX)"
MIGRATION_ACCEPTANCE_DB="$MIGRATION_ACCEPTANCE_DIR/core.db"
uv run alembic -x database_url="sqlite:///$MIGRATION_ACCEPTANCE_DB" upgrade f2c0fd3e0d31
uv run alembic -x database_url="sqlite:///$MIGRATION_ACCEPTANCE_DB" upgrade head
```

Expected: the upgrade creates shadow/support tables and preserves every legacy
table. Seeded transformation behavior is already covered by the repository and
service tests; this step verifies the real Alembic chain without a dedicated
migration-file unit test.

- [ ] **Step 2: Run dry-run and apply against the disposable database**

Run:

```bash
uv run moex-migrate-reference-data \
  --database-url "sqlite:///$MIGRATION_ACCEPTANCE_DB"
uv run moex-migrate-reference-data \
  --database-url "sqlite:///$MIGRATION_ACCEPTANCE_DB" \
  --apply
uv run moex-migrate-reference-data \
  --database-url "sqlite:///$MIGRATION_ACCEPTANCE_DB" \
  --apply
```

Expected: dry run is clean; first apply records a successful empty-source run;
second apply is idempotent. Output contains no settings. Non-empty mapping,
rollback and idempotence are proven by Task 4 and Task 5 tests.

- [ ] **Step 3: Run static, format and focused checks**

Run Ruff and Black over the exact new/touched Python paths, then:

```bash
uv run mypy \
  src/moex_sentinel/domain/user_brokers.py \
  src/moex_sentinel/adapters/broker_api_registry.py \
  src/moex_sentinel/adapters/tinvest/api_module.py \
  src/moex_sentinel/services/reference_data_migration.py \
  src/moex_sentinel/storage/repositories/reference_data_migration.py \
  src/moex_sentinel/usecases/reference_data_migration.py
uv run python -m pytest -q \
  tests/domain/test_user_brokers.py \
  tests/adapters/test_broker_api_registry.py \
  tests/storage/test_user_broker_reference_models.py \
  tests/storage/test_user_broker_v2_repository.py \
  tests/storage/test_reference_catalog_v2_repository.py \
  tests/services/test_reference_data_migration_service.py \
  tests/storage/test_reference_data_migration_repository.py \
  tests/usecases/test_reference_data_migration_usecase.py \
  tests/test_reference_data_migration_cli.py
```

Expected: mypy reports zero issues and all focused tests pass.

- [ ] **Step 4: Run full acceptance**

Run:

```bash
uv run python -m pytest -q
uv run python -m pytest -q tests/test_documentation.py
uv lock --check
git diff --check
```

Expected: zero failures, documentation tests pass, lock is current and no
whitespace errors exist.

- [ ] **Step 5: Update milestone documentation**

Mark only `0.2.1.1` complete. Record:

- exact focused/full test counts;
- migration dry-run/apply/idempotent-rerun results;
- static/format results;
- confirmation that legacy tables remain untouched;
- explicit next step `0.2.1.2` for Core trading fact tables;
- explicit statement that production repositories and HTTP routes still use the
  legacy schema until `0.2.1.5`.
- explicit statement that the broker-page provisioning UI remains on the legacy
  route until `0.2.1.5`, while the new schema already excludes preparation state
  and funding amount.

Do not mark all of `0.2.1` complete.
