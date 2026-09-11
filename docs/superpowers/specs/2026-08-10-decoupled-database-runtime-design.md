# Decoupled Database Runtime Design

## Status

- Date: 2026-08-10
- Milestone: `0.4`, prerequisite for `0.2.1.2`
- Priority: CRIT
- Decision: separate PostgreSQL runtime, Core-owned persistence and autonomous Worker recovery storage
- Review state: approved by the user
- Constraint: no Git commits

## Goal

Separate application images, database runtime and durable data so that Core,
Worker, schema migrations and PostgreSQL can be deployed or moved independently
without rebuilding an application image causing data loss.

The target topology must also allow Worker to run on a remote host without direct
access to the authoritative database while keeping its sub-second trading path
independent of PostgreSQL latency.

## Decisions

1. PostgreSQL is the authoritative Core database.
2. PostgreSQL Server is never installed in `moex-sentinel-python-base`.
3. PostgreSQL data is stored only on an external persistent volume or in a
   managed database, never in a container image or writable application layer.
4. Core is the only application allowed to read or write the authoritative
   trading database.
5. Worker never connects to PostgreSQL directly. It exchanges typed,
   idempotent facts and commands with Core through application interfaces.
6. Worker keeps hot market and decision state in memory.
7. Worker keeps a minimal durable SQLite outbox and broker-recovery journal on a
   host-mounted persistent volume. This storage is operational, not an
   authoritative UI data source.
8. Schema migrations run as an explicit one-shot migration job. Core startup
   does not apply migrations implicitly.
9. Schema delivery follows `expand -> migrate -> contract` so compatible code
   and database versions can overlap during deployment.

## Non-goals

- sharing one SQLAlchemy session, database schema or database connection between
  Core and Worker;
- moving Worker hot state or every market snapshot to PostgreSQL;
- replacing the Worker recovery journal with Redis;
- creating a separate persistence microservice in this phase;
- making Worker continue placing new broker orders after its durable outbox
  becomes unavailable;
- deleting legacy SQLite files during migration;
- implementing production orchestration, replication or a managed PostgreSQL
  provider in the local Docker Compose configuration.

## Runtime topology

```text
Remote Worker host
+-----------------------------------------------+
| trading-automaton image                       |
|                                               |
| in-memory hot state                           |
|   - current market snapshots                  |
|   - active automation snapshots               |
|   - indicators and decision inputs            |
|                                               |
| local durable SQLite volume                   |
|   - fact outbox                               |
|   - non-terminal broker-order recovery        |
|   - command/revision checkpoints              |
+-----------------------+-----------------------+
                        | typed Core API
                        | FactIngressPort / command contracts
                        v
Core host
+-----------------------------------------------+
| backend image                                 |
|   - views -> usecases -> services             |
|   - repository ports                          |
|   - fact ingress and idempotency               |
|                                               |
| migrations image/job                          |
|   - Alembic schema migration                  |
|   - explicit data migration commands          |
+-----------------------+-----------------------+
                        | PostgreSQL DSN
                        v
+-----------------------------------------------+
| PostgreSQL container or managed PostgreSQL    |
| external persistent data volume               |
+-----------------------------------------------+
```

For local development, all containers may share one Compose network. The network
topology is a deployment detail: application contracts remain the same when
Worker and Core are moved to different hosts.

## Container and image boundaries

### Python base image

`moex-sentinel-python-base` contains Python 3.12 and shared application
dependencies. It may contain a PostgreSQL client driver required by SQLAlchemy,
but it must not contain the PostgreSQL server, database files, initialization
scripts or embedded credentials.

### PostgreSQL image

Local Compose uses the dedicated upstream `postgres:16-alpine` image directly.
The image contains only the database runtime and its upstream bootstrap assets.
It contains no project source code and no persisted database files.

The Compose database service owns:

- an explicit healthcheck;
- a pre-created external named volume mounted at PostgreSQL's data directory, so
  Compose does not own its deletion;
- runtime configuration supplied outside the image;
- a stable service name used only by local deployment configuration.

Removing or rebuilding the image must not remove the persistent volume. Volume
deletion is a separate, explicit destructive operation and is not part of normal
`build`, `up`, `restart` or application deployment flows.

### Backend image

The backend image contains Core code and PostgreSQL client dependencies. It waits
for database readiness but does not mutate the schema automatically. It refuses
readiness when the database cannot be reached or the schema compatibility check
fails.

### Migration image/job

The migration image contains the same domain/ORM metadata and locked Python
dependencies as Core, plus the migration entrypoints. It runs to completion and
exits. Application deployment invokes it explicitly before switching traffic to
code that requires the expanded schema.

The migration job and backend image are built from the same source revision to
avoid model/migration drift. This is a shared build input, not a shared runtime
process.

### Worker image

The Worker image contains no PostgreSQL server and no authoritative database
credentials. Its only database URL is the local SQLite recovery store. Its Core
connection is an application API endpoint, not a SQL DSN.

## Data ownership

### Authoritative Core data

PostgreSQL owns the durable business history defined by the trading-data schema:

- user broker configuration and instruments;
- automation lifecycle and strategy snapshots;
- decisions, broker orders, order events and executions;
- position cycles, lots and allocations;
- accepted fact envelopes and linked business audit;
- analytical snapshots intended for the UI.

The UI reads these data only through Core usecases and services.

### Worker hot state

Worker may cache current commands, market data and derived indicators in memory.
The cache is disposable and rebuilt from Core and broker state after a restart.
No business history is considered persisted merely because it exists in this
cache.

### Worker durable recovery state

Before sending an order to the broker, Worker durably records the decision,
proposed order identity and idempotency key in the local SQLite journal. Broker
responses, order-state observations and executions are appended to the outbox
before they are acknowledged as delivered to Core.

Worker deletes or compacts an outbox record only after:

1. Core acknowledges the exact fact identity and sequence;
2. the broker order no longer requires local reconciliation; and
3. the related recovery checkpoint is durable.

This journal exists to bridge process, network, SDK and Core failures. It is not
a second source of truth after Core acknowledgement.

## Interfaces

### Core repository ports

Core usecases and services depend on repository protocols, not PostgreSQL or
SQLAlchemy models. PostgreSQL adapters implement those protocols. Transactions
are owned by the Core usecase/fact-ingress unit of work.

### Worker-to-Core contracts

Worker uses two explicit interface families:

- command/state interfaces for obtaining current automation state and revision;
- `FactIngressPort` for submitting ordered batches of decisions, order changes,
  executions and audit events.

Every fact batch carries stable identifiers, automation scope, sequence number,
expected revision and timestamps. Core ingestion is idempotent: retrying the
same accepted fact returns its acknowledgement without applying financial effects
twice.

Transport adapters may use HTTP in the current implementation. Domain and
application layers depend on the ports and Pydantic contracts rather than HTTP
clients or response dictionaries.

## Trading tick and persistence flow

The normal decision path does not wait for PostgreSQL:

1. Worker refreshes the current automation revisions through one batched Core
   request.
2. Worker receives or reads the current market snapshot into the in-memory hot
   cache.
3. Active positions are evaluated concurrently from the same fresh snapshot.
4. A decision that can produce an order is written to the local durable journal.
5. Worker sends the order through the broker SDK.
6. Broker response and subsequent status observations are appended locally.
7. Worker publishes ordered fact batches to Core asynchronously from the trading
   calculation.
8. Core validates scope, ordering and revision, then commits the accepted batch
   to PostgreSQL in one transaction per automation.
9. Worker advances its acknowledgement checkpoint and compacts confirmed local
   records.

The latency SLA up to SDK request dispatch therefore depends on market delivery,
in-memory calculation and a small local SQLite write, not on a remote PostgreSQL
round trip.

## Failure handling

### PostgreSQL unavailable

Core fails readiness and rejects persistence-dependent requests with a typed
temporary-unavailability error. It must not acknowledge facts that were not
committed.

Worker retains unacknowledged facts locally and retries fact publication with
bounded backoff. Existing broker orders continue through reconciliation when the
local journal and broker SDK are available, but new decisions must respect local
backpressure and recovery policy.

### Core or network unavailable

Worker continues to preserve already observed broker facts locally. Once its
configured retry/backpressure limit is exceeded, affected automations move to
`HOLD`. Recovery never resumes trading automatically; the current manual
`RESUME` rule remains authoritative.

### Worker local journal unavailable

Worker must not place a new broker order because it cannot durably establish
intent before the external side effect. Active automations move to `HOLD`, and a
structured audit event is emitted when delivery becomes possible.

### Worker restart

In-memory state is discarded. Worker first restores unacknowledged outbox and
non-terminal broker-order state, reconciles it with Core and the broker, and only
then accepts manual `RESUME` for affected automations.

### Migration failure

The migration job exits non-zero and traffic is not switched to code requiring
the new schema. The previous application version and database schema remain in
service. Destructive contract migrations are never combined with the first
deployment that stops using the old structure.

## Schema and data migration

The current Core and Worker SQLite files remain immutable migration sources until
acceptance is complete.

The transition is staged:

1. **Expand** PostgreSQL with the target tables, indexes and compatibility fields.
2. **Migrate reference data** using the existing deterministic legacy ID map.
3. **Migrate trading data** from both SQLite sources through the approved
   reconciliation and fact-normalization service.
4. **Verify** row counts, source-to-target mappings, chronological invariants,
   financial control totals and unresolved conflicts.
5. **Dual-compatible deployment** runs code that can tolerate the expanded schema
   while old fields still exist.
6. **Cut over** reads and writes to PostgreSQL after the migration report passes.
7. **Contract** removes obsolete structures only in a later independently
   reversible deployment.

Data migration commands support dry-run and idempotent rerun. They never delete
or rewrite the source SQLite files. Migration reports contain safe identifiers
and aggregate diagnostics, not broker connection settings.

## Configuration

Core receives a PostgreSQL SQLAlchemy URL and pool settings from runtime
configuration. Worker receives a local SQLite URL and Core API URL. Database
runtime configuration and credentials are never baked into images or source
files.

Local Docker Compose may provide development defaults through environment
substitution. Production deployment supplies its own external volume or managed
database DSN without changing application code.

## Deployment independence

The following operations have separate lifecycles:

- build and replace backend image;
- build and replace Worker image on another host;
- run an additive schema migration;
- migrate legacy data;
- upgrade the PostgreSQL runtime within a supported major-version procedure;
- move PostgreSQL to a managed service;
- retain, snapshot, back up or restore the database volume.

No normal application image build or replacement operation owns a database-data
deletion step.

## Testing and acceptance

### Service tests

- repository ports remain mockable without PostgreSQL;
- Worker decisions use in-memory state and do not open a PostgreSQL connection;
- order dispatch is forbidden when the local durable journal write fails;
- duplicate fact delivery is idempotent;
- acknowledged and non-terminal outbox records follow the retention rules;
- typed temporary-unavailability errors preserve retry semantics.

### Integration tests

- Core repositories and constraints run against PostgreSQL, not only SQLite;
- migration job upgrades an empty PostgreSQL database to the expected head;
- Core starts only after database health and schema compatibility succeed;
- Worker sends a fact batch through Core and the committed PostgreSQL rows can be
  read through Core;
- Core/network interruption preserves the Worker outbox and retry succeeds after
  recovery;
- rebuilding/recreating application containers preserves PostgreSQL data and
  Worker outbox data;
- the legacy SQLite-to-PostgreSQL migration is dry-runnable and idempotent.

Migration files themselves do not receive structural unit tests. Migration
behavior is accepted through end-to-end upgrade and data-reconciliation checks.

### Regression gate

Every implementation subtask ends with the full project regression suite. A
failure caused by an intentional compatible interface change may be fixed without
changing the test's business DoD. A failure that would require weakening or
changing an established DoD is not accepted automatically and must be raised for
separate review.

## Definition of Done

The design is implemented when:

1. local Compose exposes independent database, migration, Core, Worker and
   frontend lifecycle boundaries;
2. PostgreSQL Server and its data are absent from every application/base image;
3. PostgreSQL data survives application rebuild and container recreation;
4. Core is the only authoritative PostgreSQL client at runtime;
5. Worker runs with in-memory hot state plus a persistent local SQLite recovery
   volume and has no PostgreSQL DSN;
6. migration execution is explicit and blocks incompatible Core deployment on
   failure;
7. existing Core and Worker SQLite data can be migrated with a dry-run report,
   idempotent rerun and no source deletion;
8. failure and recovery cases are covered by service and integration tests;
9. the full regression suite, Ruff, Black and mypy pass;
10. operational documentation explains local startup, migration, rollback,
    backup/restore responsibility and remote Worker deployment.

## Relationship to the trading-data migration

This decision changes the storage topology but not the normalized fact semantics
approved in `2026-08-10-trading-data-schema-migration-design.md`.

The database-runtime transition is implemented before the remaining Core trading
fact tables in `0.2.1.2`. Reference-data shadow tables created in `0.2.1.1` are
migrated to PostgreSQL as part of this transition. Subsequent `0.2.1.2-0.2.1.5`
work targets PostgreSQL while Worker keeps its isolated recovery database.
