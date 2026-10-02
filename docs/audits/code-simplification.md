# Аудит production-кода перед упрощением

Дата: 2026-09-15. Автор анализа: `/root/fix_delivery`. Статус: **план до реализации**,
независимое ревью выполнено `/root/audit_review` в составе
[пакета решения](simplification-plan-review.md).
Исходники, тесты и runtime в рамках этого аудита не менялись.

Основания: `AGENTS.md`, [ADR 0005](../decisions/0005-application-layering-pattern.md), [архитектура](../architecture.md), [ADR 0007](../decisions/0007-trading-automaton-runtime-decision-coordinator.md), [план эксплуатации и производительности](../deployment/refactoring-performance.md), [общее решение](../superpowers/specs/code-and-test-simplification-design.md).

## Вывод

Наибольший выигрыш дадут устранение универсальных exception-mapper, перенос уже существующей orchestration в правильные application boundaries, удаление доказанно лишних зависимостей/динамических проверок и разделение владения транзакциями и расчётами. Массовое добавление классов `Usecase`, удаление `None`-проверок или замена явного `match` универсальным registry сделают код сложнее.

Это статический обзор всех четырёх Python-пакетов и основных frontend-путей, с подробным чтением перечисленных ниже символов. Наличие конструкции подтверждено исходниками; предполагаемое влияние на производительность не измерялось. Аудит не доказывает отсутствие иных дефектов. Тестовый план и карта сохранения сценариев составляются отдельным агентом и интегрируются root.

## Покрытие

| Подсистема | Просмотренные области | Основные выводы |
|---|---|---|
| Core HTTP/application | `views/{health,internal_market,errors}.py`; `usecases/{automations,instruments,connections,position_adoption,trading_fact_ingress,trading_summary}.py`; composition | Обход Usecase, вложенные Usecase, широкие mapper и нетипизированная зависимость `object`. |
| Core services/domain | `services/{automations,broker_factory,market_snapshot_gateway,portfolio_snapshot_collection,trading_fact_ingress,trading_fact_mapping,trading_summary}.py`; `domain/{trading_summary,user_brokers,brokers}.py` | Сохранить чистые финансовые расчёты; отделить сборку инфраструктуры и lifecycle от конечных сценариев. |
| Core storage/SDK | `storage/repositories/{trading_facts_uow,order_facts,position_ledger,portfolio_snapshots}.py`; `adapters/tinvest/{portfolio,market_data,order_execution,streaming,converters}.py` | Сохранить constraints и транзакции; убрать повторную широкую классификацию SDK ошибок после определения типов SDK. |
| Worker lifecycle | `__main__.py`, `composition.py`, `services/{streaming_runtime_coordinator,analytics_runtime,broker_runtime,supervisor}.py` | Нет пакета Usecase; один файл смешивает цикл задач и конечную orchestration. Есть альтернативные пути без обнаруженного production wiring. |
| Worker decision/execution | `services/{decision_context,decision_materialization,streaming_batch_tick,streaming_cycle_transition,position_state_hydration,position_decision_evaluator,runtime_decision_planner,batch_runtime,order_dispatch,order_tracking,position_consistency}.py` | Динамические проверки типизированного результата; лишний аргумент; creation внутри services; разделение pre/post-commit ошибки существенно. |
| Worker durable state | `storage/repository.py`, `storage/fact_outbox.py`, `storage/database.py`, `domain/{storage_dtos,position_valuation}.py` | Repository объединяет несколько ответственности и импортирует бизнес-сервис; отдельные SQL/ledger gates необходимы. |
| Analytics/shared | `market_analytics/{app,service,market_source,indicators,volatility_strategy}.py`; `sentinel_contracts/{base,time,broker_execution,market_quality,trading_facts}.py` | Analytics endpoint вызывает service напрямую; DTO compatibility shim нельзя удалить без миграции callers. |
| Frontend | `api/{automations,brokers,marketData,health,portfolio}.ts`; `views/{PositionsListView,PositionDetailsView}.vue`; `components/{HealthStatus,TradingSessionStatus}.vue` | Дублирование error parsing; lifecycle timer race; типовой TS cast не доказывает валидность JSON. |

## Проверенные замечания и предложения

### C01 — Существенно: универсальные mapper скрывают контракт ошибок Core

**Место:** `src/moex_sentinel/usecases/automations.py::_automation_error` и вызывающие `execute`; `usecases/instruments.py::_catalog_error`; `usecases/market_data.py::_market_error`; `usecases/connections.py::CheckBrokerConnectionUsecase.execute`.

**Факт:** первые три принимают произвольный `Exception`, распознают тип через `isinstance` и вызываются из широкого `except Exception`. В connections `EnvironmentMismatchError` распознаётся внутри `except ValueError`. Это противоречит ADR 0005; неизвестная ошибка получает лишнюю точку обработки без восстановления.

**Изменение:** на каждой application boundary перечислить только реально ожидаемые типы и прямо преобразовать их в `UseCaseError`; subtype поставить раньше base type. Не строить новый общий mapper с `Exception`. Неизвестной ошибке оставить исходный traceback и единый transport fallback. Сохранить существующие стабильные коды/fields и безопасные сообщения.

**Проверка:** ожидаемые not-found, conflict, environment, adapter ошибки сохраняют payload; неизвестный `RuntimeError` не превращается в broker configuration; ошибки, содержащие искусственный secret, не выходят в HTTP. Отдельно сохранить частичный результат `ViewTradingAutomationDetailsUsecase`: его fallback добавляет errors для секций и не равен бессмысленному rethrow.

### C02 — Существенно: Core View выполняет вызов Service и error mapping

**Место:** `src/moex_sentinel/views/internal_market.py::snapshot`; `views/health.py::health`; `src/moex_sentinel/api/app.py::create_app`.

**Факт:** internal market endpoint напрямую вызывает `request.app.state.market_snapshot_gateway.snapshot` и преобразует `LookupError/ValueError` в HTTP 404/409. Health View получает concrete `Engine`, вызывает DB/schema checkers и сам гасит ошибки.

**Изменение:** перенести конечную операцию получения рыночного snapshot в `ViewMarketSnapshotUsecase`, типизированный service error → UseCaseError и существующий renderer; View оставить payload/вызов/response. Для health перенести две проверки в маленькую внедряемую readiness operation; она остаётся технической проверкой, а не торговым domain service. Не добавлять бизнес-валидацию здоровья в View и не оборачивать каждый DB checker отдельным Usecase.

**Проверка:** внутренний JSON-контракт и прежние статусы; неизвестный source отдельно от временной недоступности; DB down/schema incompatible/ready в минимальном application smoke. Не повторять бизнес-сценарии отдельными View unit suites.

### C03 — Существенно: системные операции Worker не имеют application boundary

**Место:** `src/trading_automaton/__main__.py::run`, `services/streaming_runtime_coordinator.py::StreamingRuntimeCoordinatorService.run_iteration`, `services/analytics_runtime.py::AnalyticsBrokerRuntime.run/run_once`, `composition.py::build_streaming_runtime/build_broker_runtime`.

**Факт:** пакет `trading_automaton/usecases` отсутствует. `run_iteration` координирует flush, claim, reconcile, lifecycle/HOLD и runtime bundles. `run_once` получает Analytics frame, подготавливает позиции даже при недоступной аналитике, выбирает команды, запускает tick и отмечает обработанный frame. Это конечные системные сценарии, а `run/close`, event wait и task ownership — lifecycle.

**Изменение:** **переместить** конечную orchestration из этих методов в Usecase; runtime передаёт ему входной snapshot/команды и остаётся владельцем периодичности, cancellation и ресурсов. На первом этапе нужны границы двух независимо запускаемых операций: `SynchronizeTradingRuntimeUsecase` для верхнего supervisor tick и `RunBrokerIterationUsecase` для broker-loop iteration. Имена предварительные, границы обязательны. Правила freshness/selection, ledger, cash, dispatch, tracking остаются Services. Не сохранять старый метод с полной orchestration за новым pass-through wrapper; не делать Usecase для каждого stage. Эти Usecase не вызывают друг друга: supervisor управляет lifecycle broker runner через порт, независимый runner вызывает свою конечную операцию. Отдельно `__main__.run` сейчас выполняет `repository.begin_run` и `RecoveryService(repository).recover`: эту конечную startup orchestration перенести в `RecoverWorkerRunUsecase`, а fail-safe recovery rules оставить Service. Это третий независимый акторный вход, не wrapper для внутреннего stage. Shutdown marker и закрытие ресурсов остаются lifecycle через узкий порт.

**Проверка:** recovery идёт без Analytics; HOLD с активным intent контролируется без новых решений; один frame не обрабатывается повторно; pending outbox блокирует нужную последовательность; остановка ожидает tracking/закрывает SDK. Вынести и описать ownership state `_last_seen`, `_commands` и `_run_lock`, чтобы перемещение не создало вторую копию состояния.

### C04 — Существенно: portfolio collector соединяет system scenario и concrete storage

**Место:** `src/moex_sentinel/portfolio_snapshot_worker.py::run_forever/run`; `services/portfolio_snapshot_collection.py::PortfolioSnapshotCollector.collect_once`.

**Факт:** worker вызывает `CollectorPort.collect_once`; concrete collector получает `Engine/sessionmaker`, берёт `portfolio_snapshot_run_lock` и создаёт `PortfolioSnapshotRepository` внутри метода. Сценарий читает несколько счетов и сохраняет согласованный run; размер метода сам по себе не ошибка, но границы явно смешаны.

**Изменение:** перенести orchestration одного run в `CollectPortfolioSnapshotsUsecase`; reusable money/cash-flow/validation оставить в существующих domain/services. Внедрять один узкий UoW/run-lock порт, созданный composition, вместо concrete Engine/Session в бизнес-слое. Не превращать `pg_advisory_lock` и repository в Usecase. Сохранить блокировку bucket, повторный run, частичные account errors и единую транзакцию финальной записи.

**Проверка:** совпадающий bucket не записывается дважды; backward clock; отказ одного счёта сохраняет договорённый partial result; ошибка commit не оставляет частичный run; HTTP summary не начинает вызывать брокера. PostgreSQL lock/rollback проверять на отдельной БД.

### C05 — Средне: Analytics transport/service и создание calculator

**Место:** `src/market_analytics/app.py::create_app.snapshot`, `service.py::AnalyticsService.__init__/snapshot`, `market_source.py::HttpMarketSource`.

**Факт:** endpoint вызывает `AnalyticsService.snapshot` через app state и ловит `httpx.HTTPError/ValueError/ArithmeticError`; service создаёт `MarketIndicatorsService()` сам. `HttpMarketSource` корректно является HTTP adapter.

**Изменение:** конечную операцию получения и расчёта одного batch оформить `CalculateAnalyticsSnapshotUsecase`, перенеся orchestration; fresh/structure/indicator rules оставить services. Создание source/calculator вынести в composition `create_app`. Конкретный transport exception переводить в объявленную ошибку source adapter; не превращать `HttpMarketSource` в Usecase.

**Проверка:** недоступный Core, неверный набор инструментов, stale/future frame, пустая/неполная история, fallback profile; response shape и 503 сохраняются. Не добавлять один Usecase на каждый индикатор.

### C06 — Существенно: Worker Repository зависит от Service; транзакцию нельзя распилить

**Место:** `src/trading_automaton/storage/repository.py::LocalAutomationRepository._finalize_trading_cycle`, `finalize_execution`, `_append_execution_graph`, `save_decision_batch`; `domain/position_valuation.py`.

**Факт:** `_finalize_trading_cycle` локально импортирует и создаёт `TradingCycleService`, вызывая `mark_buy/mark_sell`. Repository также владеет intent, bootstrap, LIFO allocations, cycle state и outbox. Это подтверждённая обратная зависимость `storage → services`.

**Изменение:** выделить чистое правило перехода cycle в существующий domain-модуль, используемый service и session-bound persistence, либо передавать вычисленный переход через узкий порт при той же транзакционной защите. Конкретный вариант выбрать по тестируемому ownership state. DTO imports callers направить на уже существующий `domain/storage_dtos.py`, а не через реэкспорт `storage.repository`. Далее разбирать repository по реально связанным операциям небольшими session-bound помощниками; не давать каждому помощнику собственный commit.

**Проверка:** intent+execution+lot+allocation+cycle+outbox либо записаны вместе, либо отсутствуют; delayed BUY/SELL, commission ровно один раз, ms precision и replay из recovery остаются 1:1. Не переносить запись фактов в Usecase отдельными независимыми repository transactions.

### C07 — Средне: доказанная лишняя динамика и аргументы в typed Worker path

**Место:** `services/position_state_hydration.py::PositionStateHydrationService._hydrate_one`, `PositionConsistencyPort.reconcile`; `domain/dtos.py::PositionConsistencyResult`; `services/streaming_batch_tick.py::StreamingBatchTickService.__init__`; `services/batch_runtime.py::_validate_mapping`.

**Факт:** `reconcile` объявляет `PositionConsistencyResult`, production `PositionConsistencyService` возвращает именно его. Код читает `consistent/reason_code/lots` через `getattr(..., False/...)`; наличие всех полей уже гарантировано моделью. В `StreamingBatchTickService` аргумент `commissions: object` принят, но нигде не используется; composition передаёт его. `_validate_mapping` повторяет `item.intent is not None` после фильтра `intent_items`.

**Изменение:** обращаться к полям consistency напрямую; сузить `lots: tuple[object, ...]` до фактического DTO только после проверки всех producers. Nullable `reason_code` остаётся nullable; решение, как отображать `None`, не менять скрытно вместе с рефакторингом. Удалить неиспользуемый `commissions` из tick ctor и callers; комиссии продолжает получать decision/preparation service. Вместо второго фильтра собрать существующие intents одним проходом; биективность requests/intents сохранить.

**Проверка:** замена объектов в tests на declared result DTO без изменения assertions; реальные inconsistent/missing position по-прежнему блокируют decision. Все callers ctor обновлены; commission estimate pipeline и cash mapping не меняются. Это не разрешение удалить другие `None` guards.

### C08 — Средне: повторяющийся HTTP error parser frontend

**Место:** `frontend/src/api/brokers.ts::ensureSuccess`, `api/marketData.ts::ensureSuccess`, `api/automations.ts::request`.

**Факт:** первые две функции разбирают одинаковый `detail.code/message/fields`, бросают свой Error внутри `try` и затем распознают его в `catch`; automation parser использует другое поведение/fallback. Сокращение возможно без изменения запросов бизнес-операций.

**Изменение:** один небольшой transport helper для чтения safe error envelope; отделить JSON parsing от создания/броска API error. Сохранить domain-specific error code/fallback в вызывающих API функциях либо в явных параметрах. Не вводить service locator, API-классы на каждый endpoint и универсальную сеть классов.

**Проверка:** JSON/non-JSON ошибки, fields, 204, abort и network rejection; действующие сообщения брокера/рынка/автоматов. Не заменять смысл ошибок единым «что-то пошло не так».

### C09 — Средне, подтверждённый сценарий дефекта: timer создаётся после unmount

**Место:** `frontend/src/components/TradingSessionStatus.vue::onMounted/onUnmounted`, `views/PositionDetailsView.vue::onMounted/onBeforeUnmount`.

**Факт:** `onMounted` сначала `await refresh()`, затем присваивает interval. При unmount во время первого fetch cleanup видит `undefined`; завершившийся позже fetch создаёт interval уже после cleanup. Это следует из последовательности кода; runtime воспроизведение в этом аудите не выполнялось.

**Изменение:** отдельным bugfix добавить cancellation/disposed state или создать timer до await с гарантированным cleanup. После сохранения различий страниц можно выделить маленький composable lifecycle polling для этих двух потребителей. `HealthStatus.vue` уже использует AbortController; не унифицировать его одноразовую проверку с periodic polling насильно.

**Проверка:** deferred initial request → unmount → resolve не создаёт timer/повторных запросов; повторный refresh не пересекается там, где это запрещено; ошибки отображаются как раньше. Не удалять guards `loading`, `refreshing`, `actionPending` — это защита от конкурентных действий.

### C10 — Существенно: вложенные Usecase и business validation в application layer

**Место:** `src/moex_sentinel/usecases/instruments.py::SynchronizeBrokerInstrumentsUsecase.execute`, `ViewBrokerInstrumentsUsecase.execute`; `usecases/position_adoption.py::AdoptBrokerPositionsUsecase`; `composition.py`.

**Факт:** synchronize принимает `PositionAdoptionUsecasePort` и вызывает `.execute`; composition передаёт `AdoptBrokerPositionsUsecase`. Последний получает `Callable[[str], object]`, динамически читает ACTIVE/account, создаёт adapter через factory. Category business validation размещена во ViewBrokerInstrumentsUsecase.

**Изменение:** единый внешний сценарий synchronize должен вызывать catalog/adoption Services через declared DTO/ports; shared adoption step не является вложенным Usecase. Перенести ACTIVE/account/category правила в соответствующий Service; использовать `UserBroker`/узкий declared broker result вместо `object`. Если adoption имеет самостоятельного актора, сохранить его отдельный Usecase поверх того же Service, без вызова из synchronize. Не объявлять две независимые транзакции catalog/adoption атомарными без общего UoW.

**Проверка:** synchronize с и без adoption, inactive/missing account, invalid category и partial failures; существующий bootstrap ownership/четыре факта не меняются. Контракт `object` сначала уточняется — текущие `getattr` нельзя просто удалить до этого.

### C11 — Средне, кандидат после проверки поддержки: альтернативные decision/runtime paths

**Место:** `services/runtime_decision_planner.py::TradingDecisionPlanner`, `position_decision_evaluator.py::PositionDecisionEvaluator`, `broker_runtime.py::BrokerRuntimeService`, `supervisor.py::TradingSupervisorService`, `market_indicators.py`, `volatility_strategy.py`; current `composition.py`.

**Факт:** поиск production-символов не обнаружил callers supervisor и BrokerRuntimeService в `src`; planner связан с evaluator, но evaluator не подключён текущей composition. Tests используют эти классы; ADR 0007 прямо называет planner сохранённым контрактом. Compatibility imports Worker→Analytics продолжают существовать.

**Карта reachability текущего checkout:**

| Путь | Фактическое wiring | Решение до рефакторинга |
|---|---|---|
| `StreamingRuntimeCoordinatorService.run_iteration` | `__main__.run` → `composition.build_streaming_runtime` → constructor | Активный supervisor path, C03. |
| `AnalyticsBrokerRuntime.run/run_once` | `composition.build_broker_runtime` → constructor → bundle runtime task | Активный broker path, C03. |
| `TradingSupervisorService.run_iteration` | В `src` обнаружено только определение; тесты вызывают непосредственно | Сначала проверить поддержку/удаление, не переносить в Usecase параллельно активному supervisor. |
| `BrokerRuntimeService` | В `src` обнаружено только определение; используются tests старого streaming path | Сначала проверить поддержку/удаление, не добавлять второй broker Usecase. |
| `PositionDecisionEvaluator` → `TradingDecisionPlanner` | Связь между ними есть, подключения evaluator в действующей composition нет; ADR 0007 сохраняет planner contract | Сначала решение по ADR/consumers, затем перенос уникальных regressions. |

**Изменение:** составить таблицу «текущий runtime / сохраняемый контракт / только historical tests». **Удаление пока не разрешено доказательствами.** После проверки entrypoints, внешних import consumers и нормативного статуса архивировать только неподдерживаемую реализацию, перенеся её уникальные strategy regressions на активного владельца поведения. Не поддерживать две вычислительные pipelines только ради старой fixture, но и не терять предусмотренный ADR контракт.

**Проверка:** подтверждение reachable path из production composition; те же market/commission/decision cases на действующем сервисе; mapping каждого старого теста на новый. Отсутствие `rg` callers не доказывает отсутствие dynamic/external consumer.

### C12 — Средне: DI defaults создают скрытые конфигурации сервисов

**Место:** `StreamingCycleTransitionService.__init__`, `StreamingBatchTickService.__init__`, `StreamingRuntimeCoordinatorService.__init__`, `StreamingPositionDecisionService.__init__`, `DecisionContextService.__init__`, `AnalyticsBrokerRuntime._fresh_instruments` и `AnalyticsService.__init__`.

**Факт:** используются `cycles or TradingCycleService()`, `materializer or DecisionMaterializerService(...)`, `WorkerAutomationLifecycleService(repository)`, `settings or StrategySettings()`; validator создаётся в `_fresh_instruments` при вызове. Composition уже существует и может владеть этим wiring.

**Изменение:** зависимости, являющиеся Services/configuration, создавать в composition и передавать явно. Не заводить Protocol на каждый чистый helper: конкретный stateless service допустим, если он не раскрывает transport/storage. Optional capability оставлять optional лишь при реально поддержанном режиме (например, отдельный analytics metrics source); режимы выразить явными constructors/factories в composition, не цепочками fallback inside service.

**Проверка:** production и fake wiring используют одинаковые настройки; можно внедрить validator/clock и наблюдать его поведение без monkeypatch внутренних атрибутов. Простая переименовка `Service`/`Usecase` без переноса ответственности не считается выполнением.

### C13 — Средне: повторная SDK-классификация ошибок смешана с conversions

**Место:** `src/moex_sentinel/adapters/tinvest/portfolio.py::_map_error` и SDK methods; `market_data.py::_map_error` и SDK methods; `src/trading_automaton/services/order_dispatch.py::_error_code/_error_details`.

**Факт:** portfolio и market adapters повторяют gRPC status mapping и оборачивают весь метод, включая преобразование DTO, широким `except Exception`. Программная ошибка конвертера может стать `BROKER_UNAVAILABLE`. Dispatch service знает SDK-style callable `.code/.details`.

**Изменение:** определить фактические типы ошибок закреплённого SDK и перехватывать их вокруг SDK calls; один маленький adapter-level translator принимает известный SDK/status DTO, не произвольный `Exception`. Ошибку malformed broker response преобразовать отдельно и безопасно. Service получает typed broker failure/code через порт; сырой SDK текст не становится business audit контрактом.

**Проверка:** реальные adapter contract cases auth/rate-limit/timeout, malformed response и искусственная converter bug различимы; token/metadata не попадают в сообщения. `getattr` optional protobuf fields и graceful close нельзя удалять по общему правилу — сначала schema/SDK evidence.

### C14 — Средне: длинные mapper/materializer проще разделить функциями, а не новыми слоями

**Место:** `src/moex_sentinel/services/trading_fact_mapping.py::TradingFactMapper.apply`; `src/trading_automaton/services/decision_materialization.py::DecisionMaterializerService.materialize`.

**Факт:** explicit `match` mapper занимает 227 строк и объединяет разные fact handlers; materialize — 203 строки, cash decision/request, position valuation, durable intent и audit. Materialize строит `_build_request` почти одинаково в двух ветках, затем отдельно проверяет обязательные поля intent.

**Изменение:** mapper — сохранить явный dispatch по discriminated union, вынести крупные обработчики в private typed methods. Materializer — собрать request и связанный IntentBatchItem рядом после выбора cash outcome; использовать уже обязательные поля request, уменьшив параллельные nullable local variables. Не удалить защитный invariant до изменения представления пары request/intent. Выделять функции по понятным результатам, не класс на каждый fact и не dynamic registry.

**Проверка:** sequence/status/lineage и transactional replay mapper; WAIT при insufficient cash не имеет request/intent; SELL и BUY сохраняют exact mapping, комиссии и reservation. Поля audit и timestamps (raw quote отдельно от ms fact) неизменны.

### C15 — Низко/средне: docstrings должны объяснять ограничения, а не пересказывать имя

**Место:** `OrderTrackingService`, `BatchTradingRuntimeService`, `AnalyticsBrokerRuntime`, `LocalAutomationRepository`, `TradingFactsUnitOfWork`, shared execution/fact DTO.

**Факт:** многие классы не имеют class docstring; module docstring не объясняет caller контракт. Снимок статического inventory: Core 326 классов/89 class docstrings; Worker 203/20; Analytics 5/0; shared contracts 82/4. В счёт входят DTO/Protocols, поэтому процент не является метрикой качества. `TradingFactsUnitOfWork` говорит «complete fact batch», а ingress фактически коммитит отдельную automation group.

**Изменение:** писать docstring для публичной границы и сложного инварианта: кто владеет транзакцией, до/после commit, разрешённые состояния и отсутствие данных, idempotency, UTC/ms против raw quote/executed time, stop/cancellation. Коротким DTO достаточно единиц измерения и nullable semantics, trivial accessor не требует текста ради нормы. Уточнить batch/group wording. Обновлять docs вместе с переносом, не отдельной массовой генерацией.

**Проверка:** reviewer по docstring может назвать побочный эффект, владельца ресурса и поведение при повторе; примеры соответствуют signatures/существующим regressions. AST/docstring coverage gate не вводить.

### C16 — Средне, отдельное решение: PositionalModel compatibility

**Место:** `src/sentinel_contracts/base.py::PositionalModel/_PositionalModelMetaclass`; Pydantic DTO callers во всех пакетах.

**Факт:** общий базовый класс использует private Pydantic metaclass и positional field order; `kwargs.setdefault` отдаёт приоритет keyword при переданном positional значении. Многие вызовы в production позиционные. Это существенная совместимость, а не бесполезный слой.

**Изменение:** сначала определить поддерживаемый constructor contract и inventory callers; постепенно перевести сложные/длинные DTO constructors на keyword arguments. Только затем отдельно решить, нужен ли private metaclass/legacy positional support. Не смешивать его удаление с worker/storage refactor и не менять JSON поля. Чистые `domain/trading_summary.py` и `domain/position_valuation.py` остаются domain-функциями; им не нужны Usecase wrappers.

**Проверка:** constructor/schema/serialization и positional compatibility; money/quantity/time units не перепутаны; внутренний `model_copy(update=...)` не считать runtime validation. Уменьшение количества DTO не цель.

## Guards, которые сохраняются

| Место/проверка | Почему не избыточна |
|---|---|
| `OrderBookValidationService.validate`: empty bids/asks, positive/finite price, ordering, crossed/stale/future | `OrderBookSnapshot` допускает пустые tuple, `best_bid/best_ask` индексируют `[0]`; Pydantic type Decimal не гарантирует market validity. |
| Analytics `_freshness` и Worker повторная freshness check перед tick/dispatch | Между сервисами и подготовкой проходит время; это разные trust/time boundaries. Нельзя объединить проверку так, чтобы «омолодить» quote. |
| `ActiveIntentGateService.clear`: текущий intent ID должен совпасть | Поздний callback старой заявки не должен снять gate новой. |
| Repository scope/revision/unique, intent replay, cash bijection, outbox sequence | Независимая durable защита от гонки, crash/retry и невалидного сообщения. Service-проверка не заменяет SQL constraint. |
| `BatchTradingRuntimeService` различает `DurableDecisionPersistenceError` и `PostCommitBatchError` | После commit нельзя повторить scenario как будто intent не был сохранён. Широкий handler здесь не удаляется механически: сначала сохраняется semantic error contract. |
| `BrokerRuntimeBundle.close` с обработкой BaseException и закрытием session | Cancellation — часть resource ownership; простое удаление вложенного try может оставить SDK session открытой или потерять primary error. |
| `PortfolioSnapshotRun` backward clock/bucket guard и nullable baseline | Это измеренное покрытие/идемпотентность, не лишняя проверка. |
| Frontend `averagePrice` проверяет string/finite; chart проверяет данные | `as AutomationDetails` не валидирует JSON во время исполнения. Удалить guards можно лишь после явного runtime decoder contract, если его цена оправдана. |

## Этапы реализации после рассмотрения плана

| Этап | Изменения | Граница проверки/выход |
|---|---|---|
| 0. Карта контрактов | Устранить противоречие `docs/architecture.md §2.2` (Trader runtime→service) с accepted ADR 0005; подтвердить исторический статус ADR 0007 и реальную карту акторов; определить кандидатов C11 на удаление до переноса active boundaries. | Один согласованный call flow; таблица существующий сценарий→владелец→tests; независимое review до code changes. |
| 1. Малые локальные сокращения | C07, typed imports из C06; C08; документация затронутых методов C15. | Нет изменения поведения/формата; target functional tests до/после; обновлены только imports/ctor/fixtures там, где нужно. |
| 2. Явные ошибки | C01 и C13 по одному adapter/application пути; определить safe typed failures. | Старые error codes/fields сохранены; unexpected failure не маскируется; сохранена post-commit safety. |
| 3. Core application boundaries | C02, C10, C04 по отдельным сценариям. | View→Usecase→Service; нет Usecase→Usecase; UoW/lock и partial-results поведение сохранены. |
| 4. Worker/Analytics application boundaries | C03, C05 и связанные C12. Переместить orchestration, затем удалить старую; сначала один independently scheduled actor. | Нет nested Usecases, скрытых duplicate states или лишних wrappers; shutdown/recovery/one-frame behavior сохранены. |
| 5. Большие методы и storage direction | C06 и C14 отдельными малыми patch; разделить pure rules и session-bound operations. | Общий commit ledger+intent+outbox; PostgreSQL constraints и Worker SQLite recovery suite; никакой рабочей БД. |
| 6. Отдельный frontend bugfix | C09: сначала regression, затем lifecycle исправление. | Разобранный unmount race закрыт; это явное изменение поведения, не скрытая часть косметического refactor. |
| 7. Совместимость/legacy | C11 и C16 только после подтверждения consumers и карты сохраняемых regressions. | Нет удаления supported contract; уникальные сценарии сохраняются, а не заменяются ослабленными assertions. |

Производительность outbox/LIFO рассматривается по уже имеющемуся [плану](../deployment/refactoring-performance.md), после baseline. Этот аудит не разрешает изменение batch ordering, SQLite ownership или таймаутов ради уменьшения строк.

## Приёмка всей программы

- Для каждого patch есть таблица «что удалено/перемещено → почему избыточно → каким контрактом гарантировано → какой сценарий сохраняется».
- В чистом refactor сохраняются assertions, входы и ожидаемые side effects; изменение контракта, например текста `reason_code=None`, выделяется отдельно.
- Functional тест проверяет владельца поведения; PostgreSQL/HTTP integration остаются там, где проверяют настоящую транзакцию/сериализацию, а не повторяют unit assertion.
- Проверяется call flow, SOLID, понятность имён и ресурсный lifecycle независимым reviewer. Размер файла, число классов или уменьшение collected tests не являются самостоятельной приёмкой.
- Перед merge/вехой target tests + необходимые integration checks и cross-review. Для этой подготовительной вехи выполнено только чтение исходников; поведенческие тесты и production benchmarks не запускались и не объявляются пройденными.
