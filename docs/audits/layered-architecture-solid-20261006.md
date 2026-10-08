# Аудит слоистой архитектуры и SOLID backend MOEX Sentinel

Архитектура соответствует заявленным границам частично. Core, Worker и Analytics имеют отдельные роли, основные операции используют порты, а исполнение и outbox сохраняются атомарно. Остаются три существенных нарушения контрактов: неверная обработка отсутствующего broker в Core, отсутствие защиты от отзыва команды во время Worker tick и утечка SDK session при неуспешной сборке broker runtime. Поэтому общий вердикт архитектуры — **частичное соответствие**, без architecture PASS.

Дата проверки: 06.10.2026. Код: `8d4805e47a39843c4f74619e75918d31ed459915`, ветка `master`; исходное рабочее дерево чистое. Scope: `src/moex_sentinel`, `src/trading_automaton`, `src/market_analytics`, `src/sentinel_contracts` и связанные тесты. Frontend, перенос storage, исправление поведения и проверка работающих сервисов не входят. Runtime неизвестен.

Границы и инварианты заданы [AGENT_BRIEF.md](../../AGENT_BRIEF.md), очередь — [планом реализации](../phase-1-implementation-plan.md). Аудит оценивает текущий код; исторические приёмки не подтверждают сегодняшнее состояние сервисов. Important означает конкретный значимый сценарий в действующей реализации, Minor — ограничение контрактов, тестируемости или расширяемости без доказанного текущего инцидента. Critical не выявлены.

## Реальные слои и зависимости

| Контур | Вход и orchestration | Бизнес правила и порты | Адаптеры и durable state |
| --- | --- | --- | --- |
| Core HTTP | `api/app.py` собирает views → usecases → services | DTO и lifecycle policy; локальные `Protocol`; целая синхронная операция через `run_sync` | SQLAlchemy repositories/UoW, PostgreSQL; broker adapters; application-owned executor |
| Core facts | internal views → `PublishTradingFactsUsecase` → `TradingFactIngressService` | Проверка envelope, scope, sequence/revision и mapper; UoW одной группы automation | `TradingFactsUnitOfWork` и repositories сохраняют подтверждённые факты одной транзакцией |
| Core market | `MarketSnapshotGateway` управляет source runtime | Market source port, market-only snapshot; refresh/subscriptions и recovery | TInvest stream/history adapter; рынок принадлежит Core |
| Portfolio collector | entry/composition → `CollectPortfolioSnapshotsUsecase` → collection service/store ports | Координационный run lock, broker reads и единственное atomic save | PostgreSQL snapshot repository; lock не означает удержание DB Session через broker awaits |
| Worker control | entry/composition → `StreamingRuntimeCoordinator` → `SynchronizeTradingRuntimeUsecase` | Flush, claim/cache, lifecycle, сверка Core state, группировка команд; ports | Core HTTP adapter и локальный SQLite repository |
| Worker execution | `AnalyticsBrokerRuntime` → `RunBrokerIterationUsecase` → preparation/hydration → tick | Scheduler, decision rules/strategy, materializer, batch, dispatch/tracking; ports и immutable DTO | Analytics HTTP, broker SDK, SQLite intents/lots/cycle и typed outbox |
| Analytics | HTTP route → `CalculateAnalyticsSnapshotUsecase` → `AnalyticsService.calculate` | `MarketSourcePort` с нейтральной ошибкой; чистые indicators/thresholds | HTTP source читает market-only batch Core; собственного storage и broker credentials нет |
| Shared contracts | DTO, enums, time/market/lifecycle policy | Общие wire/domain contracts | Прямых импортов приложений в `sentinel_contracts` нет |

Core HTTP создаёт, использует и закрывает Session внутри одной операции рабочего потока, наружу возвращает DTO. Отмена ожидающего HTTP-запроса не освобождает место пула до окончания операции. Это сохраняет ownership ресурсов и ограничение конкуренции; async SDK, cache и locks остаются в event loop.

Worker синхронизирует команды в порядке flush → claim/cache → lifecycle → flush → Core state → публикация broker groups. Broker iteration получает Analytics frame, готовит восстановление исполнения и portfolio даже при outage, повторно проверяет TTL и текущую команду, затем запускает tick. Metrics и рынок приходят из одного frame; `_last_seen` хранит `(snapshot_id, profile_id)`.

После materialization Worker сохраняет batch до broker submission. Terminal execution обновляет intent, ledger, trading cycle и outbox одной транзакцией. `FactOutboxWriter` использует Session вызывающей операции; ACK очищает очередь доставки, сохраняя recovery state. Startup recovery и broker reconciliation опираются на durable SQLite, которую нельзя считать простым cache.

Зависимости на конкретные adapters, engine и clients в composition root ожидаемы. Нарушения ниже относятся к policy/usecase/service contracts или неполной передаче ownership ресурсов. В дереве Worker есть 12 прямых импортов Core/Analytics: это не 12 нарушений runtime, поскольку часть расположена в adapters или в неиспользуемых текущей composition путях.

## Существенные замечания

### I1 Core теряет объявленный контракт отсутствующего broker

**Important · LSP и контракт ошибок.** [UserBrokerRepository.get](../../src/moex_sentinel/storage/repositories/user_brokers.py#L49) вызывает `_get_model`, который на строке 130 выбрасывает `UserBrokerNotFoundError`. Он и `BrokerRecordNotFoundError` — независимые классы `LookupError` в `domain/user_brokers.py:23` и `domain/brokers.py:10`.

Действующая [composition](../../src/moex_sentinel/composition.py#L197) передаёт этот repository в connection, portfolio, market, catalog и automation services. Однако `usecases/connections.py:28`, `market_data.py:58/78/111`, `instruments.py:65/119/150/181` ловят старый `BrokerRecordNotFoundError`; `usecases/portfolio.py:28` вообще не переводит missing broker. При создании automation в TRADE `services/automations.py:116–120` обращается к broker до repository automation, а `usecases/automations.py:80` ловит другой, storage `RecordNotFoundError`.

Запрос с отсутствующим broker UUID на check/accounts/market/catalog и TRADE create выходит из usecase с необработанным исключением вместо стабильного `BROKER_NOT_FOUND`. `api/app.py:315–316` регистрирует только handlers `UseCaseError` и request validation; `views/errors.py` переводит `BROKER_NOT_FOUND` в 404. Actual lookup exception не попадает в этот handler и приводит к HTTP 500. Сеть брокера для этого сценария не нужна.

**Минимальное исправление:** унифицировать отсутствующий broker на границе repository/service и использовать этот контракт во всех потребителях; для create отделить ошибку broker от отсутствующего automation. Не добавлять catch всех `LookupError` или всех исключений.

**Тестовый DoD:** actual composition + изолированная БД + отсутствующий broker UUID для перечисленных routes; проверить согласованные HTTP status/code и отсутствие входа в broker adapter. Существующие usecase tests проверяют успех и adapter errors, но не реальный `UserBrokerRepository` missing path.

### I2 Отзыв команды во время tick не блокирует новый intent

**Important · контракт lifecycle и ownership команд.** [RunBrokerIterationUsecase](../../src/trading_automaton/usecases/broker_iteration.py#L77) проверяет актуальность команды до `await tick.run_tick` на строке 90. Затем [tick](../../src/trading_automaton/services/streaming_batch_tick.py#L125) содержит awaits загрузки states, decider и materializer. [save_decision_batch](../../src/trading_automaton/storage/repository.py#L1778) проверяет active intent и ожидаемый cycle, но не `cached.state` или актуальность команды; [dispatch](../../src/trading_automaton/services/order_dispatch.py#L60) проверяет access/account/TTL, но не отзыв команды.

Конкретный сценарий: у broker есть две активные automation; первая уже вошла в tick и ожидает dependency. Coordinator применяет для неё Core `HOLD` через `synchronize_core_state` и публикует замену команд. Вторая automation сохраняет bundle работающим. Продолжившийся tick первой использует старую `IN_WORK` команду и может создать и отправить новый intent после уже сохранённого Worker `HOLD`. Это отличается от обычной задержки до очередного опроса Core.

**Минимальное исправление:** передавать проверяемую актуальность команды до materialization/persistence и атомарно запрещать новый intent, если Worker уже применил отзыв. Сохранять supervision ранее committed/sent intent. Проверка должна учитывать изменения локальной revision из outbox и не приравнивать их автоматически к новой команде Core. Контракт мгновенного отзыва до очередного опроса Core этим сценарием не установлен.

**Тестовый DoD:** barrier внутри tick, отзыв первой automation при действующей второй, затем продолжение; ни нового intent, ни SDK submission. Проверить сохранение подтверждения и recovery ранее committed/sent intent. `test_analytics_runtime.py:220` покрывает замену во время preparation; cycle CAS tests защищают другой инвариант.

### I3 Неуспешная сборка broker runtime оставляет SDK session открытой

**Important · ownership ресурсов и exception safety.** В [build_broker_runtime](../../src/trading_automaton/composition.py#L175) `session.start()` открывает SDK до restore cash reservations на строке 181 и дальнейшей сборки. Cleanup на исключении или cancellation до возврата bundle на строке 290 отсутствует.

Если чтение SQLite reservations, restore или последующий шаг сборки падает, coordinator не получает bundle и не владеет session. Он не может закрыть ресурс; повторные попытки сборки открывают новые SDK sessions/channels. Cleanup готового `BrokerRuntimeBundle.close` эту стадию не покрывает.

**Минимальное исправление:** освобождать созданные ресурсы при ошибке/cancellation до успешной передачи ownership в bundle, сохраняя первичную ошибку. Подходят `AsyncExitStack` или локальный `try … except BaseException` с явной передачей ownership.

**Тестовый DoD:** fake session start/close counters; failure в reservations read/restore и cancellation после start; закрытие ровно один раз, сохранение первичного исключения. Имеющиеся composition tests проверяют готовый bundle и cleanup при tracking failure, но не rollback сборки.

## Архитектурный долг

### M1 Usecases и lot service зависят от concrete storage

**Minor · DIP и ISP.** [Core fact usecases](../../src/moex_sentinel/usecases/trading_fact_ingress.py#L6) принимают `AutomationCommandRepository`; `ClaimAutomationCommandsUsecase` и `ViewAutomationStatusesUsecase` нуждаются только в claim/statuses. [Worker LotLedgerService](../../src/trading_automaton/services/lot_ledger.py#L9) импортирует и требует `LocalAutomationRepository`, хотя использует лишь lot read/create/allocation. [Core automation usecases](../../src/moex_sentinel/usecases/automations.py#L19) зависят от storage exceptions.

При смене storage или использовании типизированного fake меняются внутренние application modules: policy import загружает ORM implementation, а ошибки заменяемого repository не соответствуют catch. Это дополнительная связанность и затруднение тестирования, без доказанного отказа текущего storage.

**Исправление и DoD:** узкие consumer-owned protocols и нейтральные lookup/conflict errors; unit tests policy/error mapping на fake, существующие DB/integration tests сохранить. Не дробить atomic execution/outbox transaction на независимые repositories ради формального SRP.

### M2 Общий broker port описывает прежнюю модель и лишние операции

**Minor · LSP и ISP.** [BrokerRepositoryPort](../../src/moex_sentinel/services/ports.py#L8) объявляет `Broker/BrokerDraft`, `replace/delete/set_test_account_state`. Actual [UserBrokerRepository](../../src/moex_sentinel/storage/repositories/user_brokers.py#L44) возвращает `UserBroker`, принимает `UserBrokerDraft`, поддерживает archive и не реализует прежний набор методов. Похожая старая annotation остаётся в `services/automaton_brokers.py:11`.

Текущие read consumers работают на пересекающихся свойствах DTO, но declared substitution неверна; новый consumer может вызвать формально разрешённый отсутствующий метод. Общий write API также навязывается сервисам, которым нужны только get/list.

**Исправление и DoD:** привести signatures к актуальным UserBroker contracts, отделить только реально используемые read capabilities от configuration writes; проверить typed wiring и archive/history scenarios. Это не требует нового общего repository framework.

### M3 Внутренние слои знают transport и vendor semantics

**Minor · DIP и OCP.** Core `services/connections.py:6/33` импортирует и самостоятельно создаёт `TInvestAdapterError`; то же семейство ошибок используется в `services/portfolio.py:9`, `portfolio_snapshot_collection.py:10` и connection/market/catalog usecases. [Market gateway](../../src/moex_sentinel/services/market_snapshot_gateway.py#L40) классифицирует retry/permanent failures по gRPC statuses, которых нет в объявленном source contract.

Worker `services/order_dispatch.py:9/120`, `uncertain_intent_reconciliation.py:10/116`, `runtime/analytics_broker.py:9/60` зависят от ошибки Core TInvest adapter. `services/analytics_frame.py:8/70` и `fact_synchronization.py:8/120–134` классифицируют конкретные `httpx` exceptions. [Recovery operations](../../src/trading_automaton/services/uncertain_intent_reconciliation.py#L235) читают `OPERATION_TYPE_*`, `OPERATION_STATE_EXECUTED`, строковые timestamps и `quantity_done` из `dict[str, object]`.

Подстановка adapter, удовлетворяющего method signatures, не гарантирует прежний retry/error/recovery contract: потребуется менять policy, а другая схема operations может молча оставить исполнение unresolved. Текущий runtime incident из этой связанности не следует; один поддерживаемый broker снижает срочность, но не устраняет DIP debt.

**Исправление и DoD:** neutral safe error с code/retryability и typed recovery-operation DTO с явными единицами; adapter переводит transport/vendor данные. Contract tests одинакового failure/recovery behavior для fake и actual adapter, включая permanent/transient/cancellation и отсутствие повторного submission.

### M4 Порты решения и batch не фиксируют реальные предусловия и результаты

**Minor · LSP и явные постусловия.** [PositionDecisionPort](../../src/trading_automaton/services/streaming_position_decision.py#L34) обещает `decide(context: object)`, но injected `TradeDecisionService.decide` в `services/decision.py:141` принимает только `DecisionContext`. Реализация сужает объявленный input; нынешний caller на строке 95 передаёт правильный DTO.

[BatchRuntimePort](../../src/trading_automaton/services/streaming_batch_tick.py#L43) возвращает `object`; `domain/dtos.py:243` описывает `BatchTickResult.persisted` тем же типом, хотя существует `BatchPersistResult`. Consumer на строках 208/212/280 через `getattr` заменяет отсутствие полей пустыми defaults и публикует hot state. Формально допустимый результат без committed intents может скрыть нарушение постусловий; actual implementation возвращает нужные данные.

**Исправление и DoD:** input `DecisionContext`, result `BatchTickResult` с `persisted: BatchPersistResult`, прямой доступ к полям; типизированная проверка wiring. Сохранить behavioral tests rollback/postcommit/CAS и заменить permissive doubles корректными DTO. Не заявлять текущий hot-state drift только по широкой annotation.

### M5 Worker сохраняет зависимость от implementation Analytics и два режима hydration

**Minor · SRP и границы сервисов.** `services/market_indicators.py:3` и `volatility_strategy.py:7` импортируют calculators из другого приложения. [PositionStateHydrationService](../../src/trading_automaton/services/position_state_hydration.py#L111) создаёт local calculator даже при `prepared_metrics`; действующая composition передаёт Analytics metrics, и ветка строк 226–229 не вычисляет показатели локально.

В дереве также остались `market_data_bootstrap`, `runtime_decision_planner`, `broker_runtime` и streaming adapter, не подключённые действующим broker builder. Часть использует Core market DTO и direct history capabilities. Это связывает packaging Worker с Analytics implementation и оставляет несколько способов получить показатели, усложняя contract review.

**Исправление и DoD:** убрать создание ненужного calculator в prepared mode; обосновать и локализовать оставшиеся calculator consumers либо удалить недостижимые пути после проверки callers. Worker production import должен требовать shared contracts, а не calculators Analytics; сохранить test отсутствия direct stream/history calls. Активную загрузку свечей Worker этим замечанием не утверждаем.

### M6 Проверки архитектуры покрывают лишь часть направлений импортов

**Minor · достаточность проверки границ.** [test_architecture.py](../../tests/test_architecture.py#L1) проверяет запрещённые идентификаторы. `tests/integration/test_worker_storage_isolation.py` ограничивает imports только трёх fact-path файлов. [Analytics dependency test](../../tests/market_analytics/test_dependencies.py#L1) проверяет важный isolated import/execution path, но не все модули.

Новый запрещённый import вне этих путей может пройти архитектурные проверки. Это пробел предотвращения регрессий, а не доказательство действующего runtime отказа.

**Исправление и DoD:** AST/import graph gate для domain/shared/application boundaries с явными допустимыми composition/adapter imports; negative fixture доказывает обнаружение нарушения, существующий runtime smoke остаётся. Не заменять direction checks blacklist отдельных названий и не запрещать все межпакетные imports без учёта слоя.

## Оценка SOLID и сохранение инвариантов

| Принцип | Оценка |
| --- | --- |
| SRP | Runtime timers/resources отделены от finite usecases; domain transitions чистые. Hydration объединяет Analytics-prepared и local-calculation режимы. Размер repository сам по себе не finding: его execution transaction имеет общий инвариант. |
| OCP | Decision rules/strategy и узкие ports допускают расширение. Transport/vendor errors и raw operation schema заставляют менять policy при замене adapter. Единая стратегия по коду — принятое ограничение продукта. |
| LSP | Missing-broker exception mismatch нарушает observable contract; старый broker port и input `object` не совпадают с actual implementation. Analytics source имеет явный neutral failure contract. |
| ISP | Большинство локальных ports узкие. Старый broker port шире потребностей read consumers; `object`/raw dictionaries не описывают необходимые postconditions. |
| DIP | Shared/domain не зависят от ORM/HTTP; concrete adapters собраны в composition. Некоторые application services/usecases всё ещё импортируют concrete storage, adapter errors и чужие calculators. |

Сохранять при исправлениях: Core — подтверждённые факты/PostgreSQL; Worker — durable SQLite/execution/outbox; Analytics — market-only без credentials/portfolio/storage. Целевой перенос Worker ещё не выбран и не является исправлением перечисленных дефектов.

Не разрывать execution/ledger/cycle/outbox atomicity, UUID/sequence ordering и replay; не переносить ORM sessions через threads/awaits. Сохранить shared lifecycle policy, terminal CLOSED, manual resume обычного HOLD, immutable broker scope, UTC milliseconds и recovery journals. В [Worker domain transitions](../../src/trading_automaton/domain/trading_cycle.py) уже есть чистые `mark_buy/mark_sell`, которые storage правильно использует внутри своей транзакции.

Проверки TTL после preparation, внутри tick и перед SDK сохраняют исходный frame/book expiry. Active intent gate и repository CAS защищают повторный intent и устаревший cycle observation. Cash reservations публикуются после commit и восстанавливаются из durable intents. Эти решения следует сохранить при устранении I2.

Оставшиеся sync SQLite calls в event loop — известное ограничение; offload W3 уже кандидат очереди. Без latency/load measurements оно не доказывает свежий incident. Portfolio collector C5 также остаётся кандидатом, а завершённые A1/C1–C4/W4 не означают отсутствия найденных контрактных дефектов.

## Приоритет исправлений

1. Закрыть I2: lifecycle/command актуальность до создания нового intent; проверить race с двумя automation и supervision ранее committed/sent заявки.
2. Закрыть I1 и I3: согласованный missing broker contract и rollback ownership при builder failure/cancellation.
3. Исправить M2/M4 и связанные M1 signatures; это небольшие изменения с проверяемым результатом и без нового транспорта/storage.
4. Описать neutral failure/recovery contracts M3, очистить зависимости M5 и закрепить допустимые imports M6. Объём каждого изменения сверять с актуальной очередью перед реализацией.

## Проверки и независимое ревью

Статически проверены actual composition и пути до repositories/adapters, error inheritance, ownership ресурсов и выбранные regression tests. Существующее покрытие включает Analytics generation/TTL/outage, command replacement during preparation, batch rollback до dispatch, postcommit cleanup, execution/cycle/outbox atomicity/replay, active-intent/cycle CAS, access scope и recovery после повторного открытия SQLite.

Авторы пакетов: `core_audit` — Core; `worker_audit` — Worker; `boundaries_audit` — Analytics/shared и матрица импортов. `core_audit` независимо подтвердил Worker builder leak и узкий HOLD race; `worker_audit` независимо проверил Core missing-broker chain и boundary dependency claims. Автор сводного документа — `worker_audit`; локальную верификацию выполнил `audit_verification`. Независимое ревью итогового документа поручено `core_audit`, который его не писал.

`core_audit` независимо проверил минимальность рекомендаций, применив инструкции Ponytail 4.13.0 full/review. Рекомендации сохраняют существующие DTO/contracts и atomic boundaries, используют обычный cleanup стандартной библиотеки, не требуют новых frameworks. Удаление неиспользуемых путей M5 допускается после проверки всех callers. Существенных замечаний к отчёту и verification helper не выявлено; Minor к карте portfolio collector исправлен добавлением `CollectPortfolioSnapshotsUsecase`.

Локальные доказательства сохранены в `develop/reports/layered-solid-20261006/`. [Журнал окружения](../../develop/reports/layered-solid-20261006/verification-environment.log) содержит команды, exit codes и ограничения; [скрипт проверки](../../develop/reports/layered-solid-20261006/verify_audit.py) использует реальные application imports, синтетические ports и SQLite в памяти. Подмена SDK modules не применялась.

| Проверка | Результат и граница доказательства |
| --- | --- |
| I1 missing broker | Реальные `BrokerConnectionService` и `CheckBrokerConnectionUsecase`, fake repository с настоящим `UserBrokerNotFoundError`: исключение не переведено в `UseCaseError`; get вызван один раз, adapter factory ни разу. Actual repository и HTTP 500 chain проверены статически, полный route с actual composition/БД не выполнялся. |
| I2 stale decision admission | Реальный `LocalAutomationRepository`, SQLite в памяти: сначала сохранён `HOLD`, revision 2; stale decision revision 1 создаёт `DISPATCH_PENDING` intent. Полное async tick/control interleaving и broker submission не выполнялись. |
| I3 builder cleanup | Только независимая статическая проверка `core_audit`; SDK-dependent composition не импортировалась. |
| Выбранный pytest набор | Exit 0: **45 passed, 1 warning, 2.71 s**. Warning — StarletteDeprecationWarning о httpx/TestClient. Набор не проверяет весь backend. |
| Матрица импортов | AST всего текущего `src`; результаты и проверка existing architecture function записаны в [verification.log](../../develop/reports/layered-solid-20261006/verification.log). |

Команда выбранного набора, результат — в [pytest.log](../../develop/reports/layered-solid-20261006/pytest.log):

```sh
PYTHONDONTWRITEBYTECODE=1 develop/reports/layered-solid-20261006/.venv/bin/python -B -m pytest \
  tests/test_architecture.py tests/market_analytics tests/contracts/test_analytics.py \
  tests/integration/test_worker_storage_isolation.py tests/services/test_connection_service.py \
  tests/usecases/test_connection_usecases.py -p no:cacheprovider \
  --basetemp=develop/reports/layered-solid-20261006/pytest-temp -q
```

Изолированное окружение создано из locked dependencies с исключением `t-tech-investments`: его установка заблокирована TLS trust chain. Установлены 60 остальных packages. SDK-dependent `tests/test_composition.py`, `tests/trading_automaton/test_streaming_composition.py` и `tests/integration/test_analytics_worker_contract.py` не запускались; это исключение до collection, а не pytest skips и не пройденные проверки.

Финальное независимое ревью `core_audit` завершено: **ACCEPTED AUDIT**, замечания к артефактам — **Critical 0 / Important 0 / Minor 0**. В исходном коде остаются открытыми **3 Important / 6 Minor**; аудит завершён, исправления не выполнялись, architecture PASS не установлен.

Проверки допускаются только локально/на изолированных fixtures; broker API, SSH, TRADE, deploy, рабочая БД и volumes исключены. Документация содержит отдельный drift: `docs/architecture.md` перечисляет миграции до `0002`, а actual chain включает `0003`; старое описание исчерпания retry также не заменяет текущую cooldown/recovery policy кода и тестов.
