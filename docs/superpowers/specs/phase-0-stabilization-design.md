# Phase 0 Regression Stabilization Design

## Status

- Date: 2026-08-09
- Decision: approved, option 1
- Scope: milestones `0.1.4` and `0.1.5`
- Constraint: no commits are created; existing test intent and DoD are preserved

## Problem

Milestone `0.1` migrated production DTOs from dataclasses to Pydantic models and
removed `@dataclass` declarations from `src`. The production migration is present,
but the full regression suite is not green:

- 512 tests pass;
- 21 tests fail;
- 20 failures are compatibility gaps left by the DTO migration;
- one failure is a semantic disagreement between `NO_ACTION` and `WAIT` after an
  immediately filled opening order.

The next phase (`0.2`, database facts and ownership) must not start on top of this
red baseline because later failures could not be attributed reliably.

## Considered approaches

### Option 1 — restore the green regression gate first

Close Pydantic compatibility gaps as `0.1.4`, validate the single semantic
disagreement separately as `0.1.5`, and only then continue with `0.2`.

This is the selected approach. It preserves causal isolation and makes the full
regression suite a reliable gate for every following phase.

### Option 2 — fix failures while implementing `0.2` and `0.3`

This creates fewer named tasks but mixes DTO compatibility, database ownership,
and lifecycle changes in one regression delta. It was rejected because a reviewer
could not accept or reject those changes independently.

### Option 3 — accept the red baseline until the end of phase 0

This was rejected because it conflicts with the milestone acceptance rule and
would hide new category C defects among known failures.

## 0.1.4 Pydantic compatibility stabilization (CRIT)

### Boundaries

This task changes compatibility mechanics, fixtures, and a non-DTO composition
holder. It does not change trading decisions, persistence semantics, lifecycle,
or external API payloads.

The local quality gate is executed through UV. Because the existing `.venv` lacks
the declared Ruff and Black dev dependencies and the broker SDK requires its
package index, stabilization also makes the already declared `dev` extra
reproducible through `uv sync --extra dev`.

### Failure classification and resolution

| Area | Failures | Category | Resolution |
|---|---:|---|---|
| Frozen audit DTO | 1 | A | Assert Pydantic `ValidationError` with `frozen_instance`; immutability DoD stays unchanged. |
| Backend worker-count check | 1 | A | Assert the current `uvicorn.run(..., workers=1)` interface instead of an obsolete CLI-token representation. |
| Scheduler prepared-state fixtures | 2 | A | Replace untyped `object()` sentinels with valid `HydratedPositionState` fixtures; keep parallelism and state-identity assertions. |
| Repository immutable DTO copies | 14 | A | Replace removed `dataclasses.replace` compatibility calls with Pydantic `model_copy(update=...)`; keep all atomicity, replay, and conflict assertions. |
| `BrokerRuntimeBundle` test doubles | 2 | A/C boundary | Make the bundle an explicit lifecycle/DI class rather than a Pydantic DTO. Keep typed structural ports and cleanup behavior; do not weaken DTO validation globally. |

`BrokerRuntimeBundle` owns resources and behavior (`close`) and transports no data
across a service boundary. Therefore it is not a DTO and must not inherit the
Pydantic compatibility base. Constructor injection remains explicit.

### Interfaces

The bundle keeps its public construction and fields:

```python
BrokerRuntimeBundle(
    broker_id: str,
    runtime: BrokerRuntimeClosePort,
    session: BrokerSessionClosePort,
    tracking: OrderTrackingWaitPort,
)
```

The ports expose only the asynchronous operations used by `close()`:

```python
class RuntimePort(Protocol):
    async def run(self) -> None: ...
    async def replace_commands(self, commands: tuple[AutomationCommand, ...]) -> None: ...
    async def close(self) -> None: ...

class BrokerSessionClosePort(Protocol):
    async def close(self) -> None: ...

class OrderTrackingWaitPort(Protocol):
    async def wait_all(self) -> None: ...
```

Runtime behavior remains:

1. close the broker runtime;
2. wait for order tracking;
3. always close the SDK session;
4. if tracking and session cleanup both fail, preserve the tracking error as the
   primary exception and chain the session error as its cause.

### DoD

- the 20 category A/C compatibility failures pass;
- no production DTO accepts invalid values merely to satisfy a test double;
- no `@dataclass` declaration is introduced into `src`;
- `uv sync --extra dev` installs the Python 3.12 development environment from
  declared project sources;
- targeted contract, scheduler, storage, composition, and compose tests pass;
- the full regression leaves at most the separately classified `0.1.5` semantic
  failure.

## 0.1.5 Post-fill decision semantics (CRIT)

### Question to validate

After an opening intent is filled and its execution event is applied, the next
iteration must not submit a second order. The current test also requires the
persisted decision kind to be `NO_ACTION`, while the current runtime persists
`WAIT`.

Both decision kinds are non-executable, so the order-count invariant already
holds. The difference matters for audit semantics:

- `WAIT` means a known temporary guard or market condition asks the worker to
  defer action;
- `NO_ACTION` means the strategy evaluated the state and found no threshold or
  state transition requiring action.

### Validation method

1. Trace the second iteration from execution-event application through context
   construction and strategy selection.
2. Record the actual `reason_code` that produces `WAIT`.
3. Compare it with `docs/trading-strategy.md` and the decision matrix tests.
4. Treat the existing runtime test as the DoD unless the documented business
   meaning proves that it is contradictory.
5. If the DoD must change, stop and request a separate explicit approval before
   altering its assertion.

### DoD

- the second iteration never submits another order;
- the selected decision kind and reason code have one documented meaning;
- the runtime test and `docs/trading-strategy.md` express that same meaning;
- the full regression is green: zero failures;
- no trading threshold or strategy ordering changes as part of this task.

## Verification gates

Each task is accepted in this order:

1. targeted failing tests;
2. all `sentinel_contracts` and `trading_automaton` tests;
3. `ruff check --config pyproject.toml --fix` and
   `black --config pyproject.toml` for touched Python paths;
4. full `uv run python -m pytest -q`;
5. `git diff --check`;
6. classification of any remaining failure as A, B, or C.

The implementation does not alter a test's business conditions merely to obtain a
green run. Interface-only adaptations are allowed when they preserve the same
asserted behavior.

## Exit and next phase

After `0.1.4` and `0.1.5` pass the full regression gate, milestone `0.1` returns
to `[x]`. Work then continues in the approved order `0.2 -> 0.3 -> 0.8`.
