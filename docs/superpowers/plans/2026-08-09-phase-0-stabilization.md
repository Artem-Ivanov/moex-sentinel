# Phase 0 Regression Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close milestones `0.1.4` and `0.1.5` by adapting Pydantic-era interfaces without changing business behavior and restoring a zero-failure regression baseline.

**Architecture:** Production DTOs remain strict frozen Pydantic models. Tests use their public Pydantic copy and validation APIs, while `BrokerRuntimeBundle` becomes an explicit resource-owning DI class because it is a lifecycle component rather than a DTO. The only semantic test update records the already documented distinction between `WAIT(NO_THRESHOLD)` and `NO_ACTION(NO_THRESHOLD)`.

**Tech Stack:** Python 3.12, Pydantic 2, pytest, Ruff, Black, SQLAlchemy, asyncio.

**Execution status:** completed. Final regression: `533 passed`, `0 failed`.
Static Protocol mismatches discovered by the additional mypy review are recorded
as prerequisite milestone `0.2.0`; they were not hidden or expanded into this
Pydantic stabilization scope.

## Global Constraints

- Do not create commits.
- Do not change trading thresholds, rule ordering, persistence behavior, API payloads, or lifecycle transitions.
- Test business conditions remain unchanged except the explicitly approved `NO_ACTION` to `WAIT(NO_THRESHOLD)` correction in task 5.
- Production DTO validation must not be weakened for test doubles.
- Do not introduce `@dataclass` into `src`.
- Run the full regression after `0.1.4` and again after `0.1.5`.

---

## File map

- `tests/sentinel_contracts/test_business_audit.py` — Pydantic frozen-model contract.
- `tests/test_compose_config.py` — single backend worker configuration contract.
- `tests/trading_automaton/storage/test_local_repository.py` — immutable storage DTO copies and persistence invariants.
- `tests/trading_automaton/services/test_position_batch_scheduler_service.py` — valid hydrated-state fixtures and parallel scheduling behavior.
- `src/trading_automaton/composition.py` — resource-owning `BrokerRuntimeBundle` and its narrow ports.
- `tests/trading_automaton/test_streaming_composition.py` — bundle cleanup and exception-chaining behavior.
- `tests/trading_automaton/services/test_runtime_service.py` — post-fill decision semantics.
- `docs/trading-strategy.md` — normative `WAIT`/`NO_ACTION` distinction.
- `docs/phase-0-trading-service-refactor.md` — milestone status and regression evidence.
- `pyproject.toml` — explicit UV source for the broker SDK and the existing `dev` extra.
- `uv.lock` — reproducible Python 3.12 application and quality-tool dependency graph.

### Task 0: Make the Python 3.12 UV quality environment reproducible

**Files:**
- Modify: `pyproject.toml`
- Create: `uv.lock`

**Interfaces:**
- Consumes: project extra `dev`; T-Bank package index for `t-tech-investments`.
- Produces: `uv sync --extra dev` environment containing pytest, Ruff, and Black.

- [ ] **Step 1: Preserve the missing-tool evidence**

Run:

```bash
test -x .venv/bin/python
test ! -x .venv/bin/ruff
test ! -x .venv/bin/black
```

Expected: all three checks exit zero for the current incomplete environment.

- [ ] **Step 2: Declare the explicit SDK package source**

Add to `pyproject.toml`:

```toml
[[tool.uv.index]]
name = "tbank"
url = "https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple"
explicit = true

[tool.uv.sources]
t-tech-investments = { index = "tbank" }
```

Do not place broker tokens or credentials in project configuration.

- [ ] **Step 3: Synchronize the declared dev environment**

Run:

```bash
uv sync --extra dev
```

Expected: UV creates or updates `uv.lock` and installs Python 3.12 application and
dev dependencies, including `t-tech-investments`, pytest, Ruff, and Black.

- [ ] **Step 4: Verify the toolchain**

Run:

```bash
uv run python --version
uv run pytest --version
uv run ruff --version
uv run black --version
```

Expected: Python reports 3.12 and every quality command exits zero.

### Task 1: Adapt immutable and storage tests to public Pydantic APIs

**Files:**
- Modify: `tests/sentinel_contracts/test_business_audit.py`
- Modify: `tests/test_compose_config.py`
- Modify: `tests/trading_automaton/storage/test_local_repository.py`

**Interfaces:**
- Consumes: Pydantic `ValidationError`; `BaseModel.model_copy(update={...})`; `uvicorn.run(workers=1)` configuration.
- Produces: the same immutability, atomicity, idempotency, conflict-detection, and single-worker assertions through current public interfaces.

- [ ] **Step 1: Reproduce the 16 interface failures**

Run:

```bash
uv run python -m pytest \
  tests/sentinel_contracts/test_business_audit.py \
  tests/test_compose_config.py \
  tests/trading_automaton/storage/test_local_repository.py -q
```

Expected: 16 failures matching `FrozenInstanceError`, obsolete `--workers` text,
and missing `repository_module.replace`.

- [ ] **Step 2: Assert Pydantic immutability without changing the invariant**

Replace the dataclasses exception import and assertion with:

```python
from pydantic import ValidationError

with pytest.raises(ValidationError, match="Instance is frozen") as raised:
    event.message = "changed"  # type: ignore[misc]
assert raised.value.errors()[0]["type"] == "frozen_instance"
```

- [ ] **Step 3: Assert the actual single-worker entrypoint interface**

Replace the two CLI-token assertions with:

```python
assert "workers=1" in backend_entrypoint.replace(" ", "")
```

This preserves the DoD that exactly one backend process is configured.

- [ ] **Step 4: Replace every obsolete immutable-copy call**

Convert calls of this form:

```python
repository_module.replace(value, field=new_value)
```

to the Pydantic public API:

```python
value.model_copy(update={"field": new_value})
```

For dynamic parameterized fields use:

```python
value.model_copy(update={field: replacement})
```

For multi-field updates use one `model_copy` call with all original values in the
`update` dictionary. Do not add a compatibility `replace` wrapper to production
code.

- [ ] **Step 5: Verify task 1**

Run the command from step 1.

Expected: all selected tests pass and their atomicity/conflict assertions remain
unchanged.

### Task 2: Replace scheduler sentinel objects with valid domain states

**Files:**
- Modify: `tests/trading_automaton/services/test_position_batch_scheduler_service.py`

**Interfaces:**
- Consumes: `HydratedPositionState`, `BrokerPosition`, `IntentHistory`, `TradingCycleState`, and `MarketIndicators`.
- Produces: `hydrated_state(index: int) -> HydratedPositionState` used by scheduler tests.

- [ ] **Step 1: Reproduce the two scheduler failures**

Run:

```bash
uv run python -m pytest \
  tests/trading_automaton/services/test_position_batch_scheduler_service.py -q
```

Expected: two failures where `PositionEvaluationResult.state` rejects `object()`.

- [ ] **Step 2: Add a valid immutable state fixture**

Import DTOs from their owning modules and add:

```python
def hydrated_state(index: int) -> HydratedPositionState:
    return HydratedPositionState(
        position=BrokerPosition(f"i{index}", Decimal("1"), Decimal("100"), Decimal("100"), "RUB"),
        history=IntentHistory(Decimal("100"), 0, 0, Decimal(), 1, Decimal()),
        lots=(),
        cycle=TradingCycleState(f"a{index}", Decimal("99"), None, True, None, NOW),
        indicators=MarketIndicators(Decimal("0.5"), Decimal("0.5"), "TEST", None, None, None, NOW),
    )
```

Use `tuple(hydrated_state(index) for index in range(3))` instead of `object()`
sentinels. Preserve identity assertions (`is` where appropriate) so the test still
proves that prepared state is passed through without rehydration.

- [ ] **Step 3: Verify scheduler behavior**

Run the command from step 1.

Expected: all scheduler tests pass, each eligible position is evaluated once, and
parallel execution still uses one market snapshot.

### Task 3: Refactor `BrokerRuntimeBundle` into a lifecycle DI class

**Files:**
- Modify: `src/trading_automaton/composition.py`
- Verify: `tests/trading_automaton/test_streaming_composition.py`
- Verify: `tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py`

**Interfaces:**
- Consumes: runtime `run/replace_commands/close`, tracking `wait_all`, and session `close` async ports.
- Produces: `BrokerRuntimeBundle` with public `broker_id`, `runtime`, `session`, `tracking`, and `close()`.

- [ ] **Step 1: Reproduce both bundle failures**

Run:

```bash
uv run python -m pytest \
  tests/trading_automaton/test_streaming_composition.py \
  tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py -q
```

Expected: two failures caused by Pydantic concrete-instance validation of lifecycle
test doubles.

- [ ] **Step 2: Define narrow lifecycle ports**

In `composition.py`, replace the Pydantic imports used only by the bundle with
`Protocol` and define:

```python
class BrokerRuntimePort(Protocol):
    async def run(self) -> None: ...
    async def replace_commands(self, commands: tuple[AutomationCommand, ...]) -> None: ...
    async def close(self) -> None: ...

class BrokerSessionClosePort(Protocol):
    async def close(self) -> None: ...

class OrderTrackingWaitPort(Protocol):
    async def wait_all(self) -> None: ...
```

- [ ] **Step 3: Implement the explicit resource holder**

Use a normal class, not `BaseModel` or `@dataclass`:

```python
class BrokerRuntimeBundle:
    __slots__ = ("broker_id", "runtime", "session", "tracking")

    def __init__(
        self,
        broker_id: str,
        runtime: BrokerRuntimePort,
        session: BrokerSessionClosePort,
        tracking: OrderTrackingWaitPort,
    ) -> None:
        self.broker_id = broker_id
        self.runtime = runtime
        self.session = session
        self.tracking = tracking
```

Keep the existing `close()` sequence and exception chaining unchanged.

- [ ] **Step 4: Verify bundle and coordinator behavior**

Run the command from step 1.

Expected: all selected tests pass, including preservation of the tracking failure
when session cleanup also fails.

### Task 4: Close milestone 0.1.4 regression gate

**Files:**
- Verify: `src/**/*.py`
- Verify: `tests/**/*.py`

**Interfaces:**
- Consumes: results of tasks 1–3.
- Produces: a classified full-suite baseline with only the approved semantic task remaining.

- [ ] **Step 1: Verify absence of production dataclasses**

Run:

```bash
rg -n "@dataclass" src
```

Expected: no matches.

- [ ] **Step 2: Run contract and trading suites**

Run:

```bash
uv run python -m pytest tests/sentinel_contracts tests/trading_automaton -q
```

Expected: all tests pass except
`test_immediately_filled_opening_stays_in_work_without_second_order`.

- [ ] **Step 3: Run the full regression**

Run:

```bash
uv run python -m pytest -q
```

Expected: exactly one failure, classified category B, expecting `NO_ACTION` while
the runtime records `WAIT(NO_THRESHOLD)`.

### Task 5: Align the approved post-fill decision test with normative semantics

**Files:**
- Modify: `tests/trading_automaton/services/test_runtime_service.py`
- Modify: `docs/trading-strategy.md`

**Interfaces:**
- Consumes: persisted `DecisionRecord.decision`, `DecisionRecord.reason_code`, and broker submission count.
- Produces: explicit invariant that an active position with available averaging levels and no reached threshold yields `WAIT(NO_THRESHOLD)` without another order.

- [ ] **Step 1: Preserve the red evidence**

Run:

```bash
uv run python -m pytest \
  tests/trading_automaton/services/test_runtime_service.py::test_immediately_filled_opening_stays_in_work_without_second_order -q
```

Expected: failure `NO_ACTION != WAIT` while the submission count assertion passes.

- [ ] **Step 2: Make the approved semantic assertion explicit**

Replace the final assertion with:

```python
decision = repository.list_decisions("automation-1")[-1]
assert decision.decision == "WAIT"
assert decision.reason_code == "NO_THRESHOLD"
```

Keep `broker.calls.count("submit_limit_order") == 1` unchanged.

- [ ] **Step 3: Clarify the normative decision matrix**

Extend the explanation around cases S-17 and S-22 in `docs/trading-strategy.md`:

```markdown
После исполнения входной заявки открытая позиция с доступным уровнем усреднения,
но без достигнутого порога покупки или продажи, относится к S-22 и сохраняет
`WAIT(NO_THRESHOLD)`. `NO_ACTION(NO_THRESHOLD)` применяется только когда новые
уровни усреднения недоступны.
```

- [ ] **Step 4: Verify task 5**

Run the command from step 1, then:

```bash
uv run python -m pytest \
  tests/trading_automaton/services/test_runtime_service.py \
  tests/trading_automaton/services/test_adaptive_scalping_strategy.py \
  tests/trading_automaton/services/test_strategy_selection_matrix.py -q
```

Expected: all selected tests pass and exactly one broker order is submitted in the
post-fill scenario.

### Task 6: Format, run complete acceptance, and close milestones

**Files:**
- Modify: touched Python files through formatters.
- Modify: `docs/phase-0-trading-service-refactor.md`

**Interfaces:**
- Consumes: completed tasks 1–5.
- Produces: green regression evidence and milestone status `[x]` for `0.1`, `0.1.4`, and `0.1.5`.

- [ ] **Step 1: Apply project formatting and import rules**

Run:

```bash
uv run ruff check --config pyproject.toml --fix \
  src/trading_automaton/composition.py \
  tests/sentinel_contracts/test_business_audit.py \
  tests/test_compose_config.py \
  tests/trading_automaton/storage/test_local_repository.py \
  tests/trading_automaton/services/test_position_batch_scheduler_service.py \
  tests/trading_automaton/services/test_runtime_service.py \
  tests/trading_automaton/test_streaming_composition.py
uv run black --config pyproject.toml \
  src/trading_automaton/composition.py \
  tests/sentinel_contracts/test_business_audit.py \
  tests/test_compose_config.py \
  tests/trading_automaton/storage/test_local_repository.py \
  tests/trading_automaton/services/test_position_batch_scheduler_service.py \
  tests/trading_automaton/services/test_runtime_service.py \
  tests/trading_automaton/test_streaming_composition.py
```

Expected: both commands exit zero after applying safe mechanical changes.

- [ ] **Step 2: Run the full regression**

Run:

```bash
uv run python -m pytest -q
```

Expected: zero failures.

- [ ] **Step 3: Verify repository hygiene**

Run:

```bash
rg -n "@dataclass" src
git diff --check
```

Expected: no production dataclasses and no whitespace errors.

- [ ] **Step 4: Record acceptance evidence**

In `docs/phase-0-trading-service-refactor.md`:

- change `0.1`, `0.1.4`, and `0.1.5` to `[x]`;
- record the final passed/failed counts and the commands used;
- state that the next active task is `0.2`.

- [ ] **Step 5: Re-run documentation and full regression after the status edit**

Run:

```bash
uv run python -m pytest tests/test_documentation.py -q
uv run python -m pytest -q
git diff --check
```

Expected: documentation tests and full regression pass with zero failures; diff
contains no whitespace errors.
