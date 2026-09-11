# Core/Worker Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Complete the tasks and review before marking the milestone done.

**Goal:** Закрыть 0.3: единые переходы, интеграционный контракт, трассировка.

**Architecture:** Чистая shared policy используется в границах Core и Worker.
Существующие typed outbox, HTTP API и схемы хранения сохраняются.

**Tech Stack:** Python 3.12, Pydantic, FastAPI, SQLAlchemy, PostgreSQL 16, SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-09-09-core-worker-lifecycle-design.md`.

## Constraints

- Рабочая sandbox-торговля и её данные не являются тестовым окружением.
- Вспомогательные исполнимые скрипты остаются в `develop/`.
- Git без начального коммита: работаем в текущем дереве, индекс пользователя не меняем.
- Миграций схемы, второго протокола и изменения стратегии нет.

## Task 1: Shared lifecycle policy and Core enforcement

Files: создать `src/sentinel_contracts/automation_lifecycle.py`; изменить
`src/moex_sentinel/services/automations.py`, `services/trading_fact_ingress.py`,
`storage/repositories/automations.py`; добавить/обновить tests для политики,
сервисов и ingress.

Контракт: `TransitionOrigin` со значениями `USER_COMMAND`, `WORKER_FACT`;
`InvalidAutomationTransition(ValueError)`;
`validate_automation_transition(current: AutomationState, target: AutomationState,
*, origin: TransitionOrigin, bootstrap: bool = False) -> bool`.
Возвращает False для того же состояния, True для разрешённого нового состояния;
запрещённые переходы поднимают бизнес-исключение.

- [x] Сначала воспроизвести запрещённое CLOSED -> IN_WORK и обычный HOLD -> IN_WORK.
- [x] Реализовать матрицу из spec, idempotent user actions и проверку ingress.
- [x] Защитить обновление public state от конкурентной потери ревизии.
- [x] Проверить rollback rejected group, exact retry, bootstrap и CAS regression.

## Task 2: Worker enforcement and two-service integration

Files: `src/trading_automaton/storage/repository.py`, при необходимости
`services/streaming_runtime_coordinator.py`, новый worker lifecycle service;
`tests/integration/test_core_worker_lifecycle.py` и целевые Worker tests.
Shared API — ровно контракт Task 1. Authoritative snapshot sync не является
локальным lifecycle proposal и не блокируется этой матрицей.

- [x] Воспроизвести недопустимый local transition и дублирование одинаковой команды.
- [x] Применить shared policy к state proposals, hold и bootstrap; сохранить транзакционность.
- [x] Через реальный HTTP app/CoreClient связать два раздельных storage и проверить
  claim -> activation -> ACK/retry -> HOLD -> resume -> CLOSED, ошибки и rollback.
- [x] Проверить существующие coordinator/outbox/bootstrap regression suites.

## Task 3: Process correlation

Files: `src/trading_automaton/services/decision_materialization.py`,
`services/uncertain_intent_reconciliation.py`, соответствующие service tests.
Использовать existing `HydratedPositionState.process_id` и explicit recovery UUID,
без добавления колонок. Входы/выходы market/decision/order сохраняют один process_id.

- [x] Тестом показать разрыв process_id между reconciliation и решением.
- [x] Переиспользовать hydration process_id, оставив UUID fallback для отсутствующего значения.
- [x] Стабильный отдельный process_id recovery использовать в каждой audit-стадии и финализации.
- [x] Проверить stage/time/process_id и неизменность торговых решений.

## Task 4: Review, acceptance and documentation

- [x] Независимый review задач и интеграционного результата.
- [x] Полная Python-регрессия на отдельном PostgreSQL 16, Ruff/Black и документационные тесты.
- [x] Обновить roadmap и phase-1: clean-slate runtime принят, 0.3 закрыта только после тестов.
- [x] Зафиксировать изменения и ограничения развёртывания без секретов/счётов/операций.

## Execution ledger

- Scope: 0.3 follows the completed clean-slate sandbox launch; 0.8 remains next.
- Workspace: no Git history exists; no worktree or commits manufactured.
- Ruling: сохранить OPENING и матрицу spec, включая IN_QUEUE -> OPENING —
  enum уже входит в контракт, требования об удалении нет; если OPENING должен
  быть запрещён, потребуется отдельное изменение контракта и матрицы тестов.
- Task 1: implementation complete; 105 targeted tests, Ruff/Black pass; independent review APPROVED (95 tests).
- Task 2: Worker policy and four HTTP integration tests complete. Final review
  reproduced a stale-read race; DB CAS now guards state/revision/sequence before
  state proposals, hold and bootstrap side effects. Three real SQLite WAL races
  reproduced RED and passed GREEN; scoped re-review APPROVED (56 tests).
- Task 3: 48 targeted tests pass; scoped independent review approved (23 tests).
  Root review found a new retry suppression after audit-start failure; implementer
  reproduced and fixed it, 24 tests pass; scoped re-review APPROVED.
- Regression: 763 passed with isolated PostgreSQL 16; 16 develop tests passed;
  Ruff clean; Black checked 342 files. The old crash-recovery fixture relied on
  a same-state event; it now performs real IN_QUEUE -> IN_WORK and verifies
  unchanged durable outbox, recovery HOLD, retry and explicit resume.
- Deployment: images rebuilt; graceful Worker stop exit 0; Core/snapshot worker
  and Worker recreated with existing volumes; frontend API healthy. Four IN_WORK,
  one pre-existing HOLD/UNCERTAIN preserved; no forced resume. Full acceptance:
  `docs/milestone-0.3-acceptance-2026-09-09.md`.
