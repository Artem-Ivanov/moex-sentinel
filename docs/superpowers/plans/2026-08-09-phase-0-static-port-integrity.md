# Phase 0 Static Port Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:executing-plans` to implement this plan task by task.

**Goal:** Устранить 14 несовместимостей dependency-injection портов в `trading_automaton` без изменения торгового, брокерского и persistence-поведения.

**Architecture:** Повторяющиеся потребительские контракты `update_intent` и `record_order_stage` выделяются в доменные Protocol-порты только после третьего использования. Остальные порты остаются локальными владельцу-потребителю и точно повторяют доступную часть сигнатуры внедряемого сервиса или репозитория. Composition root остаётся единственным местом сборки зависимостей и не получает `cast`, `type: ignore` или адаптеры-обёртки ради типизации.

**Tech Stack:** Python 3.12, `typing.Protocol`, Pydantic v2 DTO, mypy, pytest, Ruff, Black, uv.

## Global Constraints

- Не менять бизнес-логику, последовательность вызовов, retry/hold-политику, транзакции и формат сохраняемых фактов.
- Не менять бизнес-условия и assertions существующих тестов.
- Не добавлять `Any`, `cast`, `type: ignore` и ослабленные DTO для обхода mypy.
- DTO импортировать из `trading_automaton.domain`, а не через re-export concrete storage repository.
- Не обобщать порт до третьего реального потребителя.
- Не создавать коммиты.
- После каждой подзадачи запускать целевые тесты, после всей задачи — полную регрессию.

---

### Task 1: Зафиксировать красную статическую проверку и добавить общие порты

**Files:**

- Create: `src/trading_automaton/domain/ports.py`
- Modify: `src/trading_automaton/services/order_tracking.py`
- Modify: `src/trading_automaton/services/order_dispatch.py`
- Modify: `src/trading_automaton/services/uncertain_intent_reconciliation.py`
- Modify: `src/trading_automaton/services/synchronization.py`
- Test: `tests/trading_automaton/test_streaming_composition.py`

**Step 1: Подтвердить исходное падение**

Run:

```bash
uv run mypy src/trading_automaton/composition.py
```

Expected: 14 ошибок несовместимости Protocol в composition root. Результат является RED-проверкой структурного контракта.

**Step 2: Создать доменные общие порты**

В `trading_automaton.domain.ports` определить:

```python
class IntentUpdatePort(Protocol):
    def update_intent(
        self,
        idempotency_key: str,
        *,
        state: str,
        occurred_at: datetime,
        broker_order_id: str | None = None,
        requested_amount: Decimal | None = None,
        executed_amount: Decimal | None = None,
        estimated_commission: Decimal | None = None,
        executed_commission: Decimal | None = None,
        executed_lots: int | None = None,
        executed_price: Decimal | None = None,
        executed_at: datetime | None = None,
        dispatch_started_at: datetime | None = None,
        broker_responded_at: datetime | None = None,
        terminal_at: datetime | None = None,
        process_id: str | None = None,
    ) -> LocalIntentRecord: ...


class OrderStageAuditPort(Protocol):
    def record_order_stage(
        self,
        *,
        stage: BusinessAuditStage,
        process_id: str,
        automation_id: str,
        broker_id: str,
        account_id: str,
        instrument_id: str,
        message: str,
        critical: bool = False,
        **data: object,
    ) -> None: ...
```

Импорты ограничить стандартной библиотекой, `sentinel_contracts` и доменными DTO.

**Step 3: Подключить общие порты в потребителях**

- `TrackingRepositoryPort`, `DispatchRepositoryPort` и `ReconciliationRepositoryPort` наследуют `IntentUpdatePort`.
- Tracking, dispatch, reconciliation и synchronization принимают `OrderStageAuditPort`; локальные дубли `*AuditPort` удалить.
- Не менять ни один call site.

**Step 4: Запустить промежуточную статическую и runtime-проверку**

Run:

```bash
uv run mypy src/trading_automaton/composition.py
uv run python -m pytest -q \
  tests/trading_automaton/services/test_order_tracking_service.py \
  tests/trading_automaton/services/test_order_dispatch_service.py \
  tests/trading_automaton/services/test_uncertain_intent_reconciliation_service.py \
  tests/trading_automaton/services/test_synchronization_service.py \
  tests/trading_automaton/test_streaming_composition.py
```

Expected: количество mypy-ошибок уменьшается; целевые тесты проходят без изменения assertions.

---

### Task 2: Уточнить hydration и decision-audit порты

**Files:**

- Modify: `src/trading_automaton/services/position_state_hydration.py`
- Modify: `src/trading_automaton/services/streaming_batch_tick.py`
- Test: `tests/trading_automaton/services/test_position_state_hydration_service.py`
- Test: `tests/trading_automaton/services/test_streaming_batch_tick_service.py`

**Step 1: Уточнить контракт consistency**

`PositionConsistencyPort.reconcile` должен принимать только:

```python
def reconcile(
    self,
    *,
    automation_id: str,
    broker_lots: int,
    average_price: Decimal,
) -> PositionConsistencyResult: ...
```

`PositionConsistencyResult` импортировать из доменного слоя.

**Step 2: Уточнить reconciliation audit**

`PositionAuditPort.record_reconciliation` явно объявляет `stage`, `process_id`, `automation_id`, `broker_id`, `account_id`, `instrument_id`; только очищенный stage-specific payload остаётся в `**data: object`.

**Step 3: Уточнить business decision audit**

`BusinessAuditPort.record_decision_process` явно объявляет `process_id`, `automation_id`, `broker_id`, `account_id`, `instrument_id`; decision-specific payload остаётся в `**data: object`.

**Step 4: Устранить re-export импорты DTO на затронутых строках**

Перенести импорты `IntentHistory`, `TradeLotRecord`, `TradingCycleState`, `DecisionBatchItem`, `IntentBatchItem` из `trading_automaton.storage.repository` в соответствующий доменный модуль. Runtime-код не менять.

**Step 5: Проверить подзадачу**

Run:

```bash
uv run python -m pytest -q \
  tests/trading_automaton/services/test_position_state_hydration_service.py \
  tests/trading_automaton/services/test_streaming_batch_tick_service.py
uv run mypy src/trading_automaton/composition.py
```

Expected: тесты зелёные; hydration/batch несовместимости исчезли.

---

### Task 3: Уточнить порты жизненного цикла заявки

**Files:**

- Modify: `src/trading_automaton/services/order_tracking.py`
- Modify: `src/trading_automaton/services/order_dispatch.py`
- Modify: `src/trading_automaton/services/uncertain_intent_reconciliation.py`
- Test: `tests/trading_automaton/services/test_order_tracking_service.py`
- Test: `tests/trading_automaton/services/test_order_dispatch_service.py`
- Test: `tests/trading_automaton/services/test_uncertain_intent_reconciliation_service.py`

**Step 1: Уточнить tracking repository**

- `save_execution_event` объявить с обязательными `automation_id`, `occurred_at`, `process_id`, `position_snapshot`, `operation`.
- `finalize_execution` сохранить как `ExecutionFinalization -> ExecutionFinalizationResult`.
- DTO импортировать из доменного слоя.

**Step 2: Уточнить commission port**

`CommissionObservationPort.observe_execution` принимает `Decimal` для `order_amount` и `actual_commission`, возвращает `AccountCommissionProfile | None`.

**Step 3: Уточнить ledger ports**

Tracking и reconciliation сохраняют локальные порты с точными методами:

```python
def record_buy_execution(
    self,
    *,
    automation_id: str,
    intent_id: str,
    quantity_lots: int,
    price: Decimal,
    commission: Decimal,
    executed_at: datetime,
) -> TradeLotRecord: ...

def allocate_sell_execution(
    self,
    *,
    automation_id: str,
    intent_id: str,
    quantity_lots: int,
    price: Decimal,
    commission: Decimal,
    executed_at: datetime,
    lot_size: int,
) -> None: ...
```

**Step 4: Не менять lifecycle-вызовы**

Проверить diff: dispatch, polling, finalization, reconciliation, HOLD и broker audit вызываются в прежнем порядке и с прежними данными.

**Step 5: Проверить подзадачу**

Run:

```bash
uv run python -m pytest -q \
  tests/trading_automaton/services/test_order_tracking_service.py \
  tests/trading_automaton/services/test_order_dispatch_service.py \
  tests/trading_automaton/services/test_uncertain_intent_reconciliation_service.py
uv run mypy src/trading_automaton/composition.py
```

Expected: lifecycle-тесты зелёные; несовместимости tracking/dispatch/reconciliation отсутствуют.

---

### Task 4: Уточнить synchronization и coordinator repository

**Files:**

- Modify: `src/trading_automaton/services/synchronization.py`
- Modify: `src/trading_automaton/services/streaming_runtime_coordinator.py`
- Test: `tests/trading_automaton/services/test_synchronization_service.py`
- Test: `tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py`
- Test: `tests/trading_automaton/test_streaming_composition.py`

**Step 1: Использовать общий audit port в synchronization**

Удалить локальный `SynchronizationAuditPort`, тип зависимости заменить на `OrderStageAuditPort`. Вызовы и обработку ошибок не менять.

**Step 2: Уточнить coordinator repository**

Объявить точные сигнатуры:

```python
def save_state_and_event(
    self,
    *,
    automation_id: str,
    state: str,
    revision: int,
    sequence_number: int,
    event_id: str,
    safe_message: str,
    occurred_at: datetime,
    metadata: dict[str, object],
) -> None: ...

def synchronize_core_state(
    self,
    automation_id: str,
    *,
    state: str,
    revision: int,
    last_sequence_number: int | None,
    strategy: StrategyValues,
) -> AutomationCommand: ...

def pending_outbox(self, limit: int) -> list[OutboxRecord]: ...
```

Если mypy выявит инвариантность `dict`, сузить аннотацию concrete repository с `Any` до `object` только после проверки всех его вызывающих сторон; не вводить новый `Any` и не менять сериализуемое значение.

**Step 3: Проверить coordinator boundary**

Run:

```bash
uv run python -m pytest -q \
  tests/trading_automaton/services/test_synchronization_service.py \
  tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py \
  tests/trading_automaton/test_streaming_composition.py
uv run mypy src/trading_automaton/composition.py
```

Expected: `Success: no issues found in 1 source file`.

---

### Task 5: Выполнить приёмку 0.2.0 и обновить фазу

**Files:**

- Modify: `docs/phase-0-trading-service-refactor.md`
- Verify: all files touched in Tasks 1–4

**Step 1: Проверить отсутствие обходов типизации**

Run:

```bash
git diff -- src/trading_automaton | rg 'type: ignore|\bcast\(|\bAny\b'
```

Expected: нет новых совпадений, добавленных ради совместимости портов. Существующие строки оцениваются по diff, а не по всей исторической кодовой базе.

**Step 2: Отформатировать и проверить изменённый Python-код**

Run:

```bash
uv run ruff check --config pyproject.toml --fix \
  src/trading_automaton/domain/ports.py \
  src/trading_automaton/services/position_state_hydration.py \
  src/trading_automaton/services/order_tracking.py \
  src/trading_automaton/services/order_dispatch.py \
  src/trading_automaton/services/streaming_batch_tick.py \
  src/trading_automaton/services/uncertain_intent_reconciliation.py \
  src/trading_automaton/services/synchronization.py \
  src/trading_automaton/services/streaming_runtime_coordinator.py
uv run black --config pyproject.toml \
  src/trading_automaton/domain/ports.py \
  src/trading_automaton/services/position_state_hydration.py \
  src/trading_automaton/services/order_tracking.py \
  src/trading_automaton/services/order_dispatch.py \
  src/trading_automaton/services/streaming_batch_tick.py \
  src/trading_automaton/services/uncertain_intent_reconciliation.py \
  src/trading_automaton/services/synchronization.py \
  src/trading_automaton/services/streaming_runtime_coordinator.py
uv run ruff check --config pyproject.toml \
  src/trading_automaton/domain/ports.py \
  src/trading_automaton/services/position_state_hydration.py \
  src/trading_automaton/services/order_tracking.py \
  src/trading_automaton/services/order_dispatch.py \
  src/trading_automaton/services/streaming_batch_tick.py \
  src/trading_automaton/services/uncertain_intent_reconciliation.py \
  src/trading_automaton/services/synchronization.py \
  src/trading_automaton/services/streaming_runtime_coordinator.py
```

Expected: Ruff и Black завершаются успешно.

**Step 3: Запустить итоговую статическую проверку и целевую регрессию**

Run:

```bash
uv run mypy src/trading_automaton/composition.py
uv run python -m pytest -q \
  tests/trading_automaton/services/test_position_state_hydration_service.py \
  tests/trading_automaton/services/test_order_tracking_service.py \
  tests/trading_automaton/services/test_order_dispatch_service.py \
  tests/trading_automaton/services/test_streaming_batch_tick_service.py \
  tests/trading_automaton/services/test_uncertain_intent_reconciliation_service.py \
  tests/trading_automaton/services/test_synchronization_service.py \
  tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py \
  tests/trading_automaton/test_streaming_composition.py
```

Expected: mypy — 0 ошибок; целевые тесты — 0 падений.

**Step 4: Запустить полную регрессию**

Run:

```bash
uv run python -m pytest -q
```

Expected: 0 падений. Классифицировать любое падение по правилам вехи 0; не менять DoD теста без отдельной валидации.

**Step 5: Обновить статус фазы и выполнить финальные проверки**

Отметить `0.2.0` и `0.2.0.1–0.2.0.5` как выполненные только после доказанной приёмки и записать фактические результаты команд.

Run:

```bash
uv run python -m pytest -q tests/test_documentation.py
uv lock --check
git diff --check
git status --short
```

Expected: документационные тесты, lock и whitespace-проверка зелёные; status показывает только осознанные незакоммиченные изменения.
