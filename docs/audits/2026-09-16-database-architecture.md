# Архитектура БД Core и Worker

2026-09-16, `/root/audit_review`. Read-only аудит текущего дерева, без runtime-запросов, изменения ORM/миграций и удаления данных. Измерение ниже использует отдельную SQLite `:memory:`. Это аудит схемы и traced consumers, не подтверждение содержимого/размера рабочей БД.

## Полный инвентарь

[Машинный инвентарь](../../develop/reports/architecture-performance/schema-inventory.json): **17 Core таблиц /248 колонок**, **10 Worker таблиц /134 колонки**. Для каждой таблицы перечислены все колонки, типы, nullable/default/PK, UNIQUE/FK/CHECK, индексы, DDL целевого dialect и source references ORM-класса. [Воспроизводимый скрипт](../../develop/reports/architecture-performance/inventory_schema.py) импортирует metadata без подключения к БД. References — навигация, не доказательство отсутствия использования: generic model_dump/constructor mappings также читают и записывают поля.

Core PostgreSQL управляется `alembic/versions/0001_baseline.py` → `0002_portfolio_snapshot_runs.py`; schema_revision проверяет точный head. Worker SQLite создаётся `Base.metadata.create_all` из composition. Repair journal `execution_price_repair_journal` вне ORM/Alembic отдельно описан в [data ownership](../data-ownership.md); его нет в количестве27 ORM-таблиц. Также не включён служебный alembic_version. Реальный drift схемы не проверялся: для него нужен read-only introspection конкретной БД.

## Владельцы и реальные пути чтения/записи

| Таблицы | Запись | Чтение/назначение |
|---|---|---|
| Core user_brokers | UserBrokerRepository, config/connection/adoption | broker/domain DTO, account/environment/SDK config |
| Core broker_instruments, instrument_sync_state | ReferenceCatalogRepository/reconciliation | catalog, command identity, scoped internal→external ID resolution |
| Core trading_automations | AutomationRepository, AutomationFactsRepository, adoption | lifecycle commands, CAS revision/sequence, public views |
| Core position_cycles | PositionLedgerRepository, mapper cycle handler | public position projection, lineage and latest valuation |
| Core position_lots, execution_lot_allocations | PositionLedgerRepository, mapper lot/allocation handlers | remaining lots, execution attribution, realized amounts; scoped FK checks |
| Core trade_decisions, broker_orders, broker_order_events, trade_executions | OrderFactsRepository from typed mapper | aggregate transitions, exact replay, order/execution lineage; position support |
| Core automation_events | TradingAuditRepository.append_envelope via ingress | find_envelope_matches + `_same_envelope`, exact retry/revision identity |
| Core trade_audit_events | mapper/audit.append_audit | immutable diagnostics, public repository list_audit currently has no production caller |
| Core broker_account_fee_profiles, position_valuation_snapshots | repository methods exist, **current runtime callers absent** | draft mappers/tests only, see DB-03 |
| Core portfolio_snapshot_runs, portfolio_snapshots | CollectPortfolioSnapshotsUsecase→store.save→repository, one commit | summary latest/common historical run, collection idempotency and cash-flow baseline |
| Worker cached_automations | cache_command, synchronize_core_state, bootstrap/state transitions | scheduler/recovery commands, account/instrument/identity, CAS state |
| Worker worker_runs | begin_run/finish_run | clean/unclean restart detection |
| Worker account_commission_profiles | upsert_commission_profile | exact composite-key schedule and TTL |
| Worker broker_intents | save_decision_batch/update_intent/finalize_execution | active intent, reconciliation, reservation recovery, execution replay |
| Worker trade_decisions | atomic decision batch save | order event lookup by intent_id, lineage and audit correlation |
| Worker trade_lots, lot_allocations, trading_cycle_states | atomic finalization and cycle/bootstrap paths | LIFO allocation, valuation, strategy cycle and recovery |
| Worker fact_outbox | same transactions as decisions/execution/state/audit | ready/retry/ACK/reject/reconcile, typed immutable transport facts |
| Worker business_audit_events | append_audit_events, bootstrap | local diagnostics and event-ID duplicate suppression; corresponding transport fact emitted in same transaction |

Эти пути проверены по repository/service bodies, а не только именам моделей. Core UoW создаёт5 repository над одной Session и коммитит **группу одного автомата**, не весь HTTP batch; failed peer не откатывает другую уже принятую группу. Worker finalize_execution сохраняет intent/ledger/cycle/outbox атомарно. Это оправданные границы агрегатов; перенос записи лотов в отдельный service commit разрушил бы recovery.

## Дублирование: что сохранять

- **Envelope payload и отдельные ORM поля фактов:** намеренный журнал исходного принятого контракта плюс queryable projections. `_envelope_draft` сериализует payload, `_same_envelope` сравнивает все поля кроме received_at; удаление payload или замена только hash меняет exact-replay/rebuild contract. Не доказано, что `safe_message`, `fact_kind`, sequence или revision являются лишними: они входят в сравнение и identity.
- **Worker outbox ↔ Core automation_events ↔ normalized facts:** разные владельцы и этапы доставки. ACK удаляет outbox, Core сохраняет accepted fact. Worker local decision/intents нужны для execution/restart до/после ACK. Не объединять базы и не удалять local payload, пока он участвует в повторной отправке.
- **Audit JSON и выделенные process/stage/ID поля:** JSON хранит различную диагностику, колонками обеспечиваются индексация/correlation и связь с canonical facts. Business audit не является источником исполнения или текущего P&L. Retention допустим только отдельно для diagnostic history, с сохранением accepted envelopes/replay/repair requirements.
- **Broker order state ↔ order events**, **cycle aggregates ↔ executions/lots**, **portfolio run ↔ snapshots:** mutable projection против истории/attribution и группового collection commit. Current P&L нельзя восстановить простым SUM(order.executed_amount), комиссии и lot_size требуют своих фактов. captured_at, executed_at, occurred_at, received_at и created_at не взаимозаменяемы.
- **Bootstrap snapshot ↔ cycle/lot:** pending transfer ownership. Чтение `_command`, `_validate_bootstrap_group`, ready quartet/reconciliation доказывает использование отдельных bootstrap IDs/quantity/time. Internal fact instrument ID и broker UID также имеют разные пространства идентичности.
- **fact_execution_id и execution_facts_emitted_at:** replay identity/marker, а не лишняя копия broker order ID. Их удаление способно повторно выпустить execution после рестарта.
- **TimestampMixin + объявления created_at/updated_at у CachedAutomationModel/WorkerRunModel:** возможная дублирующая декларация Python-кода. Metadata содержит ровно по одной колонке каждого имени; это **не** удвоение столбцов/данных и не причина SQL migration.

Безопасно удаляемых production-колонок аудит **не доказал**. Отсутствие explicit `model.field` не доказательство: draft serializers, raw SQL diagnostics и repair tools тоже consumers. Не предлагается массовая нормализация JSON или DROP COLUMN.

## Findings

### DB-01 — средний: Worker lookup по intent_id сканирует всю историю решений

**Место:** `src/trading_automaton/storage/models.py::TradeDecisionModel.intent_id`; `storage/repository.py::_append_order_state_fact` (query около1400), вызывается из update_intent/order lifecycle. Индекса intent_id нет; существующие PK(id), automation_id/process_id не подходят equality по intent.

**Сценарий:** растёт decision history, каждый state update заявки ищет decision по intent; ожидание SQLite writer path увеличивается несмотря на один matching row. Наличие большого количества WAIT с NULL intent не отменяет scan.

**Измерение:** [probe JSON](../../develop/reports/architecture-performance/worker-decision-index-probe.json), [скрипт](../../develop/reports/architecture-performance/probe_worker_decision_index.py): actual ORM SQLite schema,100000 synthetic decisions,10000 non-NULL intent,40 warm lookups. Без индекса `SCAN trade_decisions`, median **8.6046ms**; с обычным nonunique index(intent_id) `SEARCH ... USING INDEX`, median **0.06024ms**. Это локальная синтетика, не SLA/ускорение всей торговли; write overhead и реальные cardinality не измерены.

**Рекомендация:** один nonunique индекс intent_id; unique не нужен для ускорения и добавил бы новый business invariant. **Migration risk:** ORM `index=True` плюс create_all недостаточны для существующей Worker DB. Нужен отдельный явно вызываемый additive upgrade, создающий индекс checkfirst на уже существующей таблице, до запуска concurrent workers. Никакого DROP/rebuild/copy таблицы; index build может удержать SQLite write lock и потребовать места. Fresh-schema и existing-schema пути должны использовать один index name/definition. Core Alembic сюда не относится. Проверки: существующая БД без индекса→upgrade→index present, повторный upgrade идемпотентен, строки/заявки не меняются, query uses index, order lifecycle/replay targets green. Это **дизайн**, production не изменён.

### DB-02 — средний кандидат: Core summary читает portfolio_snapshots по неиндексированному run_id

**Место:** `storage/models/trading_analytics.py::PortfolioSnapshotModel.run_id`; `repositories/portfolio_snapshots.py::latest_snapshots` и последний fetch common_baselines. Реальные callers `TradingSummaryService.view/_periods` используют эти методы. FK(run_id) сам по себе не создаёт referencing-side индекс в PostgreSQL. Existing account/currency/captured и bucket constraints не имеют run_id слева.

**Сценарий:** lookup нескольких строк последнего run сканирует растущую историю account snapshots. **Рекомендация:** измерить PostgreSQL EXPLAIN ANALYZE/BUFFERS на отдельной representative БД; затем простой index(run_id), если план/latency подтверждают. Не обещать измеренный выигрыш: здесь проверен только query/index mismatch. **Migration risk:** новый Alembic revision, согласование metadata и миграции, блокировки/место при build; concurrent index требует отдельного transaction handling. Старые индексы без измерения не удалять.

### DB-03 — низкий/архитектурный: две Core analytical таблицы не имеют текущих runtime writers/readers

**Место:** `models/trading_analytics.py::BrokerAccountFeeProfileModel, PositionValuationSnapshotModel`; `repositories/trading_analytics.py`; `TradingFactsUnitOfWork.analytics` и protocol. `upsert_fee_profile`/`append_position_valuation` вызываются только tests/storage/test_trading_analytics_repository.py. Mapper nine fact types не вызывает analytics. Current commission schedule живёт в Worker, current position view читает PositionCycleModel; portfolio history имеет отдельные реально используемые таблицы.

**Сценарий:** docs ownership выглядит как действующая история position valuations/fee synchronization, но текущий runtime её не наполняет. Это доказательство неактивной capability, **не** доказательство отсутствия исторических данных или ненужности внешнего contract. **Рекомендация:** явно пометить capability dormant в ownership docs; принять отдельное решение поддержки/архивации. Не добавлять writer лишь ради использования поля. **Migration risk:** высокий для DROP — inspect/backup/data/export потребуются отдельно; сейчас таблицы сохранять.

### DB-04 — средний capacity risk: outbox limit не ограничивает чтение backlog

**Место:** Worker `ready_fact_outbox`1134–1180: selects все PENDING rows, затем группирует/проверяет retry/deadline в Python. Индекс(delivery_state,next_retry_at) помогает только первой части predicate; due time и limit не pushdown.

**Сценарий:** после длительной недоступности Core memory/scan растут пропорционально всему backlog при каждом flush. Это query-design, не лишние payload columns. **Рекомендация:** отдельное измерение и bounded selection, сохранив head-of-line blocking per automation, bootstrap quartet, oversized first group, deadline/count semantics и ordering. Простое WHERE retry<=now + LIMIT опасно: пропустит заблокированную голову и разделит atomic bootstrap. **Migration risk:** зависит от решения; schema change может не понадобиться. Не менять запрос только по интуиции.

## Индексы и ограничения: что не предлагать вслепую

Core composite scoped UNIQUE нужны как referenced keys для составных FK даже при глобальном UUID PK. UNIQUE(active automation/cycle) и scope/lineage checks — защита целостности, не дублирующий SQL шум. Core CHECK/FK не заменяют проверку общей связи инструмент→автомат→цикл в repository.

Worker fact_outbox отдельный automation_id index перекрывается левым префиксом unique(automation_id,sequence_number); это потенциальная write-cost избыточность, но DROP без measurements не рекомендован. TradeAuditRepository.list_audit фильтрует broker/automation и сортирует time/ID без composite index, однако текущего production caller не найдено: добавлять индекс ради неиспользуемого запроса не нужно. Worker намеренно слабее Core по FK/CHECK, значимая часть invariant в atomic repository; произвольные внешние SQL writes не поддерживаются.

## Следующий безопасный шаг

Сначала независимое review DB-01 additive index design и measured outbox candidate. Отдельно документировать dormant tables; ничего не удалять. Существующие migration/constraints/postgresql, fact ingress/replay, worker order finalization/outbox tests остаются обязательными. Полный metadata inventory не заменяет анализ production row counts, plans, retention и backups; эти ограничения явно сохранены.
