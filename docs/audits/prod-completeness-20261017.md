# Проверка полноты PROD-пути — 17.10.2026

Статический аудит исходников, пересверка checkout `f751ab3`. Пользователь подтвердил границу: исправлять только реальные PROD-пробелы, сохранить TEST/Sandbox и mocks в tests. PROD runtime, VPS, SQL/SQLite и брокерский API не проверялись; результаты не означают, что торговля сейчас работает. TEST и PROD используют отдельные Core API над согласованной общей PostgreSQL. Worker владеет durable SQLite и исполнением, Analytics получает рыночные данные без broker credentials.

Аудиторы: `core_prod_audit` и `worker_prod_audit` (read-only); интеграцию результатов выполнил root. Независимая перепроверка аудита по HEAD `f751ab3` — `prod_audit_revalidation`: подтвердил I1/I3/M1/M2 и сузил M3 с учётом существующего PROD coverage; фактические уточнения внесены ниже. Это не кросс-ревью изменений: продуктовый код в этой вехе не менялся.

## Результат

В просмотренных production composition не найдено подмены настоящих PROD вызовов Sandbox RPC. TEST/Sandbox ветки portfolio/session привязаны к `environment == "TEST"` и должны сохраниться. `None` для отсутствующих позиции/ордера/кадра, fail-closed recovery, READ_ONLY для Core broker adapter и тестовые fakes также являются намеренными.

Найдены реальные пробелы изоляции, исполнения и диагностики:

### I1 — admission API не закреплён за контуром Core

Путь: `create_app()` подключает public admission router в обоих контурах (`src/moex_sentinel/api/app.py:524–529`) → операторская сессия авторизует `/api/*` → `TradingAdmissionService` → общий `TradingAdmissionRepository`. Composition не передаёт ему `APPLICATION_ENVIRONMENT` (`src/moex_sentinel/composition.py:273–278`). `get_view()` ищет только по broker ID (`src/moex_sentinel/storage/repositories/trading_admission.py:1525–1588`); `revoke()` проверяет ID/revision, но не среду Core, и затем меняет допуск/переводит автоматы в HOLD (`:1766–1796`). `owner_stopped()` аналогично не связывает broker с вызывающим контуром (`:1695–1764`). `update_limits()` проверяет, что строка broker — PROD (`:1591–1616`), но не что запрос пришёл именно из PROD Core. Некоторые методы (`arm`) также проверяют сам broker, а не контур-источник.

Сценарий: авторизованный TEST-оператор, знающий PROD broker UUID, направляет GET/PUT/revoke/owner-transfer в TEST Core. При общей базе запрос может читать или менять PROD admission; revoke способен перевести реальные PROD автоматы в HOLD. Это статический вывод из композиции и repository guards, не проверенная live-эксплуатация.

Нужно единое контурное ограничение на границе всех операторских admission операций и в мутациях repository, включая новый pending-runtime GET и owner transfer. Дополнительная перепроверка `prod_audit_revalidation` уточнила: capture_baseline/reconcile уже вызывают scoped `AutomatonBrokerService.connection`, поэтому для этих двух операций обход не доказан; gap подтверждён для view, pending runtime, limits, arm, revoke и owner-stopped, обходящих этот resolver. В отдельной тестовой PostgreSQL воспроизвести TEST→PROD отказ без изменения строки и PROD→PROD штатный вызов. Регрессионная проверка должна охватить все operator методы, включая уже защищённые baseline/reconcile.

### I2 — ENV lifetime/retry не управляют живой заявкой, отмена у Worker отсутствует

`AutomatonSettings.order_ttl_seconds` и `order_retry_limit` заданы в `src/trading_automaton/config.py:59–68` и сохраняются/передаются в settings, однако `rg` текущего production кода находит потребителя только в конфигурации. `OrderTrackingService._terminal_response()` делает фиксированные 50 status polls с шагом 0.2 секунды (`src/trading_automaton/services/order_tracking.py:339–375`), затем помечает intent `UNCERTAIN` и automation `HOLD`. Его `TrackingBrokerPort` предоставляет только get status (`:37–39`); `BrokerSdkSession` в Worker также не реализует cancel. Наличие cancel в Core read-only adapter не помогает: PROD submit/cancel принадлежат Worker.

Сценарий: лимитная заявка не достигла terminal status примерно за 10 секунд. Локально автомат уходит в HOLD/UNCERTAIN, но nonterminal заявка остаётся на стороне брокера и может исполниться позже. Изменение `STRATEGY_ORDER_TTL_SECONDS` не меняет runtime поведение; `STRATEGY_ORDER_RETRY_LIMIT` не задаёт используемую политику. Уже существующий контракт в `docs/superpowers/specs/clean-slate-typed-runtime-cutover-design.md:153–154` обещает lifetime и retry limit.

Нужно согласовать точную lifecycle политику. Если остаётся TTL: привязать его к order tracking, выполнить account-bound cancel, дождаться конечного broker status и корректно учесть fill/cancel race и partial fill; резерв не освобождать по факту отправки cancel. `UNCERTAIN` не повторять. Неиспользуемый retry limit либо удалить/пометить как deprecated, либо определить его узкое безопасное назначение. Contract/unit/integration tests должны проверять fill до cancel, fill одновременно с cancel, подтверждённый cancel, timeout/unknown и restart.

### I3 — permanent broker failure скрыт heartbeat/control progress

`AnalyticsBrokerRuntime` при постоянной `BrokerOperationError` остаётся ожидающим закрытия (`src/trading_automaton/runtime/analytics_broker.py:65–75`). Coordinator считает task живой и продолжает control sync/heartbeat (`src/trading_automaton/runtime/streaming_coordinator.py:151–171,203–228`), а `WorkerDiagnostics` не показывает broker iteration/block reason (`src/sentinel_contracts/worker_diagnostics.py:44–74`). Поэтому Worker может выглядеть как `CONTROL_PROGRESS`/`CLEAR`, хотя автоматический торговый loop заблокирован до restart. Недоступность Analytics — другой случай: `AnalyticsFrameService.fetch()` возвращает `None`, а `RunBrokerIterationUsecase.execute()` всё равно выполняет recovery preparation; это не permanent stop, но successful control iteration также не доказывает пригодность рыночных данных для решения.

Это безопасный отказ, не TEST-заглушка. Исправление — публиковать реальное последнее broker progress и блокирующую причину в diagnostics/readiness/alerts; fail-closed поведение сохранить, не возобновлять submit по одному heartbeat. Добавить сквозной тест permanent broker failure + продолжающийся control heartbeat → degraded status с причиной.

### M1 — stream сообщает `api_trade_available=True` без подтверждения

Путь: `build_market_snapshot_gateway()` создаёт PROD `TInvestMarketStreamSource` (`src/moex_sentinel/composition.py:183–196`) → `TInvestStreamingAdapter._event()` копирует торговый статус, но жёстко подставляет `api_trade_available=True` (`src/moex_sentinel/adapters/tinvest/streaming.py:104–110`) → Core market snapshot → Analytics → Worker scheduler. Риск допуска к order не подтверждён: Core risk повторно проверяет `instrument.api_trade_available_flag` при выдаче permit. Тем не менее market readiness может быть ложной и провоцировать бесполезные попытки.

Нужно использовать подтверждённый read-only instrument/trading-status источник и его freshness либо представить поле как неизвестное и не допускать решения. Нельзя исправлять постоянной заменой на `False`. Тестировать истинное, ложное, отсутствующее и устаревшее значение от mapping до decision eligibility.

### M2 — первичный блокер допуска не формирует alert

`TradingReadinessAlertService` генерирует `READINESS_LOST` только после того, как в текущем процессе уже наблюдал `effective_can_submit=True` (`src/moex_sentinel/services/trading_readiness_alerts.py:147,174–175,238–251`). После рестарта с `IN_WORK`, baseline и здоровым Worker, но без caps, readiness может оставаться false без Telegram сообщения. Тесты `tests/services/test_trading_readiness_alerts.py:516–540` закрепляют молчание при initial DISARMED.

Если Telegram blocker notifications остаются принятым эксплуатационным требованием, нужен bounded startup grace alert для длительной блокировки при `IN_WORK`; обычный HOLD и намеренно отключённый режим не считать аварией. Состояние доставки/Telegram в runtime здесь не проверялось.

### M3 — не соединён полный Analytics → PROD decision → SDK → Core facts путь

Ключевой сквозной тест Analytics → Worker → durable execution → Core facts выполняется в TEST/Sandbox (`tests/integration/test_analytics_worker_contract.py:145–208`). При этом уже есть PROD TRADE integration coverage: `tests/integration/postgresql/test_trading_admission_flow.py:424` использует Core HTTP/PG и Worker admission/SDK, проверяет permit expiry, tracking/recovery, replay и settlement; `test_trading_admission_ingress.py:114` включает Worker file SQLite, authenticated Core HTTP/PG, lost ACK и runtime transfer. Эти тесты нельзя считать отсутствующими или заменять дублями.

Узкий пробел — единая локальная проверка Analytics HTTP → production decision runtime → SDK boundary → Core HTTP/PG, без подмены внутренних risk/admission layers. Нужен один такой production-composition test на отдельном тестовом PostgreSQL, без реальной заявки; случаи lost ACK/recovery/transfer переиспользовать из существующего покрытия, расширяя только недостающие взаимодействия. Live broker status/credentials и фактическая готовность VPS остаются отдельным read-only deployment gate.

## Полнота пути и проверенные допустимые ветки

| Путь | Результат статического просмотра | Ограничение |
|---|---|---|
| PROD env/token/config binding | Есть проверка endpoint, environment, selected account, worker identity и конфигурационного fingerprint | Runtime values не подтверждались |
| Core market / portfolio / risk | PROD ветки используют T-Invest market/operations RPC; daily baseline идемпотентно строится из read-only risk snapshot | API trade flag в stream mapping жёстко true; брокерские ответы не проверены live |
| Core admission | Есть owner/lease/config/IN_WORK/instrument/four-limit permit checks и atomic reservations | Operator API cross-contour scope выше |
| Worker decision / market freshness | Есть Analytics snapshot freshness, scheduler checks, limits, durable intent и idempotent request key | API flag выше; end-to-end PROD integration отсутствует |
| Broker submit / tracking | PROD permit связан с настоящим `BrokerSdkSession`; submit идёт только после durable SUBMITTING | После nonterminal timeout нет Worker cancel, unused TTL/retry config |
| Recovery / uncertain | Fail-closed, не выводит факт исполнения из одной похожей операции/позиции, не повторяет uncertain submit | Может блокировать автомат до внешнего подтверждения; это намеренная осторожность |
| Outbox / Core authority | Typed facts, ACK/retry и Core owns PostgreSQL | Heartbeat control progress не показывает broker iteration block |
| Alerts | Configurable Telegram transport and readiness observer | Initial sustained IN_WORK block after restart may be silent; alert delivery not runtime-verified |
| UI / Telegram | Status/readiness UI и configurable alert transport существуют | Startup-disabled alert case выше; текущий delivery не проверялся |

## Намеренные TEST/Sandbox и no-op формы — не удалять по grep

- TEST portfolio/session RPC вызывает T-Invest Sandbox; PROD ветка вызывает real operations endpoints.
- Core order adapter намеренно `READ_ONLY` в PROD; боевые submit/cancel принадлежат Worker.
- `None` при отсутствии позиции/ордера/кадра — валидный результат или fail-closed; `None` в uncertain reconciliation не доказывает, что ордер не был отправлен.
- Analytics fallback сохраняет настроенные thresholds при недостаточной истории и закрывает buy window.
- READ_ONLY, HOLD и `STRATEGY_ENABLED=false` — рабочие запреты.
- `pass` в exception/base declarations и factory injection/test doubles в тестах не являются product no-op.
- `create_app(test_auth_bypass=True)` используется тестами; этот параметр не передаётся production entrypoint.

## Предложенная граница реализации

Приоритетом для production readiness являются контурная изоляция admission API, truthful API-trade readiness и честное отображение permanent Worker failure. Lifecycle TTL/cancel — отдельный безопасный контракт с cancel/fill/restart semantics; его нельзя сводить к простой отправке cancel RPC. End-to-end PROD wiring test доказывает интеграцию без live order. Никакие deployment/DB/runtime выводы из этого статического отчёта не следуют.
