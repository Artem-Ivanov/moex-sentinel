# Core Trading Facts Shadow Schema Design

## Status

- Date: 2026-08-12
- Milestone: `0.2.1.2`
- Decision: one atomic schema expansion with aggregate-oriented shadow repositories
- Review state: implemented and verified
- Alembic head: `a0f212000001`
- Next milestone: `0.2.1.3` typed fact ingress
- Implementation plan: [Core trading facts shadow schema](../plans/2026-08-12-core-trading-facts-shadow-schema.md)
- Constraint: no Git commits

## Context

Core already runs on PostgreSQL and the legacy Core SQLite transfer has passed
acceptance. Milestone `0.2.1.1` introduced the user-broker-scoped reference
parents `user_brokers_v2` and `broker_instruments_v2`. Trading history is still
stored and read through legacy tables, while Worker SQLite remains the durable
recovery source for Worker facts that have not yet been migrated.

The next safe expansion is to add the authoritative normalized trading-fact
schema to Core without changing any runtime read or write path. The canonical
entity definitions and field ownership remain in the
[trading data schema migration design](./2026-08-10-trading-data-schema-migration-design.md).
Its [physical shadow ER schema](./2026-08-10-trading-data-schema-migration-design.md#physical-shadow-schema-for-0212)
is the source of truth for relationships in this milestone.

## Goals

- Create all Core trading-fact and supporting analytical tables in one Alembic
  expansion revision after `a0f211000001`.
- Protect user-broker scope, idempotency, ordering and financial lineage with
  named database constraints.
- Add typed, SQLAlchemy-independent records and repository ports for the new
  schema.
- Add SQLAlchemy shadow adapters grouped by aggregate responsibility.
- Prepare one transaction boundary that typed fact ingress can use in
  `0.2.1.3`.
- Keep the complete legacy runtime and API behavior unchanged.

## Non-goals

- Worker-to-Core transport contracts, Worker outbox or recovery-journal changes;
- copying or reconciling legacy trading history;
- registering the new repositories in current API or Worker runtime paths;
- switching reads, writes, dashboards or analytics to the shadow schema;
- deleting, renaming or mutating legacy tables;
- removing the `_v2` suffix;
- implementing cleanup or rollback of migrated business data.

Those changes belong to `0.2.1.3` through `0.2.1.6` in the approved sequence.

## Accepted architecture

The schema is delivered by one atomic Alembic revision. A database either has
the complete `0.2.1.2` shadow graph or none of it; there is no supported
intermediate state with only decisions, orders or executions present.

The persistence adapter is split by aggregate responsibility instead of by one
repository per table:

| Adapter | Owned tables |
|---|---|
| `AutomationFactsShadowRepository` | `trading_automations_v2`, `automation_strategies_v2` |
| `OrderFactsShadowRepository` | `trade_decisions_v2`, `broker_orders_v2`, `broker_order_events_v2`, `trade_executions_v2` |
| `PositionLedgerShadowRepository` | `position_cycles_v2`, `position_lots_v2`, `execution_lot_allocations_v2` |
| `TradingAuditShadowRepository` | `trade_audit_events_v2`, `automation_events_v2` |
| `TradingAnalyticsShadowRepository` | `broker_account_fee_profiles_v2`, `portfolio_snapshots_v2`, `position_valuation_snapshots_v2` |

These adapters are exposed through a `TradingFactsShadowUnitOfWork`. Entering
the unit of work opens one SQLAlchemy session and binds every grouped repository
to it. A successful unit commits once; an exception rolls back the entire unit.
This is unused by production runtime in `0.2.1.2`, but gives `0.2.1.3` an atomic
boundary for one accepted fact batch without redesigning repository ownership.

Domain records, errors and ports do not import SQLAlchemy. Shadow names remain
explicit in adapter classes so they cannot be accidentally substituted for
legacy repositories before cutover.

## Physical schema

### Parent references

Every fact is scoped by `user_broker_id`. Instrument-bearing facts also use a
composite foreign key to
`broker_instruments_v2(user_broker_id, id)`. The migration does not create a
second user-broker or instrument catalog.

Every table that is a composite-FK parent exposes a named unique constraint on
`(user_broker_id, id)`, even though `id` is already its primary key. This makes
scope part of the database relationship rather than only an application filter.

### Trading fact tables

The atomic revision creates:

- `trading_automations_v2` and `automation_strategies_v2`;
- `position_cycles_v2`;
- `trade_decisions_v2`;
- `broker_orders_v2` and `broker_order_events_v2`;
- `trade_executions_v2`;
- `position_lots_v2` and `execution_lot_allocations_v2`;
- `trade_audit_events_v2` and `automation_events_v2`.

The column sets, timestamps, immutable strategy snapshots and fact lineage are
the canonical definitions from the parent schema design. UUIDs are stored using
the project's portable string representation, timestamps use `UTCDateTime`,
money uses the shared precision and scale, and validated payloads use portable
SQLAlchemy JSON. Lifecycle values remain checked strings rather than
database-native enums so SQLite tests and PostgreSQL use the same model shape.

### Supporting analytical tables

The same revision creates:

- `broker_account_fee_profiles_v2`;
- `portfolio_snapshots_v2`;
- `position_valuation_snapshots_v2`.

These rows are scoped analytical inputs or read models. They do not replace an
execution fact and cannot update position inventory independently.

## Integrity rules

All constraints and indexes have stable explicit names so migrations, tests and
error translation do not depend on driver-generated identifiers.

1. Composite foreign keys include `user_broker_id` for every scoped entity
   relationship.
2. A partial unique index allows only one non-closed automation for one scoped
   instrument.
3. A partial unique index allows only one open cycle for one scoped automation.
4. A decision creates at most one order.
5. `(user_broker_id, idempotency_key)` is unique for orders.
6. Scoped non-null external order and execution IDs are unique.
7. `fact_id` is unique in every immutable fact stream where it is present.
8. `automation_events_v2` is unique by both `event_id` and
   `(automation_id, sequence_number)`.
9. `(sell_execution_id, position_lot_id)` is unique for allocations.
10. Quantities, lot counts, monetary values that cannot be negative, revisions
    and sequence numbers have named check constraints.
11. A closed cycle has `closed_at`; an open cycle does not. Terminal timestamps
    and lifecycle values are checked where the rule can be expressed without
    embedding mutable domain workflow in SQL.
12. Historical facts use restrictive foreign keys and are not cascade-deleted.

Partial indexes declare equivalent `sqlite_where` and `postgresql_where`
predicates. PostgreSQL is authoritative, while SQLite remains the fast
repository and migration test dialect.

## Repository contracts

Every method requires `user_broker_id` explicitly; an unscoped `get(id)` or
`list()` is not part of any port. Read methods return detached immutable domain
records, never ORM models.

The aggregate repositories provide only the persistence primitives needed by
future fact ingestion and migration:

- create an automation together with its initial strategy snapshot;
- compare-and-set automation and strategy revisions;
- open, update and close a position-cycle aggregate;
- append immutable decisions, orders, order events and executions;
- create BUY lots and append SELL-to-lot allocations;
- append audit events and accepted automation envelopes;
- upsert scoped fee profiles and append analytical snapshots;
- retrieve individual facts and ordered aggregate history for verification.

Append operations distinguish an exact retry from a conflict. Repeating the
same stable fact identity with identical normalized content returns the existing
record. Reusing that identity with different content raises a typed conflict and
does not mutate the stored row. Generic update or delete methods are not
provided for immutable facts.

The repositories validate obvious cross-scope relationships before flushing.
Database composite foreign keys remain the final authority and protect against
races or adapter defects.

## Transactions and concurrency

The unit of work owns commit and rollback. Grouped repositories only flush and
never commit independently. This permits one future ingress batch to atomically
persist its envelope, decision, order transition, execution, lot allocation,
cycle update and audit links.

Automation revision updates use compare-and-set semantics in the SQL statement.
Zero affected rows is a typed revision conflict. Idempotency is enforced first
by stable lookup and ultimately by named unique constraints, so concurrent
retries cannot apply an execution or allocation twice.

This milestone does not add locking around legacy tables and does not attempt a
dual write. The new repositories are instantiated only by focused tests until
the ingress stage starts.

## Error model

The domain boundary uses typed errors with safe stable codes:

- `TRADING_FACT_NOT_FOUND`;
- `CROSS_SCOPE_RELATION`;
- `FACT_ID_CONFLICT`;
- `ORDER_IDEMPOTENCY_CONFLICT`;
- `AUTOMATION_SEQUENCE_CONFLICT`;
- `AUTOMATION_REVISION_CONFLICT`;
- `INVALID_FACT_STATE`.

Adapters translate known named constraint violations into these errors and
preserve the original exception as the cause. Error messages may contain table,
constraint and generalized entity type, but never payload contents. Unknown
database failures propagate to the existing application error boundary and
roll back the unit of work.

## Migration behavior

The new Alembic revision has `a0f211000001` as its direct parent. Upgrade creates
parents before children, then indexes and remaining constraints. Downgrade drops
only the tables and indexes introduced by `0.2.1.2` in reverse dependency order.
It never modifies the `0.2.1.1` parents or any legacy table.

Acceptance exercises both paths:

- upgrade from `a0f211000001` with populated legacy tables;
- upgrade from an empty database through Alembic head;
- downgrade back to `a0f211000001` while preserving legacy and reference-shadow
  rows;
- second upgrade after downgrade.

## Testing strategy

Implementation follows test-driven development in this order:

1. metadata tests define exact table, column, constraint and index names;
2. migration tests prove upgrade, downgrade and legacy-table preservation on
   SQLite;
3. PostgreSQL integration tests prove partial uniqueness, composite scope FKs,
   JSON/timestamp/numeric behavior and transaction rollback;
4. repository contract tests prove detached records, mandatory scope,
   idempotent retry, conflicting retry, revision compare-and-set and immutable
   append behavior;
5. unit-of-work tests prove one commit and complete rollback across repository
   groups;
6. architecture tests prove legacy services and usecases do not import or
   instantiate shadow repositories;
7. strict mypy, formatting, lint, complete Python regression, PostgreSQL suite,
   frontend tests and production build remain green.

Test fixtures use only synthetic local data. No production or transferred row
values are required for schema acceptance.

## Delivery and rollback

Deployment runs the explicit migration job before any code that needs the new
schema. Because `0.2.1.2` has no runtime consumer, application rollback remains
compatible with the expanded database. A schema downgrade is optional and must
only be run when no `0.2.1.3` fact ingress has started writing the shadow tables.

No legacy volume, SQLite snapshot or PostgreSQL data is deleted. Data transfer
starts only in `0.2.1.4`; repository/API cutover starts only in `0.2.1.5` after
reconciliation passes.

## Definition of Done

- one Alembic revision creates the complete physical shadow schema;
- every scoped relationship has application validation and database protection;
- grouped shadow repositories and the unit of work implement typed ports;
- immutable facts support exact idempotent retry but reject conflicting reuse;
- SQLite and PostgreSQL constraint behavior is covered;
- legacy schema, data and runtime paths remain unchanged;
- migration round-trip and complete project regression pass;
- implementation and acceptance evidence are recorded in the phase documents;
- no Git commit is created.

## Acceptance evidence

Acceptance completed on 2026-08-12:

- complete Python regression: `690 passed`, `6 skipped`, `0 failed`;
- PostgreSQL migration-container suite: `6 passed`;
- Ruff clean, Black clean for `383` files, strict mypy clean for `214` source
  files;
- frontend: `74 passed`; production build completed with `62` transformed
  modules;
- runtime-isolation tests confirm that legacy Core and Worker read/write paths do
  not instantiate the shadow unit of work;
- no trading-history rows were transferred. Data migration remains milestone
  `0.2.1.4` after typed ingress in `0.2.1.3`.
