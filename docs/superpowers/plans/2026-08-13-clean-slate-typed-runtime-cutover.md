# Clean-Slate Typed Runtime Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить параллельные legacy/shadow реализации одним типизированным baseline-потоком, запускать Worker с едиными настройками стратегии из окружения и автоматически принимать открытые позиции брокера как стартовое состояние новой торговли.

**Architecture:** Core хранит одну нормализованную PostgreSQL-схему и принимает только типизированные факты. Worker хранит recovery-состояние и единый typed outbox в SQLite. Core создаёт автомат для открытой брокерской позиции в `HOLD/BOOTSTRAPPING` и передаёт неизменяемый bootstrap-снимок в команде; Worker атомарно создаёт локальный reconciled-lot и отправляет непрерывную группу фактов, последним из которых переводит автомат в `IN_WORK`. Параметры стратегии живут только в замороженном `StrategySettings`, загружаемом при старте Worker.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, HTTPX, SQLAlchemy 2, Alembic, SQLite, PostgreSQL 16, pytest, Ruff, Black, Vue 3, TypeScript, Vitest, Docker Compose.

## Global Constraints

- Следовать утверждённому дизайну `docs/superpowers/specs/2026-08-13-clean-slate-typed-runtime-cutover-design.md`.
- Исторические строки Core и Worker не мигрировать и не воспроизводить.
- Поддержку обновления существующей БД не реализовывать: единственный допустимый путь — новая пустая PostgreSQL/SQLite БД.
- Не сохранять параметры стратегии в Core, команде автомата, Worker cache или редактируемом API.
- Снимок параметров в решении/заявке — только неизменяемое доказательство использованной конфигурации; runtime никогда его не читает как настройки.
- Все новые временные метки сохранять в UTC с точностью до миллисекунд; для полного порядка использовать `(occurred_at, event_id)`, отдельный auto-increment ID не добавлять.
- BUY всегда запрашивает ровно `STRATEGY_BUY_ORDER_LOTS`; лимита суммы позиции и числа усреднений нет.
- Тренд и скользящие средние могут вернуть только `WAIT`, но не создают новый SELL-сигнал.
- Перед созданием BUY intent обязательно повторно проверить свободные средства с учётом комиссии и уже зарезервированных BUY.
- Bootstrap не создаёт фиктивные исторические решение, заявку или исполнение. Reconciled-lot имеет собственный источник и nullable `buy_execution_id`.
- Не публиковать один факт в два endpoint, не иметь fallback и не оставлять feature flag выбора ingress.
- Не выполнять удаление старых Docker volumes или БД без отдельного подтверждения непосредственно перед разрушительной операцией.
- Не создавать Git commits.
- Mypy не использовать и не возвращать в зависимости/CI. Обязательные Python gates: Ruff, Black и pytest.

## Target Interfaces

```python
class StrategySettings(BaseSettings):
    model_config = SettingsConfigDict(
        frozen=True,
        extra="ignore",
        case_sensitive=False,
    )

    buy_order_lots: int = Field(1, validation_alias="STRATEGY_BUY_ORDER_LOTS", gt=0)
    stop_loss_percent: Decimal = Field(Decimal("5"), validation_alias="STRATEGY_STOP_LOSS_PERCENT", gt=0, le=100)
    take_profit_percent: Decimal = Field(Decimal("6"), validation_alias="STRATEGY_TAKE_PROFIT_PERCENT", gt=0, le=100)
    averaging_step_percent: Decimal = Field(Decimal("0.5"), validation_alias="STRATEGY_AVERAGING_STEP_PERCENT", gt=0, le=100)
    partial_take_profit_percent: Decimal = Field(Decimal("0.5"), validation_alias="STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT", gt=0, le=100)
    partial_sell_percent: Decimal = Field(Decimal("25"), validation_alias="STRATEGY_PARTIAL_SELL_PERCENT", gt=0, le=100)
    max_partial_sell_steps: int = Field(3, validation_alias="STRATEGY_MAX_PARTIAL_SELL_STEPS", ge=0)
    order_ttl_seconds: int = Field(10, validation_alias="STRATEGY_ORDER_TTL_SECONDS", gt=0)
    order_retry_limit: int = Field(3, validation_alias="STRATEGY_ORDER_RETRY_LIMIT", ge=0)
    core_retry_limit: int = Field(5, validation_alias="STRATEGY_CORE_RETRY_LIMIT", ge=0)
    enabled: bool = Field(True, validation_alias="STRATEGY_ENABLED")
```

```python
class BrokerPositionBootstrap(StrictFrozenModel):
    position_cycle_id: UUID
    position_lot_id: UUID
    quantity_lots: int = Field(gt=0)
    average_price: Decimal = Field(gt=0)
    invested_amount: Decimal = Field(gt=0)
    currency: str
    observed_at: MillisecondUtc


class AutomationCommand(StrictFrozenModel):
    automation_id: UUID
    user_broker_id: UUID
    broker_id: UUID
    account_id: str
    external_instrument_id: str
    instrument_id: UUID
    currency: str
    lot_size: int = Field(gt=0)
    min_price_increment: Decimal = Field(gt=0)
    state: AutomationState
    revision: int = Field(gt=0)
    last_sequence_number: int = Field(ge=0)
    resume_requested: bool
    bootstrap: BrokerPositionBootstrap | None = None
```

```python
class PositionLotSource(StrEnum):
    BROKER_EXECUTION = "BROKER_EXECUTION"
    BROKER_POSITION_BOOTSTRAP = "BROKER_POSITION_BOOTSTRAP"


class PositionLotOpenedPayload(StrictFrozenModel):
    position_lot_id: UUID
    position_cycle_id: UUID
    buy_execution_id: UUID | None
    source: PositionLotSource
    original_lots: int = Field(gt=0)
    remaining_lots: int = Field(ge=0)
    entry_price: Decimal = Field(gt=0)
    entry_commission: Decimal = Field(ge=0)
    opened_at: MillisecondUtc
    created_at: MillisecondUtc
    updated_at: MillisecondUtc
```

## File Map

| Область | Файлы |
|---|---|
| Настройки стратегии | `src/trading_automaton/config.py`, `compose.yml`, `.env.example` |
| Общие команды и факты | `src/sentinel_contracts/trading.py`, `src/sentinel_contracts/trading_facts.py` |
| Решения Worker | `src/trading_automaton/domain/dtos.py`, `src/trading_automaton/services/decision*.py`, `src/trading_automaton/services/strategies.py`, `src/trading_automaton/services/runtime_decision_planner.py` |
| Cash reservation | `src/trading_automaton/services/account_cash_reservation.py`, `src/trading_automaton/services/decision_materialization.py` |
| Typed Worker persistence | `src/trading_automaton/storage/models.py`, `src/trading_automaton/storage/fact_outbox.py`, `src/trading_automaton/storage/repository.py` |
| Typed Core ingress | `src/moex_sentinel/services/trading_fact_*.py`, `src/moex_sentinel/storage/repositories/trading_facts_uow.py`, `src/moex_sentinel/views/internal_trading_facts.py` |
| Baseline Core schema | `src/moex_sentinel/storage/models/*.py`, `src/moex_sentinel/storage/repositories/*.py` |
| Bootstrap позиций | `src/moex_sentinel/domain/position_adoption.py`, `src/moex_sentinel/services/position_adoption.py`, `src/moex_sentinel/usecases/position_adoption.py` |
| Каталог и portfolio | `src/moex_sentinel/usecases/instruments.py`, `src/moex_sentinel/services/portfolio_ports.py`, `src/moex_sentinel/adapters/tinvest/portfolio.py`, `src/moex_sentinel/adapters/tinvest/order_execution.py` |
| HTTP/UI cleanup | `src/moex_sentinel/views/automations.py`, `src/moex_sentinel/views/schemas/automations.py`, `frontend/src/api/automations.ts`, `frontend/src/router.ts`, `frontend/src/App.vue` |
| Baseline migration | `alembic/versions/0001_baseline.py`, `src/moex_sentinel/migrations/schema.py`, `compose.yml`, `pyproject.toml` |
| Runbook и gates | `README.md`, `docs/development.md`, `docs/trading-strategy.md`, `docs/clean-slate-cutover.md`, `tests/test_architecture.py`, `tests/test_documentation.py` |

---

### Task 1: Immutable environment strategy settings and strategy-free command contract

**Files:**

- Modify: `src/trading_automaton/config.py`
- Modify: `src/sentinel_contracts/trading.py`
- Modify: `src/sentinel_contracts/trading_facts.py`
- Modify: `tests/trading_automaton/test_config.py`
- Modify: `tests/contracts/test_trading_facts_contract.py`
- Modify: `compose.yml`
- Modify: `.env.example`

**Interfaces:**

- Produces `StrategySettings` exactly as shown in Target Interfaces.
- Replaces `FactAutomationCommand`/`FactAutomationStatus` with baseline `AutomationCommand`/`AutomationStatus` without `strategy`.
- Removes `StrategyValues`, shared legacy `AutomationCommand` and `AutomationEvent` from `sentinel_contracts.trading`.

- [ ] **Step 1: Write failing settings tests**

Add tests for defaults, complete override, frozen mutation, bad decimal, zero/negative integer, percent above 100, retry below zero and boolean parsing:

```python
def test_strategy_settings_use_approved_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in STRATEGY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    settings = StrategySettings()

    assert settings.buy_order_lots == 1
    assert settings.stop_loss_percent == Decimal("5")
    assert settings.partial_sell_percent == Decimal("25")
    assert settings.enabled is True


def test_strategy_settings_reject_invalid_percentage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_STOP_LOSS_PERCENT", "100.01")
    with pytest.raises(ValidationError):
        StrategySettings()
```

- [ ] **Step 2: Write failing command tests**

Assert that `strategy` and every removed field is rejected as an extra field, while `currency` and optional `bootstrap` validate. Assert JSON timestamps end in `Z` and preserve exactly three fractional digits.

- [ ] **Step 3: Run focused tests and confirm RED**

```bash
uv run pytest -q tests/trading_automaton/test_config.py tests/contracts/test_trading_facts_contract.py
```

Expected: imports/validation fail because `StrategySettings`, baseline command names and bootstrap DTO do not exist.

- [ ] **Step 4: Implement settings and contracts**

Add `StrategySettings`; keep transport/runtime settings in `AutomatonSettings`, but remove `fact_ingress_version`. Define `BrokerPositionBootstrap`, `AutomationCommand`, `AutomationStatus` and `AutomationStatusesResult` in `trading_facts.py`. Add `currency` to command context and remove all strategy values from Core transport.

- [ ] **Step 5: Expose approved environment variables**

Set all eleven defaults in `compose.yml` and document them in `.env.example`. Delete `FACT_INGRESS_VERSION`. Do not add Mypy configuration or command.

- [ ] **Step 6: Run focused tests and confirm GREEN**

```bash
uv run pytest -q tests/trading_automaton/test_config.py tests/contracts/test_trading_facts_contract.py tests/test_compose_config.py
```

Expected: all selected tests pass and Compose resolves one ingress configuration.

---

### Task 2: Inject settings into the Worker decision pipeline and remove artificial BUY limits

**Files:**

- Modify: `src/trading_automaton/domain/dtos.py`
- Modify: `src/trading_automaton/services/decision.py`
- Modify: `src/trading_automaton/services/decision_context.py`
- Modify: `src/trading_automaton/services/decision_materialization.py`
- Modify: `src/trading_automaton/services/strategies.py`
- Modify: `src/trading_automaton/services/runtime_decision_planner.py`
- Modify: `src/trading_automaton/services/position_state_hydration.py`
- Modify: `src/trading_automaton/services/intents.py`
- Modify: `src/trading_automaton/services/broker_tick_preparation.py`
- Modify: `src/trading_automaton/services/streaming_position_decision.py`
- Modify: `src/trading_automaton/services/streaming_runtime_coordinator.py`
- Modify: `src/trading_automaton/services/runtime.py`
- Delete: `src/trading_automaton/services/averaging_capacity.py`
- Delete: `src/moex_sentinel/services/automation_entry_budget.py`
- Modify: `tests/trading_automaton/services/test_adaptive_scalping_strategy.py`
- Modify: `tests/trading_automaton/services/test_decision_context_service.py`
- Create: `tests/trading_automaton/services/test_decision_materialization.py`
- Delete: `tests/trading_automaton/services/test_averaging_capacity_service.py`
- Delete: tests dedicated only to `AutomationEntryBudgetService`

**Interfaces:**

- `DecisionContext.settings: StrategySettings` replaces `DecisionContext.strategy`.
- `DecisionContext.currency: str` comes from command/instrument context.
- `TradeDecisionService` and `DecisionContextService` receive one settings instance through constructors.
- `AdaptiveScalpingStrategy` never calculates lot count from an amount budget.

- [ ] **Step 1: Write failing fixed-lot and unlimited-averaging tests**

Add cases for zero position and existing position. Both eligible BUY decisions must use configured lots:

```python
@pytest.mark.parametrize("current_lots", [0, 1, 37])
def test_eligible_buy_uses_global_lot_count(current_lots: int) -> None:
    context = eligible_context(quantity_lots=current_lots, buy_order_lots=2)
    decision = AdaptiveScalpingStrategy().decide(context)
    assert decision.kind is DecisionKind.BUY_MORE
    assert decision.quantity_lots == 2
```

Simulate more BUY histories than the old maximum and assert another BUY remains possible when signal and cash allow.

- [ ] **Step 2: Write failing trend behavior tests**

Cover `mean_5 < mean_20`/downtrend and failed reversal. Assert `WAIT`, never `SELL_PART` or `SELL_ALL`. Keep independent stop-loss and take-profit regression cases proving their priority.

- [ ] **Step 3: Run decision tests and confirm RED**

```bash
uv run pytest -q \
  tests/trading_automaton/services/test_adaptive_scalping_strategy.py \
  tests/trading_automaton/services/test_decision_context_service.py \
  tests/trading_automaton/services/test_decision_service.py
```

Expected: old amount, maximum-position and capacity logic violates new expectations.

- [ ] **Step 4: Replace strategy access with injected settings**

Use `settings.buy_order_lots` in both `_entry` and `_buy`. Delete checks and reason codes tied to `initial_order_amount`, `max_position_amount`, `minimum_free_cash`, `max_averaging_steps` and `averaging_order_lots`. Preserve pending-low, reversal, candle cooldown, stop-loss, take-profit and partial-profit ordering.

- [ ] **Step 5: Simplify the decision context**

Delete `AveragingCapacity`, `completed_averaging_steps`, `active_averaging_levels` and `available_averaging_levels` where they only cap buying. Keep historical BUY information only when used for last-buy price, commissions or indicators. Calculate visible required cash as:

```python
amount = order_book.best_ask.price * command.lot_size * settings.buy_order_lots
required_order_cash = amount + commission_schedule.estimate("BUY", amount)
```

- [ ] **Step 6: Gate new decisions with `STRATEGY_ENABLED`**

When disabled, return `WAIT / STRATEGY_DISABLED` before creating new BUY/SELL intents, but continue order supervision, reconciliation, outbox delivery and heartbeat. Add a test with an already submitted intent proving supervisor polling continues.

- [ ] **Step 7: Run Worker decision regression**

```bash
uv run pytest -q tests/trading_automaton/services tests/trading_automaton/test_streaming_decision_pipeline.py
```

Expected: all Worker service tests pass without an averaging-capacity service.

---

### Task 3: Make free cash the only BUY affordability boundary

**Files:**

- Modify: `src/trading_automaton/services/account_cash_reservation.py`
- Modify: `src/trading_automaton/services/decision_materialization.py`
- Modify: `src/trading_automaton/services/batch_runtime.py`
- Modify: `src/trading_automaton/services/streaming_position_decision.py`
- Modify: `src/trading_automaton/storage/repository.py`
- Modify: `tests/trading_automaton/services/test_account_cash_reservation_service.py`
- Modify: `tests/trading_automaton/services/test_decision_materialization.py`
- Modify: `tests/trading_automaton/services/test_batch_runtime_service.py`

**Interfaces:**

- Affordability condition: `available_free_cash - pending_batch_reservations >= order_value + estimated_commission`.
- `try_reserve` no longer accepts `minimum_free_cash`.
- Insufficient funds creates neither `BrokerIntentModel` nor broker request.

- [ ] **Step 1: Write failing exact-boundary tests**

Test exact cash, one minimal unit below, existing durable reservation, two same-batch BUYs and separate currencies/accounts. Exact cash including commission must pass; any shortage must yield `WAIT / INSUFFICIENT_FREE_CASH`.

- [ ] **Step 2: Write failing durability test**

After an insufficient-cash materialization, query SQLite and assert no new intent and no dispatch-pending outbox artifact exist, while the decision/audit record may state WAIT.

- [ ] **Step 3: Run tests and confirm RED**

```bash
uv run pytest -q \
  tests/trading_automaton/services/test_account_cash_reservation_service.py \
  tests/trading_automaton/services/test_decision_materialization.py \
  tests/trading_automaton/services/test_batch_runtime_service.py
```

- [ ] **Step 4: Implement authoritative reservation**

Replace the current condition with:

```python
cash_key = (command.account_id, command.currency.upper())
projected = available_before - pending_cash.get(cash_key, Decimal())
if projected < required_order_cash:
    decision = TradeDecision(DecisionKind.WAIT, 0, None, "INSUFFICIENT_FREE_CASH")
else:
    pending_cash[cash_key] = pending_cash.get(cash_key, Decimal()) + required_order_cash
```

Use `command.currency`; do not read currency from strategy snapshots.

- [ ] **Step 5: Run focused tests and confirm GREEN**

```bash
uv run pytest -q \
  tests/trading_automaton/services/test_account_cash_reservation_service.py \
  tests/trading_automaton/services/test_decision_materialization.py \
  tests/trading_automaton/services/test_batch_runtime_service.py
```

---

### Task 4: Collapse Worker storage and HTTP delivery to one typed baseline

**Files:**

- Rename: `src/trading_automaton/storage/fact_outbox_v2.py` -> `src/trading_automaton/storage/fact_outbox.py`
- Modify: `src/trading_automaton/storage/models.py`
- Modify: `src/trading_automaton/storage/database.py`
- Modify: `src/trading_automaton/storage/repository.py`
- Modify: `src/trading_automaton/adapters/core_client.py`
- Modify: `src/trading_automaton/services/fact_synchronization.py`
- Modify: `src/trading_automaton/composition.py`
- Delete: `src/trading_automaton/services/synchronization.py`
- Delete: legacy-only audit delivery path after typed audit facts cover it
- Rename: `tests/trading_automaton/storage/test_fact_outbox_v2.py` -> `tests/trading_automaton/storage/test_fact_outbox.py`
- Modify: `tests/trading_automaton/storage/test_worker_database.py`
- Modify: `tests/trading_automaton/storage/test_local_repository.py`
- Modify: `tests/trading_automaton/adapters/test_core_client.py`
- Modify: `tests/trading_automaton/services/test_fact_synchronization.py`
- Delete: `tests/trading_automaton/services/test_synchronization_service.py`
- Delete: `tests/trading_automaton/storage/test_history_event_stream.py`

**Interfaces:**

- SQLite table `fact_outbox`; ORM `FactOutboxModel`; writer `FactOutboxWriter`.
- Core endpoints exactly `/internal/automation-commands`, `/internal/automation-statuses`, `/internal/automation-facts`.
- One `FactSynchronizationService` owns batching, retry and continuous-prefix ACK.

- [ ] **Step 1: Rewrite tests to baseline names and endpoints**

Assert Worker metadata contains `fact_outbox` and not `outbox_events`/`fact_outbox_v2`. Assert the HTTP adapter never calls `/internal/automaton/events` or `/internal/v2/*`.

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
uv run pytest -q \
  tests/trading_automaton/storage/test_worker_database.py \
  tests/trading_automaton/storage/test_fact_outbox.py \
  tests/trading_automaton/adapters/test_core_client.py \
  tests/trading_automaton/services/test_fact_synchronization.py
```

- [ ] **Step 3: Rename typed storage as baseline**

Rename model/table/index/constraint identifiers. Delete `OutboxEventModel` and branches keyed by ingress version. A clean Worker DB is created directly at the final schema; remove additive compatibility upgrades for deleted tables.

- [ ] **Step 4: Collapse CoreClient**

Keep only baseline typed command/status/fact methods plus required heartbeat, broker connection and audit ownership calls. Delete `_command`, `publish_events`, legacy status conversions and all `StrategyValues` parsing.

- [ ] **Step 5: Collapse composition and synchronization**

Construct one writer, one synchronizer and one repository path unconditionally. Retry limits come from injected `StrategySettings.core_retry_limit`; batch size/deadline remain transport settings.

- [ ] **Step 6: Verify retry/idempotency semantics**

Run:

```bash
uv run pytest -q \
  tests/trading_automaton/storage \
  tests/trading_automaton/adapters/test_core_client.py \
  tests/trading_automaton/services/test_fact_synchronization.py
```

Expected: lost responses retry the same event IDs; only accepted continuous prefixes are removed/marked delivered.

---

### Task 5: Promote normalized Core models and repositories to baseline names

**Files:**

- Rename: `src/moex_sentinel/storage/models/user_brokers_v2.py` -> `user_brokers.py`
- Rename: `src/moex_sentinel/storage/models/reference_data_v2.py` -> `reference_data.py`
- Rename: `src/moex_sentinel/storage/models/automation_facts_v2.py` -> `automation_facts.py`
- Rename: `src/moex_sentinel/storage/models/order_facts_v2.py` -> `order_facts.py`
- Rename: `src/moex_sentinel/storage/models/position_facts_v2.py` -> `position_facts.py`
- Rename: `src/moex_sentinel/storage/models/trading_analytics_v2.py` -> `trading_analytics.py`
- Rename: `src/moex_sentinel/storage/models/trading_observability_v2.py` -> `trading_observability.py`
- Modify: `src/moex_sentinel/storage/models/__init__.py`
- Rename corresponding `*_v2.py` repositories to baseline names
- Modify: `src/moex_sentinel/storage/repositories/trading_facts_uow.py`
- Modify: `src/moex_sentinel/services/trading_fact_ports.py`
- Modify: `src/moex_sentinel/services/trading_fact_ingress.py`
- Modify: `src/moex_sentinel/services/trading_fact_mapping.py`
- Modify: all matching storage/service tests

**Interfaces:**

- ORM names lose `V2`; table/constraint/index names lose `_v2`.
- Remove `AutomationStrategyModel` entirely.
- `PositionLotModel.buy_execution_id` becomes nullable and is required only for source `BROKER_EXECUTION`.
- Constraint failures always translate to stable domain errors, including SQLite foreign-key errors.

- [ ] **Step 1: Change model tests to the target schema**

Assert exact required table set, absence of `_v2`, no strategy table, scoped foreign keys, millisecond timestamps and the lot source constraint:

```sql
CHECK (
  (source = 'BROKER_EXECUTION' AND buy_execution_id IS NOT NULL) OR
  (source = 'BROKER_POSITION_BOOTSTRAP' AND buy_execution_id IS NULL)
)
```

- [ ] **Step 2: Add failing cross-scope/error translation tests**

For SQLite and PostgreSQL, attempt cross-scope automation/instrument/cycle/lot links and assert `TradingFactPersistenceError(CROSS_SCOPE)` rather than raw `IntegrityError`. Cover partial unique constraints and one-open-cycle conflicts.

- [ ] **Step 3: Run storage tests and confirm RED**

```bash
uv run pytest -q tests/storage tests/domain/test_trading_facts.py
```

- [ ] **Step 4: Rename normalized implementation without compatibility aliases**

Use baseline class names such as `TradingAutomationModel`, `AutomationFactsRepository`, `OrderFactsRepository`, `PositionLedgerRepository`, `ReferenceCatalogRepository` and `UserBrokerRepository`. Do not leave aliases named `*V2*` or `*Shadow*`.

- [ ] **Step 5: Remove persisted strategy and migration-only columns**

Delete automation strategy relationships and drafts. Remove `legacy_source_id`, `migrated_at` and `LEGACY_AGGREGATE`; execution sources are `BROKER_FILL` and, only if an execution is genuinely needed later, non-historical baseline sources. Bootstrap itself uses a reconciled lot and no synthetic order/execution.

- [ ] **Step 6: Complete safe constraint translation**

Map named PostgreSQL constraints and SQLite messages/explicit parent lookups to stable errors before flush. Never expose driver text. Add pre-flush scoped-parent validation where SQLite cannot identify the violated composite relation.

- [ ] **Step 7: Run storage suites and confirm GREEN**

```bash
uv run pytest -q tests/storage tests/domain/test_trading_facts.py tests/integration/postgresql/test_trading_fact_ingress.py
```

Expected: normalized repositories pass on SQLite and supplied PostgreSQL; no raw `IntegrityError` crosses a repository boundary.

---

### Task 6: Replace command projection with a native baseline automation repository

**Files:**

- Replace: `src/moex_sentinel/storage/repositories/fact_command_projection.py` -> `automation_commands.py`
- Modify: `src/moex_sentinel/domain/trading_facts.py`
- Modify: `src/moex_sentinel/services/trading_fact_ports.py`
- Modify: `src/moex_sentinel/usecases/trading_fact_ingress.py`
- Modify: `src/moex_sentinel/views/schemas/trading_facts.py`
- Modify: `src/moex_sentinel/views/internal_trading_facts.py`
- Modify: `src/moex_sentinel/composition.py`
- Modify: `src/moex_sentinel/api/app.py`
- Replace: `tests/storage/test_fact_command_projection.py` -> `tests/storage/test_automation_command_repository.py`
- Modify: `tests/api/test_trading_fact_ingress_contract.py`

**Interfaces:**

- `AutomationCommandRepository.claim(limit) -> list[AutomationCommand]` reads only baseline tables.
- `statuses(ids) -> AutomationStatusesResult` has no strategy.
- Router has no prefix and exposes the three exact `/internal/automation-*` routes.

- [ ] **Step 1: Write failing native command tests**

Create baseline user broker, instrument and automation directly. Claim must return the external broker instrument ID, internal fact instrument UUID, currency and optional bootstrap snapshot without reading ID maps or strategy rows.

- [ ] **Step 2: Write failing HTTP surface tests**

Assert OpenAPI contains the three baseline internal paths and does not contain `/internal/v2`, `/internal/automaton/events` or legacy command/status paths.

- [ ] **Step 3: Run tests and confirm RED**

```bash
uv run pytest -q tests/storage/test_automation_command_repository.py tests/api/test_trading_fact_ingress_contract.py
```

- [ ] **Step 4: Implement native claim/status repository**

Query `TradingAutomationModel`, `UserBrokerModel` and `BrokerInstrumentModel` directly. Eligible rows are `IN_QUEUE`, `HOLD/BOOTSTRAPPING`, or `resume_requested`. The command must be a pure projection; no shadow copy or source mapping occurs during claim.

- [ ] **Step 5: Move routes to baseline paths**

Set:

```python
router = APIRouter(tags=["internal-trading-facts"])

@router.post("/internal/automation-commands")
@router.post("/internal/automation-statuses")
@router.post("/internal/automation-facts")
```

Remove the obsolete event router methods while retaining heartbeat, broker connection and any still-required business-audit endpoint under `/internal/automaton`.

- [ ] **Step 6: Run command/API tests and confirm GREEN**

```bash
uv run pytest -q tests/storage/test_automation_command_repository.py tests/api/test_trading_fact_ingress_contract.py tests/api
```

---

### Task 7: Implement idempotent adoption of open broker positions

**Files:**

- Create: `src/moex_sentinel/domain/position_adoption.py`
- Create: `src/moex_sentinel/services/position_adoption.py`
- Create: `src/moex_sentinel/usecases/position_adoption.py`
- Create: `src/moex_sentinel/storage/repositories/position_adoption.py`
- Modify: `src/moex_sentinel/domain/portfolio.py`
- Modify: `src/moex_sentinel/services/portfolio_ports.py`
- Modify: `src/moex_sentinel/adapters/tinvest/portfolio.py`
- Modify: `src/moex_sentinel/adapters/tinvest/order_execution.py`
- Modify: `src/moex_sentinel/usecases/instruments.py`
- Modify: `src/moex_sentinel/composition.py`
- Create: `tests/services/test_position_adoption_service.py`
- Create: `tests/storage/test_position_adoption_repository.py`
- Create: `tests/usecases/test_position_adoption_usecase.py`
- Modify: `tests/adapters/test_tinvest_portfolio_adapter.py`
- Modify: `tests/adapters/test_tinvest_order_execution_adapter.py`
- Modify: `tests/usecases/test_instrument_catalog_usecases.py`

**Interfaces:**

```python
class ActiveBrokerOrder(LegacyPositionalModel):
    account_id: str
    instrument_id: str
    broker_order_id: str
    status: str


class PositionAdoptionBrokerPort(Protocol):
    async def get_positions(self, account_id: str) -> tuple[ExternalPosition, ...]: ...
    async def list_active_orders(self, account_id: str, instrument_id: str) -> tuple[ActiveBrokerOrder, ...]: ...
```

`ExternalPosition` exposes `quantity_lots`, not an ambiguous unit quantity.

- [ ] **Step 1: Write broker adapter contract tests**

Use synthetic SDK responses to prove `quantity_lots` is read from the broker `quantity_lots` field and active orders are filtered by account, instrument and non-terminal status. Terminal filled/cancelled/rejected orders must not block adoption; unknown/uncertain statuses must block.

- [ ] **Step 2: Write failing adoption domain tests**

Cover:

- positive whole lots and positive average price;
- zero/negative/fractional lots;
- missing average price or currency mismatch;
- missing catalog mapping;
- active/uncertain order;
- new valid position;
- exact retry;
- retry with conflicting bootstrap values;
- already managed active automation.

- [ ] **Step 3: Define stable results and error codes**

Use `PositionAdoptionResult(adopted, existing, held, skipped)` and safe reason codes `BOOTSTRAP_INVALID_QUANTITY`, `BOOTSTRAP_PRICE_UNAVAILABLE`, `BOOTSTRAP_INSTRUMENT_NOT_FOUND`, `BOOTSTRAP_ACTIVE_ORDER`, `BOOTSTRAP_COMMISSION_UNAVAILABLE`, `BOOTSTRAP_CONFLICT`.

- [ ] **Step 4: Implement one atomic Core adoption write**

For a new position, one transaction creates:

- automation state `HOLD`, `hold_reason="BOOTSTRAPPING"`;
- deterministic or persisted `position_cycle_id` and `position_lot_id`;
- `bootstrap_quantity_lots`, `bootstrap_average_price`, `bootstrap_currency`, `bootstrap_observed_at` on the automation start snapshot;
- zero realized P&L and commissions.

Use unique `(user_broker_id, instrument_id)` active-automation constraint as the final idempotency guard. On duplicate, load and compare the existing bootstrap identity rather than creating another row.

- [ ] **Step 5: Invoke adoption only after catalog commit**

Change `SynchronizeBrokerInstrumentsUsecase` to await catalog synchronization, then call `AdoptBrokerPositionsUsecase.execute(broker_id)`. Do not put broker network calls inside the catalog DB transaction. If one position fails, return catalog success plus position-level HOLD/error diagnostics; never roll back the synchronized catalog.

- [ ] **Step 6: Run adoption suites and confirm GREEN**

```bash
uv run pytest -q \
  tests/adapters/test_tinvest_portfolio_adapter.py \
  tests/adapters/test_tinvest_order_execution_adapter.py \
  tests/services/test_position_adoption_service.py \
  tests/storage/test_position_adoption_repository.py \
  tests/usecases/test_position_adoption_usecase.py \
  tests/usecases/test_instrument_catalog_usecases.py
```

---

### Task 8: Bootstrap Worker recovery state and activate only through typed facts

**Files:**

- Create: `src/trading_automaton/services/position_bootstrap.py`
- Modify: `src/trading_automaton/storage/repository.py`
- Modify: `src/trading_automaton/storage/fact_outbox.py`
- Modify: `src/trading_automaton/services/runtime.py`
- Modify: `src/trading_automaton/services/recovery.py`
- Modify: `src/trading_automaton/services/lot_ledger.py`
- Modify: `src/trading_automaton/services/position_state_hydration.py`
- Modify: `src/trading_automaton/composition.py`
- Create: `tests/trading_automaton/services/test_position_bootstrap_service.py`
- Modify: `tests/trading_automaton/storage/test_local_repository.py`
- Modify: `tests/trading_automaton/services/test_recovery_service.py`
- Create: `tests/integration/test_open_position_bootstrap.py`

**Interfaces:**

- `PositionBootstrapService.ensure(command, settings) -> BootstrapResult`.
- One SQLite transaction creates cache/recovery/cycle/reconciled lot and the complete typed fact sequence.
- The final fact in the sequence is `AutomationStateChanged(IN_WORK)`.

- [ ] **Step 1: Write failing atomic bootstrap test**

Given a HOLD command with bootstrap snapshot, assert one transaction creates exactly one local lot with source `BROKER_POSITION_BOOTSTRAP`, exact lots/price, zero commission and no intent/order/execution. Assert outbox sequence contains cycle update, lot open, audit and final state change.

- [ ] **Step 2: Write failing idempotency/crash tests**

Call `ensure` twice and after simulated failure before commit. Assert no duplicate lot/outbox rows. Simulate accepted Core transaction plus lost response; retry must reuse event IDs and Core must return exact idempotent success.

- [ ] **Step 3: Write failing HOLD tests**

Missing market, commission or bootstrap validation must leave Core state HOLD and create no trade intent. A partial or failed fact batch must not expose `IN_WORK` because the Core group is one transaction and the state-change fact is last.

- [ ] **Step 4: Implement the contiguous bootstrap fact group**

Build facts with consecutive sequence numbers and the same initial expected revision. `PositionCycleUpdated` uses broker quantity/average, invested amount and zeros for historical P&L/fees. `PositionLotOpened` has `buy_execution_id=None`, source `BROKER_POSITION_BOOTSTRAP`. Append `TradeAuditRecorded(BOOTSTRAP_POSITION_ADOPTED)` and finally `AutomationStateChanged(IN_WORK)`.

- [ ] **Step 5: Hydrate adopted positions**

After Core ACK/status refresh, use the reconciled local lot as normal LIFO inventory. Market indicators and commission profiles load through existing runtime services. No special decision path remains after activation.

- [ ] **Step 6: Run bootstrap integration**

```bash
uv run pytest -q \
  tests/trading_automaton/services/test_position_bootstrap_service.py \
  tests/trading_automaton/services/test_recovery_service.py \
  tests/trading_automaton/storage/test_local_repository.py \
  tests/integration/test_open_position_bootstrap.py
```

Expected: the position reaches `IN_WORK` only after one idempotently accepted fact group.

---

### Task 9: Remove all persisted strategy APIs and frontend editing

**Files:**

- Delete: `src/moex_sentinel/services/strategies.py`
- Delete: `src/moex_sentinel/usecases/strategies.py`
- Delete: `src/moex_sentinel/storage/repositories/strategies.py`
- Delete: `src/moex_sentinel/views/strategies.py`
- Modify: `src/moex_sentinel/services/automations.py`
- Modify: `src/moex_sentinel/usecases/automations.py`
- Modify: `src/moex_sentinel/views/schemas/automations.py`
- Modify: `src/moex_sentinel/views/automations.py`
- Modify: `src/moex_sentinel/composition.py`
- Modify: `src/moex_sentinel/api/app.py`
- Delete: `frontend/src/views/StrategiesView.vue`
- Delete: `frontend/src/views/StrategiesView.spec.ts`
- Modify: `frontend/src/api/automations.ts`
- Modify: `frontend/src/api/automations.spec.ts`
- Modify: `frontend/src/router.ts`
- Modify: `frontend/src/App.vue`
- Delete: strategy service/usecase/repository/API tests

**Interfaces:**

- Automation creation accepts only `account_id`.
- Automation read DTO may expose `strategy_code`, `strategy_version` and optional effective diagnostics, but no mutable strategy object.
- No strategy CRUD or PATCH route.

- [ ] **Step 1: Change API tests to reject strategy writes**

Assert an extra `strategy` creation field returns 422 and strategy-template/PATCH endpoints return 404. Assert automation responses contain no persisted `strategy` object.

- [ ] **Step 2: Change frontend tests**

Assert router/navigation have no `strategies`; API module exports no strategy mutations or template types. Keep readonly strategy code/version on position details only if actually supplied by Core.

- [ ] **Step 3: Run API/frontend tests and confirm RED**

```bash
uv run pytest -q tests/api/test_strategy_contract.py tests/api
npm --prefix frontend test -- --run frontend/src/api/automations.spec.ts
```

- [ ] **Step 4: Delete strategy persistence and simplify automation lifecycle**

Remove strategy resolution, entry-budget checks, `update_strategy` and strategy-table joins. Manual automation creation creates an `IN_QUEUE` row; adopted position creation remains the separate HOLD bootstrap path.

- [ ] **Step 5: Remove frontend editing surface**

Delete route, navigation, view, API methods and TypeScript types for strategy editing. Update position components to consume the strategy-free automation shape.

- [ ] **Step 6: Run backend/frontend tests and confirm GREEN**

```bash
uv run pytest -q tests/api tests/usecases
npm --prefix frontend test
npm --prefix frontend run typecheck
```

---

### Task 10: Delete migration/replay tooling and every compatibility implementation

**Files:**

- Delete: `src/moex_sentinel/domain/reference_data_migration.py`
- Delete: `src/moex_sentinel/domain/trading_history_migration.py`
- Delete: `src/moex_sentinel/services/reference_data_migration.py`
- Delete: `src/moex_sentinel/services/trading_history_migration.py`
- Delete: `src/moex_sentinel/usecases/reference_data_migration.py`
- Delete: `src/moex_sentinel/storage/models/data_migration.py`
- Delete: `src/moex_sentinel/storage/repositories/reference_data_migration.py`
- Delete: `src/moex_sentinel/storage/repositories/database_transfer.py`
- Delete: `src/moex_sentinel/migrations/database_transfer.py`
- Delete: `src/moex_sentinel/migrations/reference_data.py`
- Delete: `src/moex_sentinel/migrations/trading_history.py`
- Delete: `src/moex_sentinel/migrations/worker_event_stream.py`
- Delete: superseded legacy Core trading/reference models and repositories after baseline read cutover
- Modify: `src/sentinel_contracts/base.py`
- Modify: all imports of `LegacyPositionalModel`
- Modify: `pyproject.toml`
- Modify: `compose.yml`
- Delete: matching migration/replay/legacy tests
- Create/Modify: `tests/test_architecture.py`

**Interfaces:**

- Rename shared base `LegacyPositionalModel` to `PositionalModel` with no compatibility alias.
- Keep only `moex-migrate-schema` CLI.
- Keep only schema-migration Compose service; remove `database-transfer` and `trading-history-replay`.

- [ ] **Step 1: Add a failing production-source architecture scan**

```python
FORBIDDEN = (
    r"\blegacy\b",
    r"\bV2\b",
    r"_v2\b",
    r"FACT_INGRESS_VERSION",
    r"/internal/v2",
    r"strategy_templates",
    r"automation_strategies",
    r"instrument_strategy_defaults",
)
```

Scan `src`, `frontend/src`, `compose.yml`, `.env.example`, active README/runtime docs and `pyproject.toml`. Exclude the approved historical spec and this implementation plan.

- [ ] **Step 2: Run scan and confirm RED**

```bash
uv run pytest -q tests/test_architecture.py
```

- [ ] **Step 3: Remove migration/replay entry points and profiles**

Delete three obsolete script entries from `pyproject.toml`, their modules, Docker Compose services/volumes and tests. Retain Alembic schema application only.

- [ ] **Step 4: Remove superseded runtime code**

Delete old automation events, old Core order/ledger implementations, compatibility aliases, `STOPPED`/`COMPLETED` states, `_recover_legacy_opening`, ingress switches and old comments. Rename remaining normalized modules/classes as baseline rather than wrapping them.

- [ ] **Step 5: Rename the positional base throughout the repository**

Replace imports/usages with `PositionalModel`; do not leave an alias containing the forbidden term.

- [ ] **Step 6: Run architecture and import tests**

```bash
uv run pytest -q tests/test_architecture.py tests/test_composition.py tests/test_compose_config.py
```

Expected: no forbidden runtime identifier/path/table remains.

---

### Task 11: Replace the Alembic chain with one clean baseline

**Files:**

- Delete: all current `alembic/versions/*.py`
- Create: `alembic/versions/0001_baseline.py`
- Modify: `src/moex_sentinel/storage/schema_revision.py`
- Modify: `tests/migrations/test_schema_command.py`
- Replace: old shadow migration tests with `tests/migrations/test_baseline_schema.py`
- Modify: `tests/integration/postgresql/test_trading_fact_ingress.py`
- Modify: `tests/test_compose_config.py`

**Interfaces:**

```python
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None
```

- [ ] **Step 1: Write failing empty-database schema tests**

On SQLite metadata and disposable PostgreSQL schema, assert `upgrade head` from empty produces exactly the baseline table set, all required scoped FKs/unique/check constraints and no removed table. Assert a second `upgrade head` is a no-op.

- [ ] **Step 2: Run migration tests and confirm RED**

```bash
uv run pytest -q tests/migrations/test_baseline_schema.py tests/migrations/test_schema_command.py
```

- [ ] **Step 3: Generate and review one explicit baseline revision**

Delete the old chain and create one revision whose `upgrade()` creates the final tables and whose `downgrade()` drops them in reverse FK order. Do not call application `Base.metadata.create_all()` from the revision; keep migration DDL explicit and reviewable.

- [ ] **Step 4: Verify metadata/migration parity**

Compare table names, columns, nullability, defaults, PK/FK, unique/check constraints and indexes between SQLAlchemy metadata and the migrated PostgreSQL schema. Add a test that fails on drift.

- [ ] **Step 5: Run PostgreSQL migration and ingress tests**

```bash
POSTGRES_USER=moex_sentinel_user docker compose --profile migrations run -T --rm migrations
uv run pytest -q -m postgresql tests/integration/postgresql
```

Expected: clean upgrade reaches `0001_baseline`; all PostgreSQL tests pass; no `_v2` relation exists.

---

### Task 12: Update active documentation and operational cutover runbook

**Files:**

- Modify: `README.md`
- Modify: `AGENT_BRIEF.md`
- Modify: `docs/development.md`
- Modify: `docs/trading-strategy.md`
- Create: `docs/clean-slate-cutover.md`
- Modify: `docs/phase-1-implementation-plan.md` or replace it with current milestone status
- Delete: obsolete active migration/replay documentation not retained as approved historical records
- Modify: `tests/test_documentation.py`

**Interfaces:**

- Runbook separates reversible detach/new-volume steps from permanent deletion.
- Runbook uses `STRATEGY_ENABLED=false` quiescence and proves supervisor remains active.
- Permanent volume deletion has an explicit STOP/confirmation gate.

- [ ] **Step 1: Write failing documentation assertions**

Assert active docs contain the eleven environment variables, three typed endpoints, bootstrap HOLD→IN_WORK flow, no historical migration instruction and no Mypy command.

- [ ] **Step 2: Document the runtime data flow inline**

Include this baseline diagram:

```text
broker catalog sync
  -> read open positions + active orders
  -> Core automation HOLD/BOOTSTRAPPING
  -> typed command with bootstrap snapshot
  -> Worker reconciled cycle/lot + typed outbox
  -> Core atomic fact group
  -> final state fact IN_WORK
  -> normal runtime decisions
```

- [ ] **Step 3: Document quiescence acceptance**

The runbook must require:

1. set `STRATEGY_ENABLED=false` and restart only Worker;
2. confirm no new intent IDs appear while heartbeat/supervisor continue;
3. wait until all orders are `FILLED/CANCELLED/REJECTED/EXPIRED`;
4. block on any `UNCERTAIN`, submitted or partially filled order;
5. stop Worker and Core only after the terminal check.

- [ ] **Step 4: Document volume handling without deleting anything**

Resolve and record exact Compose project and volume names with read-only Docker inspection. Stop services, retain old volumes detached, choose new explicit `POSTGRES_VOLUME_NAME` and `AUTOMATON_VOLUME_NAME`, then start the clean stack. Put permanent deletion in a separate final section headed `STOP — требуется отдельное подтверждение`.

- [ ] **Step 5: Run documentation tests**

```bash
uv run pytest -q tests/test_documentation.py
```

---

### Task 13: Full automated verification

**Files:**

- Modify only files required by failures; do not weaken gates.

- [ ] **Step 1: Run formatting and lint gates**

```bash
uv run ruff check .
uv run black --check .
```

Expected: both commands exit 0. Do not run Mypy.

- [ ] **Step 2: Run the complete backend suite**

```bash
uv run pytest -q
```

Expected: all non-PostgreSQL tests pass with no unexpected skips or warnings promoted by strict markers.

- [ ] **Step 3: Run frontend gates**

```bash
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

Expected: Vitest, Vue TypeScript and production build all pass.

- [ ] **Step 4: Validate Compose and clean schema**

```bash
docker compose config --quiet
POSTGRES_USER=moex_sentinel_user docker compose --profile migrations run -T --rm migrations
```

Expected: Compose resolves; Alembic reaches `0001_baseline` on an empty named volume.

- [ ] **Step 5: Run PostgreSQL suite**

```bash
uv run pytest -q -m postgresql tests/integration/postgresql
```

Expected: all scoped persistence, constraint translation, fact ingress and bootstrap tests pass against PostgreSQL.

- [ ] **Step 6: Run final forbidden-code scan manually**

```bash
rg -n -i '\blegacy\b|\bV2\b|_v2\b|FACT_INGRESS_VERSION|/internal/v2|strategy_templates|automation_strategies|instrument_strategy_defaults' \
  src frontend/src compose.yml .env.example pyproject.toml README.md docs/development.md docs/trading-strategy.md
```

Expected: no output.

---

### Task 14: Controlled sandbox cutover and runtime acceptance

**Files:**

- No source changes unless acceptance reveals a reproducible defect; fixes return to the relevant TDD task.
- Update checklist/results only in `docs/clean-slate-cutover.md` without credentials or raw broker payloads.

- [ ] **Step 1: Quiesce the existing Worker**

Set `STRATEGY_ENABLED=false`, recreate Worker and observe that no new intent appears while existing order tracking, fact delivery and heartbeat continue. Poll until every current order is terminal. If any status is active or uncertain, stop this task and keep the old databases untouched.

- [ ] **Step 2: Resolve old resources read-only**

Use `docker compose ps`, `docker compose config` and `docker volume inspect` to record exact Core/Worker volume names and mount ownership. Do not print or copy connection secrets. Stop services and leave volumes detached; do not delete them.

- [ ] **Step 3: Start clean Core storage**

Choose new explicit volume names, create/start PostgreSQL, run `moex-migrate-schema`, then start Core without Worker. Verify health and that no table name ends in `_v2`.

- [ ] **Step 4: Manual broker setup checkpoint**

Pause for the user to configure the sandbox broker through the normal UI/API. Do not import an old broker row or credential. Continue only after the broker connection and account are active.

- [ ] **Step 5: Synchronize catalog and adopt positions**

Run the normal instrument synchronization. For each positive open position, verify exactly one automation exists in HOLD/BOOTSTRAPPING, then start Worker and verify the contiguous bootstrap fact group is acknowledged and the automation becomes `IN_WORK`.

- [ ] **Step 6: Prove runtime behavior**

Verify:

- runtime iterations and heartbeat continue;
- open broker quantity and average price equal the reconciled starting cycle/lot;
- historical realized P&L and commissions start at zero;
- no bootstrap order/execution was invented;
- insufficient cash yields WAIT and no intent;
- an eligible sandbox BUY requests exactly configured lots;
- stop-loss/take-profit/partial-profit behavior remains active;
- restart does not duplicate automation, cycle, lot or facts.

- [ ] **Step 7: Keep old volumes through the acceptance window**

Report successful runtime acceptance with the detached volume names generalized in user-facing output. Do not delete anything in this step.

- [ ] **Step 8: Destructive cleanup gate**

Before any `docker volume rm`, SQLite deletion or obsolete artifact deletion outside source control, show the exact resolved targets and request a new explicit confirmation. If confirmation is not provided, finish with the old volumes safely retained.

## Completion Criteria

- One baseline Core schema and one typed Worker outbox exist; no runtime compatibility branch remains.
- Core command/status/fact endpoints have baseline paths and strategy-free contracts.
- Strategy configuration is global, immutable, environment-derived and validated before trading.
- BUY lots and cash behavior match the approved rules without amount/step limits.
- Every valid open sandbox position is adopted exactly once and reaches `IN_WORK` through typed facts.
- Full Python, frontend, PostgreSQL, Compose and architecture gates pass.
- Runtime acceptance succeeds on new volumes while old volumes remain recoverable until a separately confirmed deletion.
- No Git commit was created.
