# Typed Fact Ingress and Worker Outbox Design

## Status

- Date: 2026-08-13
- Milestone: `0.2.1.3`
- Priority: CRIT
- Decision: parallel typed `v2` ingress with a unified Worker fact outbox
- Review state: approved by the user section by section
- Previous milestone: `0.2.1.2`, Alembic head `a0f212000001`
- Next milestones: history migration `0.2.1.4`, cutover `0.2.1.5`, legacy cleanup `0.2.1.6`
- Constraint: no Git commits

## Context

Milestone `0.2.1.2` created the complete user-broker-scoped Core trading-fact
shadow schema, grouped repositories and `TradingFactsShadowUnitOfWork`. The
current Worker still publishes the legacy `AutomationEvent` contract with an
untyped `metadata` mapping, and Core applies it through legacy tables and
repositories. That path cannot express the normalized decision, order,
execution, lot and audit facts without ambiguous payload conventions.

Milestone `0.2.1.3` introduces a new typed delivery path for newly produced
runtime facts. It does not migrate historical Worker rows, switch API reads to
the shadow schema, or remove legacy code. Those actions remain separate stages
so delivery can be accepted and rolled back independently.

## Goals

- Define one shared, strictly typed Worker-to-Core fact contract without Core
  ORM or domain dependencies in Worker.
- Persist each new runtime fact in a durable Worker SQLite outbox in the same
  transaction as the recovery-state change that produced it.
- Publish a mixed-automation batch accumulated by count or deadline to one new
  internal `v2` endpoint.
- Validate scope, sequence, revision, lineage and payload type before accepting
  facts into Core PostgreSQL.
- Process every automation group atomically while allowing other automation
  groups in the same HTTP batch to succeed independently.
- Preserve exact idempotent retry and deterministic timeline ordering.
- Keep legacy ingress available only as an explicit rollback path until cutover.
- Establish an explicit handoff to history migration, repository/API cutover and
  complete legacy-code cleanup.

## Non-goals

- migrating or acknowledging historical legacy outbox/history rows;
- changing current API/dashboard read repositories;
- dual-publishing one runtime fact to legacy and `v2` paths;
- automatic fallback from `v2` to legacy ingress;
- introducing Kafka, RabbitMQ, Redis Streams or another message broker;
- sharing Core PostgreSQL sessions, ORM models or domain records with Worker;
- deleting legacy tables, endpoints, DTOs, repositories or compatibility code;
- adding a global auto-incrementing business-event identifier.

## Chosen architecture

The new path runs in parallel with the existing legacy endpoint until
`0.2.1.5`. A deployed Worker uses exactly one configured ingress version. For
new runtime facts `FACT_INGRESS_VERSION=v2` selects the new endpoint and the new
outbox. Legacy ingress remains deployable only through an explicit rollback
configuration. There is no dual publish and no automatic fallback.

Because current API reads remain on legacy repositories through `0.2.1.4`, the
production Worker remains explicitly configured for `legacy` during
`0.2.1.3`. The `v2` path is exercised with an isolated acceptance Worker and
becomes the production selection only together with the read/repository cutover
in `0.2.1.5`. Enabling `v2` writes earlier would leave legacy reads stale and is
therefore outside this milestone's rollout.

```text
Worker recovery transaction
  -> recovery-state mutation + immutable fact_outbox_v2 rows
  -> unified batch selected by count or publish deadline
  -> POST /internal/v2/automation-facts
  -> strict transport and timestamp validation
  -> internal grouping by automation_id
  -> scope, sequence, revision and lineage validation
  -> one Core transaction per automation group
  -> per-automation acknowledgement or stable failure
  -> selective Worker acknowledgement
```

Batching never creates separate queues for fact kinds or automation types. The
Worker selects ready rows from one outbox. Core groups rows by `automation_id`
only as an internal ordering and transaction mechanism.

## Shared contract boundary

The transport contract lives in `sentinel_contracts.trading_facts`. Worker
imports only this shared contract. Core converts validated payloads explicitly
to the SQLAlchemy-independent draft models introduced by `0.2.1.2`; Worker
never imports `moex_sentinel.domain` or any Core repository/ORM module.

One immutable envelope represents one typed fact. Its required fields are:

- `event_id`: globally unique UUID string;
- `user_broker_id`: scope copied from the authoritative Core command;
- `automation_id`;
- `sequence_number`: strictly increasing within one automation;
- `expected_revision`;
- `fact_kind`: discriminator for the payload union;
- `payload`: the exact type selected by `fact_kind`, with extra fields forbidden;
- `safe_message`: bounded diagnostic text without arbitrary driver data;
- `occurred_at`: event time in UTC at millisecond precision.

Core assigns `received_at` at ingress, also in UTC at millisecond precision.
Every accepted timestamp must satisfy `microsecond % 1000 == 0`. Serialization
uses an explicit UTC offset. Retry preserves the original `event_id`,
`occurred_at`, `sequence_number` and payload.

There is no global auto-incrementing business ID. Timeline ordering is:

- global audit/timeline: `(occurred_at, event_id)`;
- one automation: `(sequence_number, event_id)`;
- ingestion diagnostics may additionally use `received_at` but cannot reinterpret
  business order from arrival order.

Pagination uses a composite cursor derived from the same ordering keys.

## Runtime command projection prerequisite

Core must create the target `_v2` automation aggregate before it can validate
the first runtime fact. History migration does not run until `0.2.1.4`, so a
`v2` command claim performs an idempotent projection of that one authoritative
legacy automation and strategy into the already migrated reference scope. The
projection resolves `user_broker_id` and the target broker-instrument ID through
`legacy_id_map`, preserves the automation ID, current revision and current
sequence, and returns both target identifiers in the Worker command.

This projection is not history migration and does not copy decisions, orders,
executions, lots, audit rows or legacy outbox events. A legacy command claim
does not create the projection. Missing or ambiguous reference mappings fail
the `v2` claim safely instead of falling back to the legacy ingress.

## Runtime fact kinds

The discriminated union contains only facts that Worker produces or confirms:

- `AUTOMATION_STATE_CHANGED`;
- `TRADE_DECISION_RECORDED`, including `WAIT` and `NO_ACTION`;
- `BROKER_ORDER_RECORDED`;
- `BROKER_ORDER_STATE_CHANGED`;
- `TRADE_EXECUTION_RECORDED`;
- `POSITION_CYCLE_UPDATED`;
- `POSITION_LOT_OPENED`;
- `EXECUTION_LOT_ALLOCATED`;
- `TRADE_AUDIT_RECORDED`.

Broker configuration, instrument catalogs, strategy templates, strategy
configuration commands, fee profiles and analytical snapshots remain
Core-owned and do not enter Worker fact outbox. Automation creation and
strategy changes remain Core commands. Worker publishes only observed runtime
state transitions and business facts.

Each fact payload contains the complete immutable or aggregate value required
by the corresponding `0.2.1.2` repository method. The Core mapping is explicit:
no repository receives an untyped dictionary and no `fact_kind` branch silently
ignores fields.

## Worker outbox and recovery journal

Worker adds `fact_outbox_v2`. A row stores the envelope fields, typed JSON
payload, delivery state, retry count, next retry time and creation/update
timestamps. The shared Pydantic contract validates the row before insert and
again before HTTP publication.

One outbox row contains one fact. If one Worker operation produces several
related facts, it creates several immutable rows with consecutive sequence
numbers. The recovery-state mutation and every row produced by it commit in one
SQLite transaction. A failure rolls back both state and outbox rows.

The publisher uses one ready-row query ordered by `(occurred_at, event_id)` and
limited by configured batch size. Publication starts when either:

- the ready-row count reaches the batch limit; or
- the configured publish deadline expires while at least one row is ready.

The query does not partition by automation or fact kind. A batch may therefore
contain arbitrary interleaving of automations and fact kinds.

An acknowledgement removes or marks delivered only the accepted continuous
prefix for the acknowledged automation. Rows not acknowledged remain unchanged.
Non-terminal broker recovery rows are independent of outbox delivery: they may
be removed only after Core acknowledgement and confirmed terminal broker state.

New runtime writes use only `fact_outbox_v2`. Existing legacy outbox/history
rows remain untouched in `0.2.1.3`; `0.2.1.4` migrates and reconciles them.

## Core ingress processing

The fact route is `POST /internal/v2/automation-facts`. Two supporting routes,
`POST /internal/v2/automation-commands` and
`POST /internal/v2/automation-statuses`, project claimable commands and read
authoritative shadow status respectively. The views perform transport parsing
only and delegate to application usecases. The fact and status usecases do not
call legacy trading-fact repositories; only command projection reads the
authoritative legacy automation until cutover.

Processing has two levels:

1. Validate the complete HTTP payload as a strict discriminated union. A
   malformed envelope, invalid timestamp precision or mismatched payload kind
   returns `422` and writes nothing.
2. Group valid envelopes by `automation_id`. Sort every group by
   `sequence_number`, then process one group in one
   `TradingFactsShadowUnitOfWork` transaction.

For each group Core:

- verifies all envelopes carry one matching `user_broker_id`;
- verifies the scope against the authoritative Core automation;
- recognizes already accepted exact retries before advancing state;
- requires a continuous sequence after the current accepted sequence;
- requires every new event's `expected_revision` to equal the revision current
  immediately before that event; an event that changes the automation aggregate
  increments the revision, while an immutable supporting fact keeps it unchanged;
- maps each typed payload to its draft and aggregate repository operation;
- replaces the mutable broker-order aggregate on an order-state transition and
  decrements the referenced position-lot aggregate when accepting an allocation;
- persists the accepted envelope with the corresponding facts;
- commits facts, envelope and automation/cycle aggregate updates once.

Any group failure rolls back that group only. Other automation groups in the
same HTTP request remain independently eligible for commit. Execution order
between groups is not a business ordering guarantee.

## Idempotency, errors and acknowledgements

An exact retry has the same envelope identity, sequence and normalized payload.
It succeeds without reapplying financial effects. Reusing an identity with
different content is a stable conflict.

The response contains one result per automation:

- success: `automation_id`, `accepted_through_sequence`, `current_revision` and
  accepted or exactly-retried `event_id` values;
- failure: `automation_id`, stable error code, offending event IDs and sequence
  numbers, plus `retryable`.

Safe codes distinguish validation, scope, sequence gap/conflict, revision
conflict, fact identity conflict, lineage/state conflict and temporary Core
failure. Responses and logs never include a full payload, DB driver message or
connection detail.

Worker retry policy is:

- network failure, timeout or `5xx`: repeat the unchanged batch using bounded
  exponential backoff and jitter;
- successful per-automation acknowledgement: acknowledge only its continuous
  accepted prefix;
- non-retryable sequence, revision, scope or lineage failure: do not blind
  retry; request authoritative automation status and place the affected
  automation in `HOLD` if safe continuation cannot be proven;
- retryable group failure in a partially successful HTTP response: retain only
  the failed group's unacknowledged rows for retry.

## Rollout and rollback

Deployment order is:

1. verify Core is at Alembic head `a0f212000001` and apply the Worker SQLite
   migration that creates `fact_outbox_v2`;
2. deploy Core with the new endpoint while leaving the legacy endpoint active;
3. keep the production Worker explicitly configured with
   `FACT_INGRESS_VERSION=legacy` while current reads remain legacy-owned;
4. deploy an isolated acceptance Worker with explicit
   `FACT_INGRESS_VERSION=v2` and exercise smoke, process restart, exact retry,
   partial batch success and Core outage recovery;
5. select `v2` for production only in the coordinated ingress/read cutover at
   `0.2.1.5`, retaining the legacy pair for manual rollback until acceptance.

Rollback selects the legacy Worker/Core pair and legacy ingress explicitly. It
does not copy `v2` rows into legacy tables, acknowledge unaccepted `v2` rows or
delete the new outbox. Returning forward to `v2` republishes the original
immutable rows.

## Testing strategy

Contract tests cover every fact kind, discriminator mismatch, forbidden extra
fields, frozen envelopes, UUID identity, UTC normalization and millisecond
precision.

Worker tests prove:

- recovery-state and multi-row outbox atomicity;
- consecutive sequence allocation under one SQLite transaction;
- one unified mixed-automation queue;
- batch publication by count and by deadline;
- persistence across process restart;
- immutable retry and selective per-automation acknowledgement;
- retention of non-terminal recovery journal rows;
- no dual publish or automatic legacy fallback.

Core tests prove:

- strict transport validation produces no writes on `422`;
- mixed-automation grouping is internal only;
- sequence continuity, revision, user-broker scope and lineage validation;
- exact retries are successful and conflicting retries are rejected;
- one failed automation transaction does not roll back accepted peers;
- no `v2` ingress usecase writes to legacy repositories;
- acknowledgements contain safe, stable fields only.

PostgreSQL end-to-end acceptance runs Worker SQLite through HTTP `v2` into Core
PostgreSQL and verifies envelope, decision/order/execution lineage, aggregate
effects and selective acknowledgement. Architecture tests reject Worker imports
from Core domain/ORM/repositories and reject production `v2` imports of legacy
fact repositories.

Final gates are full pytest regression, PostgreSQL integration, Ruff, Black,
strict mypy, frontend tests and production build.

## Subsequent stages and mandatory legacy cleanup

`0.2.1.3` ends after new runtime facts use and pass the `v2` path. It does not
claim history migration or read cutover.

- `0.2.1.4` migrates existing Worker/Core trading history and legacy outbox,
  performs deterministic ID mapping and reconciliation, and does not acknowledge
  a source row before its target fact is verified.
- `0.2.1.5` switches repository/API reads and remaining writes to the normalized
  model, runs full reconciliation and operational acceptance.
- `0.2.1.6` is an explicit cleanup milestone after separate approval and a
  rollback backup checkpoint. It removes legacy trading tables and migrations'
  temporary compatibility surfaces, old ORM models, domain/transport DTOs,
  repositories, services, endpoints, composition wiring, feature flags,
  adapters, fixtures, tests and obsolete documentation.

The detailed `0.2.1.6` deletion inventory is prepared after `0.2.1.5`
reconciliation because only the accepted cutover graph can distinguish obsolete
compatibility code from intentional migration and rollback artifacts.

`0.2.1.6` must include an inventory of legacy symbols before deletion and
repository-wide `rg` assertions proving zero unintended references afterwards.
It must preserve only intentionally retained historical migration evidence and
rollback artifacts outside active runtime code. Deletion is not bundled into
the `0.2.1.4` data-copy transaction or the `0.2.1.5` cutover deployment.

## Definition of Done

- the shared strict discriminated contract covers all approved runtime fact
  kinds;
- every envelope has a UUID, scoped identity, per-automation sequence and UTC
  millisecond timestamp;
- Worker recovery changes and their immutable `v2` outbox facts are atomic;
- batching is controlled only by count or deadline and uses one unified queue;
- Core validates and writes each automation group in one isolated transaction;
- exact retry cannot duplicate financial effects;
- partial success returns safe per-automation acknowledgements and failures;
- Worker uses only `v2` for new runtime facts, with no dual publish or automatic
  fallback;
- history, read cutover and deletion remain isolated in `0.2.1.4`–`0.2.1.6`;
- complete local, PostgreSQL and frontend quality gates pass;
- no Git commit is created.
