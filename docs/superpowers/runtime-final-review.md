# Финальное независимое review runtime и snapshot migration

Дата: 2026-09-08.

**Verdict: APPROVED в проверенном scope. Открытых подтверждённых замечаний нет.**

Проверены точные исходные файлы и регрессионные тесты; HEAD отсутствует,
поэтому git diff не использовался. Основания: clean-slate typed runtime spec
2026-08-13, broker-account snapshot cache spec 2026-08-14, closeout plan
2026-09-08 и отчёты исполнителей. Исходный код reviewer не изменял.

## Проверенные исправления

- Worker outbox сохраняет bootstrap quartet целиком при малых batch limits,
  откладывает группу при retry одного участника и публикует последующий HOLD
  отдельным запросом. ACK префикса сохраняет speculative revision.
- Core до записи проверяет полный начальный quartet и соответствие immutable
  snapshot. Повтор принятой группы идемпотентен.
- Adoption после CLOSED создаёт последовательные детерминированные поколения
  automation/cycle/lot без изменения закрытых snapshots; повторное наблюдение
  активной позиции не создаёт дубликат.
- Readiness требует подготовленные market/portfolio/commission данные до ensure;
  начальный HOLD пропускает обычную hydration/reconciliation. Core status
  ограничивает команды runtime до подтверждения начальной активации.
- Collector сериализует запуск общим PostgreSQL advisory lock, отсекает
  существующие и ранее не встречавшиеся устаревшие buckets до broker reads,
  проверяет денежные значения и исключает счёт при cash movement внутри
  неопределённого интервала чтения.
- Миграция блокирует заменяемые таблицы перед проверкой пустоты; URL перед
  передачей в Alembic ConfigParser корректно экранирует процентные символы.

## Замечания review, закрытые до approval

1. **Speculative IN_WORK при Core CLOSED.** При pending bootstrap outbox
   synchronize_core_state сохранял локальный IN_WORK, а первый gate обрабатывал
   только Core HOLD. На реальной временной SQLite воспроизведена передача
   IN_WORK в bundle при Core CLOSED. Итоговый gate исключает неподтверждённые
   статусы и сохраняет HOLD до активации.
2. **NaN в стакане завершал preparation.** Сравнение NaN в order-book validator
   выбрасывало decimal.InvalidOperation. Исходный readiness regression падал;
   проверка finite prices теперь сохраняет HOLD до вызова validator.
3. **Фильтрация CLOSED обрывала восстановление ранее принятой позиции.**
   После bootstrap ACK cached command продолжает содержать bootstrap snapshot.
   При UNCERTAIN intent и последующем закрытии в Core list_active намеренно
   возвращает CLOSED для reconciliation. Первое исправление gate исключало
   эту команду целиком. Сценарий воспроизведён на реальном repository; итоговый
   gate передаёт эффективный CLOSED при active intent, сохраняя supervisor
   и исключая новые market ticks. Добавлен regression с SQLite и durable intent.

## Независимая проверка

После последнего исправления CLOSED supervisor:

```sh
.venv/bin/pytest -q tests/services/test_trading_fact_ingress.py tests/integration/test_open_position_bootstrap.py tests/trading_automaton/storage tests/trading_automaton/services/test_position_bootstrap_service.py tests/trading_automaton/services/test_fact_synchronization.py tests/storage/test_position_adoption_repository.py tests/services/test_position_adoption_service.py tests/usecases/test_position_adoption_usecase.py tests/trading_automaton/services/test_broker_tick_preparation_service.py tests/trading_automaton/services/test_broker_runtime_service.py tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py tests/trading_automaton/services/test_position_state_hydration_service.py tests/trading_automaton/test_streaming_composition.py --tb=short
```

**118 passed**, exit 0; 16 существующих SDK deprecation warnings.

После появления stale unseen bucket guard:

```sh
.venv/bin/pytest -q tests/services/test_portfolio_snapshot_collection.py tests/storage/test_portfolio_snapshot_repository.py tests/test_portfolio_snapshot_worker.py tests/integration/test_portfolio_snapshot_worker_isolation.py tests/migrations/test_schema_command.py tests/migrations/test_baseline_schema.py --tb=short
```

**40 passed**, exit 0; 16 существующих SDK deprecation warnings.

PostgreSQL concurrency/migration tests прочитаны, но reviewer не запускал их
повторно одновременно с PostgreSQL acceptance координатора. Полная матрица
проекта, Compose startup и операционный acceptance принадлежат основной задаче;
данное approval их не подменяет. Проверки reviewer используют только
синтетические fixtures и временные SQLite, без брокерских API и live trading.
