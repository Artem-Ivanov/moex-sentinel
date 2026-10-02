# План упрощения кодовой и тестовой базы

> **Для исполнителей:** выполнять по вехам с `superpowers:subagent-driven-development`
> либо `superpowers:executing-plans`; перед реализацией прочитать решение и оба аудита.

**Цель:** убрать повторяющийся тестовый setup и необоснованную сложность кода,
сохранив торговые сценарии, и привести прикладные входы к юзкейсам.

**Архитектура:** конечные операции входят через юзкейсы; правила остаются в
сервисах/домене, I/O — в адаптерах. Изменения тестов и production-кода выполняются
разными авторами и проверяются отдельно, затем интеграция проходит третье ревью.

**Стек:** Python >=3.12, pytest, Pydantic, SQLAlchemy, SQLite/PostgreSQL,
FastAPI, Vue 3, TypeScript, Vitest; новые зависимости для рефакторинга не требуются.

**Решение:** [design](../specs/code-and-test-simplification-design.md).

**Исходные аудиты:** [тесты](../../audits/test-simplification.md),
[production-код](../../audits/code-simplification.md).

Нумерация вех этого документа задаёт единый порядок исполнения; локальные номера
этапов внутри аудитов служат пояснениями, а `C01–C16` — идентификаторами findings.

## Подтверждённая база анализа

В `tests/` и `develop/tests/`: 157 тестовых Python-модулей, 773 определения тестов,
33 fixture-функции; выявлено 5 групп одинаковых тел fixtures и 69 импортов из
`test_*` модулей. Collection обоих каталогов: 1198 cases, без исполнения тестов.
В frontend — 19 spec-файлов. Метрики являются сигналами для разбора, а не списком
обязательных удалений. Подробные определения и ограничения приведены в аудите.

**Статус:** пользователь разрешил реализацию 2026-09-15 и добавил исправление
отсутствующего графика свечей. Реализация вех и итоговая проверка завершены; фактические изменения
фиксируются в `develop/reports/code-test-simplification/`. Этот план дополняет
[эксплуатационный backlog](../../deployment/refactoring-performance.md).

**Ревью решения:** аудит тестов — `/root/fix_valuation`, аудит кода —
`/root/fix_delivery`, интеграция плана — `/root`, независимый рецензент —
`/root/audit_review`. Существенное замечание R1 исправлено и повторно проверено;
открытых существенных замечаний нет. Область проверки и ограничения — в
[отчёте ревью](../../audits/simplification-plan-review.md).
Первоначальное решение рассмотрено пользователем до реализации.

## F0. Отсутствующий график минутных свечей — первый bugfix

**Пример:** `/positions/ab0ea88f-7f7a-5b9b-84d3-23bf82e62dc1`, SIBN.
В 19:04 UTC локальный `/details` вернул `candles=[]` и
`MARKET_CANDLES_UNAVAILABLE`. Прямой market-запрос с внутренним catalog ID получил
409, с внешним broker UID — 200 и 112 завершённых свечей за тот же двухчасовой
интервал. См. `develop/reports/code-test-simplification/candles-190458.json`.

**Причина:** details usecase передаёт `automation.instrument_id` (Core ID)
в generic `BrokerMarketDataService.candles`, чей контракт — внешний ID брокера.
SDK отклоняет запрос, frontend закономерно не создаёт SVG без свечей.

**Решение:** добавить catalog-scoped чтение свечей в существующий
`InstrumentCatalogService`: разрешить `(broker_id, internal_id)` через каталог,
запросить adapter с `external_instrument_id`, вернуть завершённые свечи.
Composition передаёт этот сервис только в candle-port details usecase.
Generic market API продолжает принимать внешний ID; бизнес-данные автомата и
торговый Worker не изменяются. Ошибку получения свечей в UI отличать от успешного
пустого ответа, чтобы одновременно не показывать «свечей нет» и ошибку запроса.

- [x] Отдельный автор тестов воспроизводит неверный internal→external переход,
  проверяет broker scope, точный двухчасовой interval, complete filter и wiring.
- [x] Независимое ревью решения F0 до изменения production-кода.
- [x] Автор кода исправляет catalog resolution и composition; frontend убирает
  противоречивое empty-сообщение при ошибке (отдельный UI regression).
- [x] Целевые backend/frontend тесты, code review, затем локальная проверка
  `/details` и видимого графика после обновления нужных сервисов. Никаких миграций.

Проблемы масштаба, округления и timezone здесь не заявлены причиной отсутствующего
графика; они рассматриваются только при отдельном воспроизведении.

При браузерной проверке восстановленного графика обнаружено отдельное
переполнение CSS Grid: intrinsic width SVG растягивал всю страницу. Браузерный
RED на трёх размерах дал ширину страницы 2622/2555/2288 px при viewport
1425/753/485 px. Общий `main { min-width: 0; }` сохраняет desktop и responsive
grid, ограничивает main и оставляет прокрутку у графика. GREEN: ширина страницы
равна viewport во всех трёх случаях; chart scrollWidth > clientWidth.
Артефакты: `chart-layout-before.json`, `chart-layout-after.json`,
`f0-layout-design-review.md` в каталоге текущей реализации.

## Общие ограничения

- Ни один шаг не обращается к рабочей БД или брокеру. PostgreSQL — отдельный тестовый экземпляр.
- Сохранять API/DTO/error codes, порядок фактов, транзакции, торговые состояния и данные.
- Не удалять проверки отсутствия данных без доказательства контракта всех вызывающих путей.
- Один тест — один сценарий; несколько assertions одного результата допустимы.
- Общие фикстуры по смыслу и ADR 0004; mutable state не делится между тестами.
- Перемещения тестов сначала 1:1; параметризация и удаление дублей следующим diff.
- Упрощение не должно создавать usecase на каждую функцию, универсальный test builder
  с десятками флагов или общую retry/error-abstraction для разных политик.
- Каждый затронутый модуль получает содержательные docstrings функций/методов
  в рамках своей вехи; не откладывать документацию до конца всего проекта.
- Никакого процента сокращения tests/LOC заранее: удаление оценивается по сценариям.

## Порядок и распределение

| Веха | Автор | Зависимости | Независимый рецензент |
|---|---|---|---|
| T0: карта сценариев и baseline | Агент тестов | Одобренное решение | Агент кода |
| T1–T3: фикстуры и сценарии Python | Агент тестов | T0; последовательно по пакетам | Агент кода |
| T4: frontend-тесты | Агент тестов | T0; можно параллельно Python-коду после фиксации границ | Агент кода |
| C1: локальные упрощения/ошибки | Агент кода | T0 и стабильные fixtures затронутых suites | Агент тестов |
| C2: прикладные границы Core/Analytics | Агент кода | C1; по одной операции | Агент тестов |
| C3: Worker usecases и reachable code | Агент кода | C1; T1–T3 для затрагиваемых suites | Агент тестов |
| C4: storage и frontend lifecycle | Агент кода | Соответствующие C2/C3/T4 | Агент тестов |
| C5: оставшиеся contracts/DI/mapper | Агент кода | C1–C4 по затрагиваемому пути | Агент тестов |
| G: интеграция и документация | Основной агент | Все выбранные вехи | Третий агент, не автор интеграции |

Нельзя одновременно менять фабрику/фикстуру и её production-потребителя в разных
ветках без зафиксированного контракта. На переходе к C-вехам заморозить соответствующий
T-diff. Новые регрессионные тесты на найденный дефект поручать агенту тестов до
исправления; автор кода воспроизводит failure. Чистый перенос проверяется прежними тестами.

## T0. Baseline и карта сценариев

**Файлы:** `develop/scripts/audit_test_structure_20260915.py`, `tests/`,
`frontend/src/**/*.spec.ts`; создаваемый отчёт
`develop/reports/code-test-simplification/scenario-map.md`.

- [x] Повторить структурную инвентаризацию на выбранном commit; отделить AST test
  functions, collected cases, параметризации и время исполнения. Исторический
  recovery-прогон не выдавать за проверку новой версии.
- [x] Сохранить коллекцию `pytest` и результаты Vitest в `develop/reports/code-test-simplification/`.
- [x] Для первой затрагиваемой группы перечислить old nodeid, контракт, new nodeid/param id,
  вид переноса и причину удаления, если оно предлагается. Расширять карту в каждой вехе.
- [x] Зафиксировать отдельные сценарии recovery: повтор без submit, пустой broker ID,
  delayed BUY/SELL, stale hydration, ms precision, rollback и повторный ACK.

```sh
.venv/bin/python develop/scripts/audit_test_structure_20260915.py
.venv/bin/pytest --collect-only -q tests develop/tests
npm --prefix frontend test
```

**Результат:** проверяемая карта сценариев; сравнение количества функций не подменяет
сравнение поведения. Полный baseline выполняется командами G на отдельной БД.

Отмеченные пункты T1–T4 относятся к выбранным группам из `develop/reports/code-test-simplification/scenario-map.md`; остальные межтестовые импорты и различающиеся варианты setup не объявляются мигрированными.

## T1. Явные фабрики и ресурсы вместо скрытой совместимости

**Изменять:** `tests/conftest.py`, `tests/trading_automaton/command_factory.py`,
`tests/storage/trading_facts_helpers.py`, использующие их модули из аудита.
**Создавать по подтверждённым потребителям:** тематические `factories.py` и `conftest.py`
в тестовом пакете; точная карта файлов приведена в аудите тестов.

- [x] Сначала перенести используемые вне модуля helpers из `test_*.py` в тематические
  support-модули без изменения значений, assertions или порядка действий.
- [x] Отделить чистые DTO-фабрики от фикстур, владеющих Session/engine/client.
  Переиспользовать существующий `command(...)`, а не создать конкурирующую фабрику.
- [x] Заменить зависимость от глобальных `dataclasses.replace/asdict` patches:
  Pydantic DTO создавать через явный валидирующий конструктор; настоящие dataclass
  оставлять со стандартным `dataclasses.replace`. `model_copy(update=...)` не
  считать повторной валидацией и не менять им semantics invalid-input теста.
- [x] Удалить shim только после миграции всех найденных потребителей в `tests/`
  и отдельно запускаемых `develop/tests/`. Async pytest hook не переписывать попутно.
- [x] При выносе БД setup сохранить тип соединения, SQLite pragmas, StaticPool,
  `check_same_thread`, commit/rollback и teardown там, где они существенны.

**Пример политики фабрики:** существующий
`command(state=AutomationState.HOLD, bootstrap=...)` оставляет начальное состояние
в тесте; fixture не должна автоматически переводить этот автомат в IN_WORK.

```sh
.venv/bin/pytest -q tests/contracts tests/sentinel_contracts tests/storage tests/trading_automaton
.venv/bin/pytest --collect-only -q develop/tests
```

**Критерий:** старые сценарии сопоставлены 1:1, исчезают cross-imports из `test_*`
для выбранной группы, ресурсы освобождаются. Дополнительные `develop/tests` запускаются
по их зависимостям на тестовом окружении до удаления глобального shim.

## T2. Фикстуры хранилища и изоляция

**Изменять:** `tests/storage/`, `tests/trading_automaton/storage/`,
`tests/integration/postgresql/conftest.py`, `tests/services/test_portfolio_snapshot_collection.py`.

- [x] Вынести одинаковое владение engine/session factory в function-scoped yield fixtures.
  Данные для сценария сеять явно; не создавать готовый торговый граф всем тестам autouse.
- [x] Reopen/persistence тестам оставить файловую SQLite через `tmp_path`, обычным
  SQL unit — их текущий режим; PostgreSQL constraints не заменять SQLite.
- [x] PostgreSQL оставить отдельные schema/migration/cleanup на тест. Не переносить
  schema на session scope ради скорости и не подменять рабочий URL переменной тестовой БД.
- [x] Проверить сбой setup/исключение и teardown существующими сценариями владения
  ресурсами. Добавлять отдельный тест лишь если появляется новая сложная lifecycle-логика.

```sh
.venv/bin/pytest -q tests/storage tests/trading_automaton/storage tests/services/test_portfolio_snapshot_collection.py
.venv/bin/pytest -q tests/integration/postgresql
```

**Критерий:** повторяемый setup убран, тестовые ресурсы изолированы, после ошибки
тест не оставляет Session/task и не влияет на следующий сценарий. PostgreSQL suite
без настроенного изолированного URL не считается выполненным.

## T3. Один сценарий и параметризованные варианты

**Изменять:** конкретные группы аудита в `tests/trading_automaton/services/`,
`tests/storage/`, `tests/api/`, `tests/integration/postgresql/`, а также
`tests/integration/test_net_position_facts.py`.

- [x] После T1/T2 разделить независимые действия длинных тестов. Не разрывать
  read-after-reopen, replay, CAS/rollback и повторное исполнение: их причинная цепочка — сценарий.
- [x] Одинаковые state/error/threshold варианты оформить одной функцией с явными
  `pytest.param` IDs. Не объединять тесты только по схожему названию.
- [x] Для существующих таблиц параметров добавить осмысленные IDs, начиная с торговых
  матриц и контрактов ошибок. Пример ID: `freshness-exactly-2s`, `stale-by-1ms`,
  `future-by-1ms` в `test_order_book_validation_service.py::test_order_book_time_boundaries`.
- [x] API оставить минимальный smoke для wiring/serialization/error rendering;
  повтор бизнес-матрицы удалить только при наличии соответствующего service/usecase
  теста и доказанном отсутствии transport-specific assertions.
- [x] Для каждого удаления заполнить карту old→new. Не удалять PostgreSQL дубль,
  если он проверяет unique index/реальный rollback, которых нет у fake/SQLite.
- [x] Для `test_next_tick_keeps_lifo_basis_when_broker_average_differs_after_partial_sale`
  выделить LIFO valuation, raw quote +65 µs, older quote, stale hydration и delayed
  fill сценарии по 13-строчной карте аудита. Side BUY/SELL параметризовать там, где
  сохраняется общий поток действий; закрытие цикла и комиссии проверять явно.

```sh
.venv/bin/pytest -q tests/trading_automaton/services tests/api tests/storage
.venv/bin/pytest -q tests/integration/postgresql
.venv/bin/pytest -q tests/integration/test_net_position_facts.py tests/integration/test_open_position_bootstrap.py
```

**Критерий:** сценарий понятен без чтения helper; тест не содержит ветвления по имени
кейса; покрытие сохранено. Сокращение строк и количества setup-копий измерено на diff.

## T4. Повторения frontend-тестов

**Изменять:** `frontend/src/api/*.spec.ts`, `frontend/src/views/*.spec.ts`,
`frontend/src/components/*.spec.ts` согласно аудиту.

- [x] Общий синтетический HTTP response/fixture выделить только при одинаковом
  контракте; endpoint, payload, действие и expected error оставить в конкретном тесте.
- [x] Параметризовать одинаковые HTTP ошибки через `it.each`; проверки DOM, запроса
  и пользовательского сценария не смешивать в одну матрицу.
- [x] Общий mount helper не должен скрывать router state, polling и время.
  Timers/mocks/unmount очищаются после каждого теста.
- [x] Сохранить сценарии unmount при незавершённом запросе и обновления выбранной
  сущности; если обнаружен дефект, новый тест передать в C4 как отдельный bugfix.

```sh
npm --prefix frontend test
npm --prefix frontend run typecheck
```

## C1. Малые упрощения с доказанными контрактами

**Изменять:** `src/moex_sentinel/usecases/{automations,instruments,market_data,connections}.py`,
`src/trading_automaton/services/position_state_hydration.py`,
`src/trading_automaton/services/streaming_batch_tick.py`, соответствующая composition.

- [x] Заменить универсальные `_automation_error/_catalog_error/_market_error`
  конкретными `except` ожидаемых ошибок, сохранив коды и безопасные сообщения.
  Случаи неожиданной ошибки проверить на внешнем fallback: изменение статуса 500
  или safe detail не включать молча в рефакторинг.
- [x] Для `PositionConsistencyResult` заменить `getattr` прямыми атрибутами после
  проверки всех реализаций порта; test doubles должны соблюдать настоящий контракт.
- [x] Убрать неиспользуемый параметр `commissions` из `StreamingBatchTickService`
  и wiring, убедившись, что вычисление комиссий остаётся у фактического владельца.
- [x] По реестру аудита разбирать пустые/default ветки по одной: доказательство
  недостижимости → удаление → целевой сценарий. Внешние пустые данные не «исправлять» дефолтом.
- [x] Добавить docstrings для затронутых функций: результат, ошибка, side effect.

```sh
.venv/bin/pytest -q tests/usecases tests/api tests/trading_automaton/services/test_position_state_hydration_service.py tests/trading_automaton/services/test_streaming_batch_tick_service.py
```

## C2. Core и Analytics: конечные прикладные операции

**Изменять:** `src/moex_sentinel/views/internal_market.py`,
`src/moex_sentinel/services/portfolio_snapshot_collection.py`,
`src/moex_sentinel/portfolio_snapshot_worker.py`, `src/moex_sentinel/composition.py`,
`src/market_analytics/app.py`, `src/market_analytics/service.py`.
**Новые границы:** `src/moex_sentinel/usecases/market_snapshot.py`,
`src/moex_sentinel/usecases/portfolio_snapshots.py`, `src/market_analytics/usecases.py`.

- [x] `GetMarketSnapshotUsecase.execute(MarketSnapshotRequest) -> MarketSourceSnapshot`
  становится входом internal_market; HTTP 404/409 и безопасные details сохраняются
  в transport renderer. Gateway продолжает владеть рыночным I/O/cache.
- [x] `CollectPortfolioSnapshotsUsecase.execute() -> PortfolioSnapshotRunResult`
  получает конечную orchestration `collect_once`; worker оставляет только цикл,
  ожидание и завершение. Сохранить advisory lock, skip minute, partial account errors
  и единый save; инфраструктуру Session/Engine держать за узким портом/UoW.
- [x] `CalculateAnalyticsSnapshotUsecase.execute(AnalyticsSnapshotRequest) -> AnalyticsSnapshot`
  получает HTTP-вход Analytics. Чистые индикаторы остаются функциями/сервисами;
  создание зависимостей — composition `create_app`, не constructor сервиса.
- [x] Каждую из трёх операций интегрировать отдельным diff и отдельной проверкой;
  сначала переместить соответствующие тесты 1:1, затем менять DI.
- [x] Устранить C10 отдельно: `SynchronizeBrokerInstrumentsUsecase` вызывает общий
  adoption Service вместо `AdoptBrokerPositionsUsecase.execute`. Самостоятельный
  adoption usecase остаётся входом своего актора; ACTIVE/account/category проверяет
  Service с типизированным broker DTO. Две транзакции не называть общей атомарной.
- [x] Для `views/health.py` вынести DB/schema readiness в одну техническую операцию
  через DI; транспорт отображает ответ. Не создавать отдельный торговый юзкейс
  на каждую техническую проверку. Сохранить ready/down/incompatible HTTP контракт.

```sh
.venv/bin/pytest -q tests/api/test_internal_market.py tests/services/test_market_snapshot_gateway.py tests/services/test_portfolio_snapshot_collection.py tests/test_portfolio_snapshot_worker.py tests/market_analytics tests/test_composition.py
.venv/bin/pytest -q tests/integration/postgresql/test_portfolio_snapshots.py
.venv/bin/pytest -q tests/usecases/test_instrument_catalog_usecases.py tests/usecases/test_position_adoption_usecase.py tests/api/test_health.py tests/api/test_instrument_catalog.py
```

**Критерий:** транспорт/таймер не выполняет бизнес-orchestration; нет пустой цепочки
классов ради названия. Ошибки, данные и транзакции прежние. Новые usecase suites
включаются в целевой запуск после их создания.

## C3. Worker: карта операций перед переносом

**Изменять:** `src/trading_automaton/__main__.py`, `composition.py`,
`services/streaming_runtime_coordinator.py`, `services/analytics_runtime.py`;
создать тематические `usecases/` и `runtime/` только для выбранных переносов.

- [x] Зафиксировать reachable graph от CLI/composition. Для старых supervisor,
  broker streaming и альтернативных batch paths найти всех callers. Если путь
  существует только ради собственных тестов и не является поддерживаемым контрактом,
  предложить его удаление отдельным diff вместе с исключительно его тестами.
- [x] Конечная control-операция `run_iteration` и рыночная `run_once` становятся
  отдельными прикладными входами. `run/close`, tasks, timers и stop event остаются lifecycle.
  Контрольная операция не вызывает рыночный usecase: runtime запускает их независимо.
- [x] Startup `repository.begin_run` + `RecoveryService.recover` из `__main__.run`
  перенести в `RecoverWorkerRunUsecase`: это третья самостоятельная операция до
  запуска циклов. Clean-shutdown marker и закрытие ресурсов остаются lifecycle
  через порт; startup recovery не вызывается из broker iteration.
- [x] Разделить orchestration и lifecycle на основе карты из production-аудита.
  Общие этапы preparation/reconciliation/dispatch/sync остаются сервисами; не
  добавлять вложенную цепочку юзкейсов вокруг каждой стадии.
- [x] Сохранить lock вокруг tick, dedup snapshot/profile, freshness и контроль
  изменения команд во время await; закрытие ждёт текущую операцию до закрытия клиента.
- [x] Сохранить наблюдение HOLD с active intent, reconciliation при Analytics outage,
  flush/claim/sync order, bootstrap restrictions и независимый heartbeat.
- [x] Удалить старый путь только после переключения composition и прохождения
  прежних behavior tests; не держать два production pipeline как временную норму.

```sh
.venv/bin/pytest -q tests/trading_automaton/test_analytics_runtime.py tests/trading_automaton/test_analytics_recovery.py tests/trading_automaton/test_streaming_composition.py tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py tests/trading_automaton/services/test_uncertain_intent_reconciliation_service.py tests/trading_automaton/services/test_broker_tick_preparation_service.py
```

**Критерий:** для каждой действующей внешней/планируемой конечной операции указан
один application owner; lifecycle не вызывает бизнес-репозитории. Отсутствие `usecases/`
само по себе не доказывает нарушение конкретного класса: проверяется направление вызовов.

Минимальная карта новых Worker-входов:

| Актор | Application owner | Состояние и граница |
|---|---|---|
| Startup CLI | `RecoverWorkerRunUsecase` | begin-run/recovery до запуска задач; не второй broker loop |
| Control loop | `SynchronizeTradingRuntimeUsecase` | flush/claim/reconcile; bundle task ownership остаётся runtime |
| Отдельный broker loop | `RunBrokerIterationUsecase` | fetch/preparation/selection/tick/dedup; одна копия commands/last_seen и один run lock |

Имена задают целевые границы; при переносе сохранить входы/выходы существующих
операций. У владения `_commands`, `_last_seen`, `_run_lock`, heartbeat task и
runtime bundles должен быть ровно один явно указанный хозяин в code review.

## C4. Storage и frontend: отдельные небольшие изменения

**Storage:** `src/trading_automaton/storage/repository.py::_finalize_trading_cycle`,
`src/trading_automaton/services/trading_cycle.py`, затронутые domain DTO.

- [x] Убрать создание `TradingCycleService` внутри repository: чистый расчёт
  перехода разместить в domain-функции, доступной сервису и repository без обратного
  импорта слоя. Сохранить persisted state read, CAS и запись фактов в той же Session.
- [x] Дальнейшее разделение большого repository — только по связным обязанностям
  с той же транзакционной границей. Оптимизацию outbox SQL и LIFO агрегации оставить
  отдельным измеряемым пунктом прежнего performance-плана.

```sh
.venv/bin/pytest -q tests/trading_automaton/services/test_trading_cycle_service.py tests/trading_automaton/storage tests/services/test_trading_fact_ingress.py
.venv/bin/pytest -q tests/integration/postgresql/test_trading_facts.py
```

**Frontend:** `frontend/src/api/*.ts`, `frontend/src/components/TradingSessionStatus.vue`,
`frontend/src/views/PositionDetailsView.vue`, соответствующие `.spec.ts`.

- [x] Убрать повторное чтение/error parsing и перехват только что созданной собственной
  ошибки; общий HTTP helper имеет один транспортный контракт, не содержит бизнес-решений.
- [x] Проверить race `await initial fetch → unmount → setInterval`; исправлять
  найденный дефект отдельным тестом и diff, не скрывать bugfix в переименовании.
- [x] Общий polling composable выделять только при совпадении lifecycle у реальных
  потребителей. Если эффекты различны, оставить простую локальную реализацию.

```sh
npm --prefix frontend test
npm --prefix frontend run build
```

## C5. Остальные подтверждённые точки упрощения

Эта веха состоит из независимых diff; общий большой rewrite не нужен.

| Finding / файлы | Действия и граница | Проверка |
|---|---|---|
| C12: `services/{streaming_cycle_transition,streaming_batch_tick,streaming_runtime_coordinator,streaming_position_decision,decision_context,analytics_runtime}.py` и composition | Создание Services/settings/validators перенести в composition; обязательные зависимости сделать явными. Реальный optional режим сохранить, не заменить его no-op сервисом ради удаления if. | Прежние service и composition suites с фактическими одинаковыми settings; отмена/close/retry прежние |
| C13: `moex_sentinel/adapters/tinvest/{portfolio,market_data}.py`, `trading_automaton/services/order_dispatch.py` | По установленному SDK определить типы ошибок. Ограничить catch SDK-вызовом, отдельно преобразовать malformed response; Service получает типизированную ошибку порта. Общая adapter-функция только для действительно одного SDK-контракта. | Adapter auth/timeout/rate-limit, converter failure, безопасное сообщение без secret; dispatch до/после submit |
| C14: `services/trading_fact_mapping.py`, `services/decision_materialization.py` соответствующих пакетов | Явный `match` сохранить; крупные fact handlers выделить в private typed methods. Request и IntentBatchItem создавать из одного выбранного outcome, убрать дублирование nullable локальных переменных. | WAIT без intent, BUY/SELL с точным quantity/price, cash reservation, replay/lineage, raw-vs-ms время |
| C16: `sentinel_contracts/base.py` и DTO callers | Составить constructor inventory и перевести сложные вызовы на keyword arguments. Сам positional metaclass пока сохранить; удаление требует отдельного решения о поддержке constructor API после миграции всех callers. | Constructor validation, keyword/positional precedence, JSON/schema и единицы DTO |

```sh
.venv/bin/pytest -q tests/adapters tests/trading_automaton/adapters tests/trading_automaton/services tests/services/test_trading_fact_ingress.py tests/contracts tests/sentinel_contracts tests/trading_automaton/test_streaming_composition.py
```

Docstrings C15 входят в каждую C-веху. Исправить описание
`TradingFactsUnitOfWork`: атомарна группа автомата, а не произвольный общий batch.
Достаточность docstrings проверяется на функциях и методах, а не только на классах.
Не вводить метрику количества docstrings как замену ясному контракту.

## Карта покрытия production-аудита

| Findings | Вехи |
|---|---|
| C01, C07 | C1 |
| C02, C04, C05, C10 | C2 |
| C03, C11 | C3; C11 удаляется только после доказательства отсутствия поддерживаемого контракта |
| C06, C08, C09 | C4; C09 отдельный воспроизводимый bugfix |
| C12, C13, C14 | C5 |
| C15 | Каждая C-веха + G |
| C16 | C5: keyword migration; удаление metaclass отдельно после решения о compatibility |

## G. Итоговая интеграция и проверка

**Документы:** `docs/architecture.md`, `docs/development.md`,
`docs/decisions/0005-application-layering-pattern.md`, карты двух аудитов.

- [x] Согласовать фактический runtime→usecase путь с архитектурным документом;
  ADR описывает правило, архитектура — реализованные входы. Не менять статус
  незавершённой вехи на готовый из-за написанного плана.
- [x] Проверить все четыре Python-пакета и frontend по матрице аудита: выполнено,
  сохраняется с обоснованием или отдельная следующая веха. Не заявлять «весь код
  приведён к usecases», пока остаются непроверенные прикладные входы.
- [x] Зафиксировать docstring coverage для функций по подсистемам, исключения и
  качество контракта при code review, а не только наличие строки.
- [x] Проверить направление зависимостей независимым code review по ADR 0005.
  Не создавать тесты структуры файлов или AST-графа импортов. DI подтверждать
  поведением Service/Usecase с внедрёнными fake/stub, а не формой реализации.
- [x] На отдельной PostgreSQL выполнить полный backend прогон; frontend — test/build.
  Сохранить warnings/skips и время, сравнить карту сценариев, а не только итоговый count.
- [x] Третий агент проверяет integrated diff, соответствие задаче, SOLID без лишних
  слоёв, teardown, invariant/replay coverage. Исправить и повторно проверить существенные замечания.

```sh
git diff --check
POSTGRES_TEST_DATABASE_URL="${POSTGRES_TEST_DATABASE_URL:?Set a dedicated disposable PostgreSQL URL}" .venv/bin/pytest -q tests develop/tests
npm --prefix frontend test
npm --prefix frontend run build
```

Перед backend-прогоном `POSTGRES_TEST_DATABASE_URL` должен указывать только на
заранее подготовленный отдельный тестовый PostgreSQL. Миграционные тесты создают
и удаляют schemas. Проверить линтером изменённые Python-файлы; не применять
автоисправления линтера ко всему рабочему дереву.

**Итоговый отчёт реализации:** сохранённые/объединённые/удалённые сценарии с причинами,
уменьшение дублирующего setup и сложности выбранных функций, результаты проверок,
авторы и рецензенты, оставшиеся ограничения. Ручной SSH/Git деплой и его инструкция
продолжают действовать; развёртывание не является шагом этого плана.
