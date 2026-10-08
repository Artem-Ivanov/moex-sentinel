# Исправления слоистой архитектуры и SOLID — план реализации

> **Для исполнителей:** выполнять согласованные root пакеты по `superpowers:subagent-driven-development` либо `superpowers:executing-plans`; перед изменением поведения применять `systematic-debugging` и `test-driven-development`. Отметки ниже означают локальный результат, независимая приёмка указана отдельно.

**Цель:** закрыть три Important и шесть Minor принятого аудита, сохранив публичные контракты и durable execution/recovery.

**Архитектура:** consumer-owned узкие Protocol и нейтральные ошибки/DTO; SDK/HTTP translation остаётся в адаптерах. Lifecycle admission проверяется в той же Worker-транзакции, что создаёт intent. Ownership SDK session передаётся готовому bundle только после успешной сборки.

**Стек:** существующие Python, Pydantic, SQLAlchemy, FastAPI, SQLite, PostgreSQL и T-Invest SDK; новых зависимостей нет.

**Spec:** [принятый аудит](../../audits/layered-architecture-solid-20261006.md), [границы](../../../AGENT_BRIEF.md), [очередь](../../phase-1-implementation-plan.md). Явная команда пользователя 06.10: «тогда исправим эти замечания».

Модели Core/Worker исполнителей ниже фиксируют их фактическое назначение на
реализацию. Актуальная матрица профилей, ролей и моделей приведена в
[описании оркестрации](../../agent-orchestration.md). Для closeout-документации
назначен senior implementation/docs `gpt-6-luna/high`.

## Ограничения

- Новые Git-ветки, включая worktree с новой веткой, создавать только по отдельному явному распоряжению пользователя. Задание на исправление кода не разрешает ветку. Коммиты делает пользователь; агенты не выполняют stage, commit или push.
- Текущий checkout `master`, исходный HEAD `8d4805e47a39843c4f74619e75918d31ed459915`; исходный аудит и пользовательские изменения сохранены.
- Не менять frontend, стратегию, внешние API/JSON, очередь оптимизаций W3/C5, storage placement, зависимости или миграции.
- Не выполнять SSH, live broker API, TRADE, deploy; локальный тест TRADE admission использует fake broker и отдельную SQLite, без заявки.
- БД проверять только в памяти или на отдельном тестовом экземпляре; рабочие БД/volumes не затрагивать. PostgreSQL skip не означает PostgreSQL PASS.
- Сохранить immutable broker/account scope, archive/history, UTC milliseconds, TTL, lifecycle/sequence/revision, execution/ledger/cycle/outbox atomicity и recovery journals.
- Root утверждает декомпозицию до записи, назначает sole writers, ведёт CURRENT/WORK_LOG и интеграцию; author не принимает собственный пакет.

## Особое внимание ревью

1. Missing broker на accounts/internal connection/TRADE create: стабильный 404, без SDK I/O; validation и READ_ONLY priority прежние.
2. Две automation при HOLD/replacement между preparation и admission: stale command не создаёт intent и не удерживает provisional cash; текущая продолжает tick без ложного WAIT при бюджете ровно одной BUY.
3. Уже committed/sent intent после HOLD: tracking и recovery продолжаются, повторный submission исключён.
4. Failure/cancellation после SDK start: close ровно один раз, первичная ошибка не заменяется cleanup error.
5. Permanent/transient/ExceptionGroup/CancelledError: прежняя retry policy и безопасные diagnostics; quantity recovery — units, не lots.

## Владельцы и порядок

Root оркестрирует; Core senior `/root/core_fixes_senior` и Worker senior `/root/worker_fixes_senior` — `gpt-6.1-sol/medium`. Env investigator восстанавливает genuine pinned SDK с TLS verification. Shared contracts пишет только Worker senior. Малые junior пакеты запускает root после освобождения слота по профилю `gpt-6-luna/low`; другие исполнители их файлы не меняют. Независимый reviewer — `gpt-6.1-sol/high`, не автор реализации.

### 1. Core missing broker и актуальные порты — I1/M2

**Owner:** Core senior. **Файлы:** `src/moex_sentinel/domain/user_brokers.py`; `src/moex_sentinel/services/ports.py`, `connections.py`, `portfolio.py`, `portfolio_snapshot_collection.py`, `automations.py`, `automaton_brokers.py`, `market_data.py`, `instrument_catalog.py`, `brokers.py`, `broker_factory.py` в том же services; `src/moex_sentinel/usecases/portfolio.py`, `automations.py`, `automaton_brokers.py`.

**Контракт:** `UserBrokerNotFoundError` подставляется в доменный `BrokerRecordNotFoundError`; `UserBrokerLookupPort.get(str) -> UserBroker`, `UserBrokerReadPort.list() -> list[UserBroker]`; configuration writes используют `UserBrokerDraft`/archive.

- [x] RED: реальные repository/services/usecases в SQLite — 11 missing-broker failures, validation priority сохранён. `tests/usecases/test_missing_broker_contract.py`.
- [x] Минимальное исправление и локальный GREEN: 40 PASS с выбранной регрессией; `tests/services/test_broker_port_contract.py` проверяет immutable/archive identity и исключение archived из portfolio reads.
- [x] Actual HTTP composition в `tests/api/test_missing_broker_contract.py`: 12 check/accounts/market/catalog/internal connection/scope/TRADE create cases возвращают 404 `BROKER_NOT_FOUND`, adapter не создаётся.
- [x] Независимое ревью I1/M2 и archive/account regression.

### 2. Neutral persistence contracts — M1

**Owner:** Core senior пишет `src/moex_sentinel/domain/persistence_errors.py`, `storage/repositories/__init__.py`, `storage/repositories/automations.py`, `usecases/automations.py` и `tests/usecases/test_automation_errors.py`; префикс `src/moex_sentinel/` для этих файлов. Старые storage imports сохраняются через reexports одного класса; policy импортирует domain.

**Зарезервировано junior:** только `src/moex_sentinel/usecases/trading_fact_ingress.py` и `tests/usecases/test_trading_fact_usecases.py`. `ClaimAutomationCommandsPort.claim(limit: int) -> list[AutomationCommand]`, `AutomationStatusesPort.statuses(list[UUID]) -> AutomationStatusesResult`; заменить concrete repository type, wire behavior не менять.

**Worker owner:** senior, `src/trading_automaton/services/lot_ledger.py`; локальный Protocol реальных lot read/create/allocation capabilities, без дробления atomic repository.

- [x] Core neutral lookup/duplicate/revision error mapping и lifecycle regression — 13 PASS.
- [x] Junior narrow fact ports и fake с одной capability; M1/M4 assigned regression — 55 PASS, existing command/fact behavior сохранён.
- [x] Worker lot protocol wiring и lot behavior tests — 9 PASS.
- [x] Независимое ревью M1: внутренние слои не импортируют concrete storage.

### 3. Worker admission и rollback сборки — I2/I3

**Owner:** Worker senior. **Файлы:** `src/trading_automaton/storage/repository.py`, `services/streaming_batch_tick.py`, `usecases/broker_iteration.py`, `composition.py`; тесты `tests/trading_automaton/storage/test_local_repository.py`, `services/test_streaming_batch_tick_service.py`, `test_analytics_runtime.py`, `test_streaming_composition.py` под тем же tests/trading_automaton.

**Контракт:** текущий command проверяется до admission; revision команды не приравнивается к delivery/outbox revision. Проверка IN_WORK и создание intent находятся в одной транзакции. Bundle владеет session только после успешной сборки.

- [x] I2 локальный RED/GREEN stale HOLD admission; затем независимо перепроверен.
- [x] Barrier interleaving двух automation: stale не проходит, peer проходит; отозванная BUY во время await materialize освобождает provisional cash, чтобы соседняя актуальная BUY не получила ложный WAIT при бюджете одной BUY. Supervision ранее committed/sent intent и atomicity/CAS tests GREEN.
- [x] I3 genuine SDK RED 8 failures reservations/restore/cancellation после start; cleanup ровно один раз, освобождение internally owned Analytics client и сохранение первичной ошибки.
- [x] I3 минимальный cleanup и GREEN genuine SDK-dependent composition tests — 49 PASS.
- [x] Независимое ревью I2/I3; повторный recheck не нашёл замечаний.

Историческое промежуточное review `/root/fixes_integration_review` выявило ещё два Important внутри I2. Оба исправлены и повторно проверены:

- [x] Во время cash await следующей BUY отзыв предыдущей BUY освобождает provisional budget и пересчитывает уже полученный результат для текущей; barrier с real cash materializer и бюджетом одной BUY. RED 1 failed / 3 passed → GREEN 30 passed.
- [x] На file SQLite с двумя connections admission reads защищены реальной write transaction до state SELECT; conditional `BEGIN IMMEDIATE` только для нового intent сохраняет WAIT/finalize interleaving и fact sequence. RED 2 failed → targeted GREEN 131 passed. Admission-first сериализует HOLD за commit нового intent; stale CAS reject после него, свежий HOLD проходит.

Scope и DoD root зафиксировал до followups в `develop/WORK_LOG.md`, записи «независимый Important: cash revocation во втором await» и «независимый Important: SQLite admission transaction». Повторное независимое review I2 прошло без замечаний.

### 4. Neutral broker/transport failures и recovery DTO — M3

**Shared owner:** Worker senior. `src/sentinel_contracts/broker_errors.py`: `BrokerOperationError(code: str, safe_message: str, *, retryable: bool)`. `src/sentinel_contracts/broker_execution.py`: frozen `BrokerRecoveryOperation(operation_id, side: OrderSide | None, executed: bool, occurred_at: datetime, quantity_units: Decimal, price: Decimal, commission: Decimal, currency: str)`.

**Core owner:** senior, `src/moex_sentinel/adapters/tinvest/errors.py`, `request_errors.py`, `market_stream_source.py`; `services/connections.py`, `portfolio.py`, `portfolio_snapshot_collection.py`, `market_snapshot_gateway.py`, `portfolio_ports.py`, `market_data_ports.py`; `usecases/connections.py`, `market_data.py`, `instruments.py` с префиксом `src/moex_sentinel/`. `TInvestAdapterError` — compatibility subclass. Market adapter переводит пять permanent statuses: UNAUTHENTICATED, PERMISSION_DENIED, INVALID_ARGUMENT, FAILED_PRECONDITION, UNIMPLEMENTED; остальные market transport failures transient. Read adapter defaults не меняются.

**Worker owner:** senior, `src/trading_automaton/adapters/core_client.py`, `analytics_client.py`, `tinvest_broker_session.py`; `services/order_dispatch.py`, `uncertain_intent_reconciliation.py`, `analytics_frame.py`, `fact_synchronization.py`; `runtime/analytics_broker.py`. Recovery vendor operations переводятся в DTO в SDK adapter.

- [x] Shared safe error доступен; Core RED vendor-independent errors и permanent start/history, локальный GREEN — 54 PASS gateway/connection.
- [x] Genuine SDK boundary RED 35 failed / 1 passed → GREEN; `tests/adapters/test_tinvest_market_stream_source.py`, `test_tinvest_read_errors.py`, actual HTTP и ранее исключённые trading-session tests — 75 PASS. Read mapping/defaults сохранены; cancellation/unexpected identity на пяти операциях — 10 PASS; широкая Core regression — 820 PASS. Scoped independent review пройдено.
- [x] Worker transport/recovery failure tests: genuine SDK boundary selection — 81 PASS; transient/permanent/cancellation, no resubmission, quantities units при lot > 1 сохранены.
- [x] Независимое ревью M3 и adapter/application import boundaries.

### 5. Typed decision/batch и Analytics boundary — M4/M5

**M4 junior после стабилизации I2:** `src/trading_automaton/services/streaming_position_decision.py`, `services/streaming_batch_tick.py`, `domain/dtos.py`; сначала senior передаёт ownership общего tick файла. `PositionDecisionPort.decide(DecisionContext)`; `BatchTickResult.persisted: BatchPersistResult`; убрать permissive getattr defaults, fake возвращают реальные DTO.

**M5 Worker senior:** `src/trading_automaton/services/position_state_hydration.py`, `market_indicators.py`, `volatility_strategy.py`; проверять callers `market_data_bootstrap.py`, `runtime_decision_planner.py`, `broker_runtime.py` и `adapters/tinvest_streaming.py` до удаления неиспользуемых путей. Exact deletion paths root утверждает после расследования; не переносить вычисления обратно в active Worker.

- [x] M4 typed wiring, rollback/postcommit/CAS/TTL regression GREEN: junior assigned selection — 55 PASS; batch runtime fake followup RED 13 passed / 3 failed → GREEN 16 PASS. `_publish_committed_states(BatchPersistResult)` читает поля напрямую; scoped independent review пройдено.
- [x] M5 prepared mode не создаёт calculator Analytics; active Worker использует обязательные prepared metrics и не вызывает stream/history. Local M5 selection — 50 PASS; перенесённые calculator suites в Analytics — 55 PASS.
- [x] Caller graph проверен до удаления: удалены legacy `broker_runtime`, `market_data_bootstrap`, `tinvest_streaming` и calculator wrappers `market_indicators`/`volatility_strategy`; полезные planner/evaluator сохранены, stale ADR0007 получает датированное уточнение. Root ACK сохранён в журнале.
- [x] Независимое ревью M4/M5.

### 6. Направление импортов и общая приёмка — M6

**Зарезервировано junior:** `tests/test_architecture.py` после стабилизации пакетов; whole-src AST gate для shared/domain/application boundaries, учёт абсолютных/относительных импортов. Composition и adapter imports разрешаются явно по слою, не blanket blacklist всех cross-package names.

- [x] Negative fixtures RED обнаруживают запрещённые absolute/relative imports за пределами прежних трёх fact-path файлов.
- [x] Gate GREEN на согласованном дереве; runtime isolation smoke сохранён; architecture selection — 39 PASS.
- [x] Исторический backend baseline до PostgreSQL follow-up: 1825 passed, 166 skips, 17 warnings, exit 0; skips были вызваны отсутствием `POSTGRES_TEST_DATABASE_URL` в том запуске.
- [x] Датированный PostgreSQL follow-up: полный backend suite — 1991 passed, 0 failed, 0 skipped, 17 warnings, exit 0; 187 PostgreSQL cases PASS, оба paired restore PASS. Подробные receipts и ограничения приведены в [аудите исправлений](../../audits/layered-solid-fixes-20261006.md#postgresql-follow-up--06102026).
- [x] `ruff check`, `black --check`, `git diff --check`; независимый reviewer повторно проверил I2, M6, ADR clarification и DTO fakes без замечаний.
- [x] Финальный independent review всей интеграции, этих трёх документов и role/model matrix: PASS, Critical 0 / Important 0 / Minor 0. Root зафиксировал итог в CURRENT/WORK_LOG и очереди.

Доказательства: `develop/reports/layered-solid-fixes-20261006/`; Core логи — `core/`, shared/Worker — `worker/`. Веха принята как LOCAL DONE; незакрытых пунктов её scope нет. Runtime неизвестен; локальные результаты не означают deploy.

Финальный independent review `/root/fixes_integration_review` охватил интеграцию,
эти документы и role/model matrix: **PASS, Critical 0 / Important 0 / Minor 0**.
На момент этого review полный backend baseline завершился (1825 passed, 166
PostgreSQL skips, 17 warnings, exit 0); 448 source/test hashes совпали с
принятыми, HEAD diff check чистый. Отдельный PG follow-up дал 1991 passed,
0 failed, 0 skipped, 17 warnings, exit 0. Исходная кодовая веха принята как
**LOCAL DONE**; отдельное независимое review PostgreSQL evidence завершено:
`/root/postgres_compose_review` — PASS, Critical 0 / Important 0 / Minor 0.
Live runtime не проверялся.
Актуальная матрица — [отчёт исправлений](../../audits/layered-solid-fixes-20261006.md).
