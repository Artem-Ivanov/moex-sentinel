# Владение данными и целостность торговых фактов

Текущая схема Core: `0001_baseline` → `0002_portfolio_snapshot_runs`.
Документ описывает действующий runtime после перехода на единую схему,
а не исторические таблицы `_v2` или импорт прежних хранилищ.

## Владельцы

| Данные | Владелец и хранилище | Ответственность |
| --- | --- | --- |
| `user_brokers`, `broker_instruments`, `instrument_sync_state` | Core, PostgreSQL | Подключения, справочник, состояние синхронизации |
| `trading_automations`, `position_cycles`, `position_lots` | Core, PostgreSQL | Подтверждённое состояние автоматов и позиций |
| `trade_decisions`, `broker_orders`, `broker_order_events`, `trade_executions`, `execution_lot_allocations` | Core, PostgreSQL | Подтверждённые решения, заявки, исполнения и атрибуция лотов |
| `automation_events`, `trade_audit_events` | Core, PostgreSQL | Принятые envelopes, последовательность и бизнес-аудит |
| `broker_account_fee_profiles`, `position_valuation_snapshots` | Core, PostgreSQL | Профили комиссии и оценки позиции |
| `portfolio_snapshots`, `portfolio_snapshot_runs` | Процесс снимков внутри Core, PostgreSQL | Сохранённая сводка портфеля и её сбор |
| `cached_automations`, `worker_runs`, `account_commission_profiles` | Worker, SQLite | Кэш команд, восстановление процесса и комиссии |
| `broker_intents`, `trade_decisions`, `trade_lots`, `lot_allocations`, `trading_cycle_states` | Worker, SQLite | Восстановимое состояние выполнения и принятия решений |
| `fact_outbox`, `business_audit_events` | Worker, SQLite | Доставка исходных фактов и локальная диагностика |
| Рыночные подписки, стаканы и история свечей | Core, память процесса | Один источник рыночных данных для Analytics |
| Адаптивные показатели рыночного снимка | Analytics, без постоянного хранилища | Расчёт по market-only контракту |

Одинаковые названия таблиц решений в Core и Worker не означают два независимых
источника торговых фактов. Worker атомарно сохраняет результат выполнения и
outbox; Core принимает этот результат по единственному typed-протоколу.
Worker не подключается к PostgreSQL и не изменяет Core ORM напрямую.

`execution_price_repair_journal` в каждой БД — отдельный постоянный журнал
выполненной коррекции цен, вне Alembic. Он сохраняется; восстановление проекций
учитывает завершённые коррекции по [runbook](../develop/execution-price-repair.md).

## Идентичность, связи и время

Группа фактов одного автомата принимается в одной транзакции Core.
UUID события и `(automation_id, sequence_number)` задают идентичность и
порядок; `expected_revision` защищает от записи поверх другого состояния.
Точный повтор принимается идемпотентно. Изменение содержимого уже принятого
события отклоняется; ошибочная группа не оставляет частичных записей.

Внешние ключи и составные ограничения связывают факты с подключением брокера.
Дополнительно repository проверяет принадлежность инструменту, автомату и
циклу внутри одного подключения. Отдельные корректные ссылки на объекты
одного брокера сами по себе не доказывают корректность их общей связи.
Эти проверки относятся к штатным repository/ingress-путям; произвольная SQL
запись в обход них не является поддерживаемым интерфейсом приложения.

Ключевые связи защищены FK с `ON DELETE RESTRICT`; UNIQUE и частичные индексы
ограничивают повторные факты и одновременно активные сущности. Явные времена
события, решения, исполнения, открытия, получения или оценки сохраняются в
соответствующих таблицах. Persisted-время нормализуется до UTC с точностью
миллисекунд; временная метка не заменяет идентификатор и порядковый номер.

## Проверяемые границы

| Инвариант | Основное покрытие |
| --- | --- |
| Миграции, актуальная ревизия и совместимость схемы | `tests/migrations/test_baseline_schema.py`, `tests/integration/postgresql/test_schema_runtime.py`, `test_core_runtime.py` |
| Ограничения моделей, scope, partial index и откат | `tests/storage/test_trading_facts_models.py`, `tests/integration/postgresql/test_trading_facts.py` |
| Семантика FK, UNIQUE и индексов миграций относительно ORM | `tests/integration/postgresql/test_schema_constraints.py` |
| Атомарная группа, ревизия, sequence и точный replay | `tests/services/test_trading_fact_ingress.py` |
| Durable outbox, выборочный ACK и восстановление | `tests/trading_automaton/storage/test_fact_outbox.py`, `test_local_repository.py`, `services/test_fact_synchronization.py` |
| Раздельные базы Core/Worker | `tests/integration/test_core_worker_lifecycle.py`, `tests/trading_automaton/storage/test_worker_database.py` |
| UTC и миллисекундная точность | `tests/contracts/test_trading_facts_contract.py`, `tests/integration/postgresql/test_schema_runtime.py` |
| Позиция, оценка и принадлежность связанных объектов | `tests/storage/test_position_ledger_repository.py`, `test_trading_analytics_repository.py` |

Стратегия определяется кодом и одна для всех позиций. Исторические параметры
решения и фактические адаптивные показатели сохраняются как факты расчёта;
каталога стратегий, назначений и новых полей бюджета позиции нет.
