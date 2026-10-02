# Clean-Slate Typed Runtime Cutover Design

## Status

- Date: 2026-08-13
- Milestone: replacement for `0.2.1.4–0.2.1.6`
- Priority: CRIT
- Review state: approved by the user section by section
- Decision: discard historical application data, make the normalized typed flow
  the only baseline implementation and bootstrap current broker positions
- Constraint: no Git commits

## Context

The project currently has two parallel implementations:

- legacy Core tables, repositories and `/internal/automaton/*` event ingress;
- normalized `_v2` fact tables, repositories and `/internal/v2/*` typed ingress.

It also stores mutable strategy values in template, per-automation and
per-instrument tables. Production still selects the legacy flow through
`FACT_INGRESS_VERSION`, while the normalized flow is treated as an optional v2
path.

Historical migration is no longer required. Existing Core and Worker data may
be discarded after a controlled shutdown and acceptance checkpoint. Open broker
positions are authoritative at the new start boundary and must be adopted into
the clean system without reconstructing earlier decisions, orders, executions,
P&L or commissions.

## Goals

- Run one typed Worker-to-Core flow with baseline names and no legacy/v2 switch.
- Remove every legacy repository, ORM model, endpoint, DTO, usecase, composition
  branch, test adapter, migration/replay tool and superseded document.
- Replace the current Alembic chain with one clean baseline for empty databases.
- Keep strategy logic in code and load one immutable parameter set from
  environment variables at Worker startup.
- Remove all mutable strategy storage and per-automation overrides.
- Automatically create and activate automations for broker positions found
  after a new broker connection is configured.
- Start new history, realized P&L and commission accounting at the cutover
  boundary.
- Prove the result by running the Worker against open sandbox positions, not
  only by schema or unit-test acceptance.

## Non-goals

- Migrating any historical Core or Worker row.
- Preserving historical P&L, commissions, LIFO attribution, audit or timeline.
- Supporting an in-place upgrade of an old database.
- Supporting legacy rollback, dual publish or automatic ingress fallback.
- Adding a SELL signal based only on trend or moving-average crossover.
- Supporting per-broker, per-instrument or per-automation strategy settings.
- Changing strategy parameters without restarting the Worker.
- Supporting real trading or a production broker endpoint.

## Cutover safety boundary

“Stop trading” is a two-step quiescence process:

1. Disable creation of new BUY/SELL intents while keeping the existing order
   supervisor and broker reconciliation running.
2. Wait until every submitted or uncertain order reaches `FILLED`, `CANCELLED`,
   `REJECTED` or `EXPIRED`, then stop the Worker completely.

No database or volume reset is allowed while a non-terminal or ambiguous order
remains. Filled orders are reflected in the broker positions that become the
new authoritative starting state.

The old Core and Worker volumes are first detached and retained read-only during
acceptance. Their exact names are resolved before deletion. Permanent deletion
is a separate destructive operation requiring explicit confirmation immediately
before execution.

## Target architecture

```text
Environment
  -> immutable StrategySettings at Worker startup

Broker API
  -> account, instruments, open positions and live order state
  -> bootstrap open positions

Core PostgreSQL
  -> one normalized baseline schema
  -> automation aggregates and typed immutable facts

Worker SQLite
  -> command cache, recovery state, intents, executions, lots, allocations
  -> one typed fact outbox

Worker typed outbox
  -> POST /internal/automation-facts
  -> Core transaction per automation
  -> selective continuous-prefix acknowledgement
```

Terms such as `V2`, `_v2`, `legacy`, `FACT_INGRESS_VERSION` and compatibility
fallbacks must not remain in production names or runtime branches. Historical
design documents may mention them only when clearly marked as superseded.

## Baseline data model

The normalized shadow schema becomes the only schema. Tables, ORM classes,
repositories, ports and contracts lose their `V2`/`_v2` suffixes.

The baseline keeps:

- user broker and scoped instrument catalog;
- automation aggregate, position cycle and current state;
- immutable decisions, broker orders and order events;
- executions, lots and execution-to-lot allocations;
- typed automation fact envelopes and trade audit;
- portfolio and valuation snapshots needed by the new runtime;
- migration-independent system settings, heartbeat and schema metadata;
- Worker-local cache, intents, recovery markers and one typed outbox.

The baseline removes:

- all legacy Core trading/reference tables;
- legacy Worker event outbox and legacy audit delivery path;
- `strategy_templates`;
- `automation_strategies` and `automation_strategies_v2`;
- `instrument_strategy_defaults_v2`;
- all data-migration bookkeeping that exists only to map old identifiers;
- all old Alembic revisions, replaced by one empty-database baseline revision.

Automation rows do not store mutable strategy settings. Decision and order facts
store the strategy code, version and immutable parameter snapshot actually used
for the operation. This snapshot is audit evidence and is never read back as
runtime configuration.

## Strategy configuration

The algorithm remains code-owned. One `StrategySettings` object is constructed
from environment variables at Worker startup and injected through the decision
pipeline. It is immutable and shared by all automations. Environment changes
apply to all automations after Worker restart.

Required settings and defaults:

| Environment variable | Default | Meaning |
|---|---:|---|
| `STRATEGY_BUY_ORDER_LOTS` | `1` | Lots in every initial or averaging BUY |
| `STRATEGY_STOP_LOSS_PERCENT` | `5` | Full loss-exit threshold |
| `STRATEGY_TAKE_PROFIT_PERCENT` | `6` | Full profit-exit threshold |
| `STRATEGY_AVERAGING_STEP_PERCENT` | `0.5` | Price movement used by BUY setup |
| `STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT` | `0.5` | Minimum partial-profit target |
| `STRATEGY_PARTIAL_SELL_PERCENT` | `25` | Share of the position sold partially |
| `STRATEGY_MAX_PARTIAL_SELL_STEPS` | `3` | Maximum partial-profit steps |
| `STRATEGY_ORDER_TTL_SECONDS` | `10` | Order lifetime |
| `STRATEGY_ORDER_RETRY_LIMIT` | `3` | Broker order retry limit |
| `STRATEGY_CORE_RETRY_LIMIT` | `5` | Core delivery retry limit |
| `STRATEGY_ENABLED` | `true` | Global strategy enable switch |

Worker startup fails before any broker or trading action when a value is
missing after default resolution, unparsable or outside its validation range.
`STRATEGY_BUY_ORDER_LOTS` and TTL are positive integers; retry counters and
partial-step count are non-negative integers; percentage values are greater
than zero and no greater than 100.

Currency belongs to the broker instrument/account context and is not a strategy
setting.

The following fields are removed from contracts, schemas, database models,
audit payloads, API and UI:

- `max_position_amount`;
- `minimum_free_cash`;
- `initial_order_amount`;
- `max_averaging_steps`;
- `averaging_order_lots`.

`STRATEGY_BUY_ORDER_LOTS` replaces `averaging_order_lots` for both initial and
subsequent BUY orders. There is no limit on total position value or number of
averaging steps.

## Strategy runtime behavior

Every decision uses the same ordered flow:

1. Fail-safe guards verify Core, market state, trading status, commission data
   and absence of an active intent.
2. Existing stop-loss and take-profit rules have priority.
3. Existing partial-profit rules run without semantic expansion.
4. BUY evaluation uses the last buy price, configured step, trend, moving
   averages, pending low, reversal confirmation and candle cooldown.
5. A downtrend or moving-average condition that does not permit a BUY returns
   `WAIT`; it does not create a new SELL signal.
6. An eligible BUY requests exactly `STRATEGY_BUY_ORDER_LOTS`.
7. Immediately before durable intent creation, the materialization boundary
   atomically checks current account free cash minus reservations for other
   active and same-batch BUY intents, including estimated commission.
8. Sufficient cash creates the durable intent. Insufficient cash returns
   `WAIT / INSUFFICIENT_FREE_CASH` without an intent or broker call.

The cash check remains at both decision visibility and final materialization,
but the materialization check is authoritative. No minimum balance is retained.
The system may buy again on any later eligible tick when cash becomes available.

The old entry-budget validation service and averaging-capacity service are
deleted. Free-cash reservations remain because they prevent concurrent BUY
decisions from overspending the same account snapshot.

## API and UI cleanup

Core commands contain automation identity, scoped broker/instrument/account,
state, revision, sequence and execution metadata, but no strategy values.
Worker combines the command with its injected `StrategySettings`.

The only Worker/Core endpoints are:

- `POST /internal/automation-commands`;
- `POST /internal/automation-statuses`;
- `POST /internal/automation-facts`;
- baseline heartbeat, broker-connection and business-audit endpoints under the
  same `/internal/automaton` ownership boundary where they remain necessary.

There are no `/internal/v2/*` or legacy event endpoints.

Remove:

- CRUD `/api/strategy-templates`;
- PATCH `/api/trading-automations/{id}/strategy`;
- optional strategy payload on automation creation;
- strategy template and automation-strategy usecases/services/repositories;
- `/strategies` route, navigation and editable strategy UI;
- frontend API types and methods for strategy editing.

Read APIs may expose `strategy_code`, `strategy_version` and the current
effective environment-derived settings for diagnostics, but they cannot accept
or persist changes.

## Open-position bootstrap

Bootstrap starts only after the user manually configures a broker connection in
the clean system. The successful instrument-catalog synchronization operation
invokes an `AdoptBrokerPositionsUsecase` after its catalog transaction commits.
The adoption usecase reads every account position through the broker portfolio
port and applies the idempotent flow below. Repeating catalog synchronization
repeats adoption safely and discovers positions opened outside this application.

For every open broker position:

1. Resolve the scoped instrument and currency from the broker catalog.
2. Verify that position quantity is a positive whole number of lots and average
   price is positive.
3. Verify that there are no active, non-terminal or uncertain orders for the
   account/instrument.
4. Idempotently create one automation when none exists.
5. Create one initial reconciled position cycle and one synthetic reconciled lot
   using the exact broker quantity and average price.
6. Set pre-cutover realized P&L and commissions to zero. Calculate invested
   amount from quantity, lot size and average price.
7. Persist typed bootstrap facts and the strategy code/version used by the new
   runtime.
8. Load market, commission and recovery state.
9. Keep the automation in `HOLD` with reason `BOOTSTRAPPING` until every check
   and fact acknowledgement succeeds, then move it to `IN_WORK`. No separate
   `BOOTSTRAPPING` state is added to the state machine.

Repeated bootstrap must not create another automation, cycle or lot. A broker
position with an active/uncertain order, invalid quantity, missing price,
missing instrument mapping or unavailable commission profile remains in HOLD
with a stable safe reason code. No BUY or SELL intent is allowed before the
bootstrap transaction and Core acknowledgement complete.

Positions discovered after the initial cutover use the same idempotent adoption
flow. A zero broker position is not adopted.

## Legacy code removal scope

Cleanup is repository-wide, not limited to database tables. Remove or replace:

- legacy and v2 ORM names and exports;
- legacy repositories and shadow-only `*V2Repository` naming;
- legacy automation-event ingress and serializers;
- legacy command/status endpoints and v2-prefixed typed endpoints;
- fact-ingress selection flags and both sides of the compatibility branch;
- legacy Worker outbox models, writers, retry and acknowledgement code;
- reference/history migration domain, service, usecase, repository and CLI;
- database-transfer and history-replay Compose profiles after clean cutover;
- migration-only ID mapping and source reconciliation code;
- strategy storage and entry-budget/capacity code;
- obsolete tests, fixtures, frontend clients and documentation;
- temporary compatibility aliases and comments for old states or contracts.

The typed implementations become baseline implementations instead of wrappers
around old ones. Production source and active API/OpenAPI must contain no
behavioral fallback to deleted structures.

## Operational cutover

1. Put trading into quiesce mode: no new intents, existing supervision active.
2. Wait for all broker orders to become terminal and verify no uncertain intent.
3. Stop Worker and Core.
4. Record exact old Core and Worker volume names, detach them and retain them
   read-only for the acceptance window.
5. Deploy the clean code and create new empty Core and Worker volumes.
6. Apply the one baseline schema.
7. Start Core without Worker and configure the broker manually.
8. Synchronize accounts and instrument catalog.
9. Run idempotent open-position bootstrap.
10. Start Worker with validated environment strategy settings.
11. Verify all adopted positions reach `IN_WORK` and runtime iterations use the
    single typed flow.
12. Exercise a safe sandbox scenario that proves WAIT and, when market/cash
    conditions permit, typed intent/fact delivery.
13. After acceptance, request explicit destructive confirmation and delete the
    detached old volumes and obsolete local database artifacts.

There is no historical data rollback. Before permanent deletion, code rollback
may reattach the detached volumes with the previous application version. After
deletion, rollback creates another empty system and repeats bootstrap from the
current broker state.

## Error policy

- Invalid environment configuration: Worker startup fails before trading.
- Non-terminal/uncertain order during cutover: reset is blocked.
- Invalid broker position: automation remains HOLD; no synthetic correction.
- Missing instrument/account mapping: bootstrap fails safely for that position.
- Missing commission or market state: automation remains HOLD/WAIT.
- Insufficient free cash: `WAIT / INSUFFICIENT_FREE_CASH`; no intent.
- Typed fact rejection or incomplete acknowledgement: no activation and no
  fallback endpoint.
- Duplicate bootstrap delivery: exact idempotent success.
- Conflicting bootstrap identity or values: stable conflict and HOLD.

Errors and logs contain stable codes and safe metadata only. Broker credentials,
connection strings and raw provider payloads are never logged.

## Testing and acceptance

Required automated coverage:

- `StrategySettings` environment defaults, overrides and startup validation;
- absence of removed budget and averaging-limit fields from contracts;
- initial and averaging BUY use `STRATEGY_BUY_ORDER_LOTS`;
- unlimited sequential averaging when signals and cash permit;
- trend/moving-average rejection returns WAIT and never SELL;
- authoritative cash reservation under concurrent/same-batch BUY decisions;
- insufficient cash creates no durable intent;
- stop-loss, take-profit and partial-profit regression;
- bootstrap exact broker quantity/average price and zero historical P&L/fees;
- bootstrap idempotency and HOLD failure cases;
- one typed Worker outbox and one Core ingress path;
- normalized baseline migration on empty SQLite/PostgreSQL;
- architecture checks proving no active legacy/v2 repository or endpoint path;
- frontend route/API cleanup;
- full backend, frontend, PostgreSQL and Compose regression.

Operational acceptance is mandatory:

- clean `docker compose up` on new volumes;
- one baseline schema with no `_v2` tables;
- broker configured manually after reset;
- every valid open sandbox position automatically represented by one
  automation and one reconciled initial lot;
- initial historical realized P&L and commissions are zero;
- Worker reaches `IN_WORK` and continues runtime iterations;
- runtime publishes and acknowledges typed facts without legacy calls;
- insufficient cash produces WAIT;
- an eligible funded BUY creates exactly `STRATEGY_BUY_ORDER_LOTS` lots;
- exit rules remain operational;
- repeat bootstrap and process restart remain idempotent.

Passing schema tests without the open-position runtime smoke does not complete
the milestone.

## Definition of Done

- Historical migration is cancelled and documented as intentionally discarded.
- One normalized baseline schema and one typed runtime path remain.
- All legacy/v2 compatibility code and mutable strategy storage are removed.
- Strategy settings come only from validated Worker environment configuration.
- Open broker positions bootstrap automatically and idempotently.
- Old P&L, commissions and history are not reconstructed.
- Full automated gates pass.
- Production-like sandbox Compose runs the Worker with adopted open positions
  and verified runtime trading behavior.
- Old volumes are deleted only after separate explicit destructive confirmation.
- No Git commit is created by the coding agent.
