# ENV-конфигурация сервисов и подготовка базы дня

Пользователь 15.10 подтвердил scope: **ENV, документация и автоподготовка базы**. Несколько UID и автоматический ARM отложены. Планировщик подготовки дня уже существует; повторно механизм baseline не реализуется. Проверка PROD 15.10 в 09:39 МСК подтвердила Core healthy и фактический daily flag `false`. Этот этап разработки не меняет runtime/VPS.

Используются существующие Core Settings и AutomatonSettings/StrategySettings. Analytics получает небольшой AnalyticsSettings без новых зависимостей. Composition читает Settings и передаёт конкретные значения потребителям; бизнес-сервисы не читают `os.environ`. Все эксплуатационные поля Settings и выявленные интервалы покрываются Compose и [единым каталогом ENV](../../configuration/environment.md). При старте отклоняются неконечные, нулевые и отрицательные интервалы; retry schedules должны быть непустыми, конечными и положительными; очередь ограничена. Значения по умолчанию и прямые вызовы конструкторов остаются совместимыми.

Новые имена и defaults приведены ниже. Дополнительно существующие поля Settings, включая пул БД, повторы Collector, цикл Worker, логирование, URL и путь SQLite, должны доходить до потребителей. Исправляется игнорирование параметров пула БД в HTTP lifecycle Core. Тесты проверяют передачу нестандартных значений, а не только разбор Settings.

Три ENV-шаблона полны и согласованы. Generic/TEST оставляют daily выключенным. PROD-пример явно задаёт `DAILY_ADMISSION_PREPARATION_ENABLED=true` и пустой canary UID — профиль подготовки базы без автоматического выбора инструмента. App/Compose default остаётся `false`, режим примера — `READ_ONLY`, стратегия выключена. Планировщик работает только в PROD+TRADE с выбранным broker ID; безопасный READ_ONLY-шаблон сам подготовку не запускает. Применение разрешённых значений приватного ENV, контролируемый перезапуск Core и проверка health/preparation/базы текущего дня выполняются отдельно. Daily не выдаёт ARM, не снимает HOLD/STOP и не отменяет recovery.

Host alias Core `CORE_ANALYTICS_URL` отделён от Worker `ANALYTICS_URL`. В base Compose unset сохраняет `http://analytics:8001`, явно пустая строка выключает наблюдение Core. В PROD unset/empty выключает наблюдение, явный URL разрешён. URL Worker не переносится между контурами. Необязательный `AUTOMATON_WORKER_ID` использует fallback `INTERNAL_WORKER_ID`, затем прежний default; равенство идентичностей в PROD обязательно. Remote Collector всегда получает `PORTFOLIO_SNAPSHOT_ALL_ENVIRONMENTS=true` как контракт общей БД.

Для каждого параметра каталог указывает сервис/alias, тип и единицу, default и проверку, назначение, секретность, перезапуск и пример. Шаблоны содержат placeholders, права приватного ENV — `0600`; секреты не выводятся. Портфель, broker token и immutable identity подключения, baseline/reconciliation/ARM/лимиты/revisions остаются доменными данными в хранилищах. Глобальные broker credentials в ENV вместо существующего сервиса не вводятся.

Фиксированные контракты описываются, но не становятся произвольными настройками: owner lease 30 s, permit/mark 1 s, срок reconciliation, market TTL ceiling 2000 ms и минутные свечи, календарь MSK, freshness диагностики Worker, Telegram HTTPS authority/no proxy/no redirect/4096 chars/64 KiB response, ограничения auth/crypto, SyncExecutor 4, singleton Worker и один процесс backend, внутренние порты, volumes/shared network topology/version pins. Формулы стратегии принадлежат её версии. Named volumes сохраняются; ротация Docker logs настраивается, stop grace и security topology фиксированы.

Frontend использует same-origin `/api/runtime`, без новых browser secrets. `DEV_API_PROXY_TARGET` — параметр только Vite Node dev server; default `http://localhost:8000`, допускается HTTP(S) origin без credentials. Production build его не читает.

**DoD:** покрытие Settings → Compose/шаблоны/каталог и рендер TEST/PROD подтверждены; нестандартные значения достигают потребителей; meaningful RED/GREEN и проверки неверных значений пройдены; контуры, credentials и контракты сохранены; независимое ревью без открытых C/I. Нет миграций, проверок рабочей БД, branch/stage/commit/push, SSH/deploy, ARM/Resume и реальных отправок.

## Точные новые имена и defaults

| Потребитель | ENV | Default |
|---|---|---|
| core | `DAILY_ADMISSION_CHECK_INTERVAL_SECONDS` | `60` |
| core | `DAILY_ADMISSION_ATTEMPT_TIMEOUT_SECONDS` | `30` |
| core | `DAILY_ADMISSION_RETRY_DELAYS_SECONDS` | `[60,120,300]` |
| core | `READINESS_ALERT_POLL_INTERVAL_SECONDS` | `10` |
| core | `READINESS_ALERT_STARTUP_GRACE_SECONDS` | `60` |
| core | `READINESS_ALERT_HEARTBEAT_CONFIRMATION_SECONDS` | `30` |
| core | `READINESS_ALERT_CONTROL_CONFIRMATION_SECONDS` | `30` |
| core | `READINESS_ALERT_OUTBOX_CONFIRMATION_SECONDS` | `60` |
| core | `READINESS_ALERT_RETRY_DELAYS_SECONDS` | `[30,60,300]` |
| core | `READINESS_ALERT_QUEUE_LIMIT` | `16` |
| core | `TELEGRAM_REQUEST_TIMEOUT_SECONDS` | `5` |
| core | `MARKET_RECOVERY_RETRY_BASE_SECONDS` | `1` |
| core | `MARKET_RECOVERY_PROBE_INTERVAL_SECONDS` | `60` |
| core | `MARKET_OPERATION_TIMEOUT_SECONDS` | `10` |
| core | `MARKET_CLOSE_TIMEOUT_SECONDS` | `5` |
| core | `MARKET_QUIET_SECONDS` | `30` |
| core | `MARKET_CANDLES_REFRESH_SECONDS` | `60` |
| core | `ANALYTICS_VERSION_HTTP_TIMEOUT_SECONDS` | `1` |
| worker | `AUTOMATON_CORE_HTTP_TIMEOUT_SECONDS` | `10` |
| worker | `AUTOMATON_ANALYTICS_HTTP_TIMEOUT_SECONDS` | `1` |
| worker | `AUTOMATON_BROKER_RECOVERY_PROBE_SECONDS` | `60` |
| worker | `AUTOMATON_ACCOUNT_SNAPSHOT_TTL_SECONDS` | `60` |
| analytics | `ANALYTICS_CORE_HTTP_TIMEOUT_SECONDS` | `2` |
| frontend_dev | `DEV_API_PROXY_TARGET` | `http://localhost:8000` |
| deployment_operational | `DOCKER_LOG_MAX_SIZE` | `10m` |
| deployment_operational | `DOCKER_LOG_MAX_FILES` | `5` |

Все существующие Settings поля и aliases также входят в scope. Тесты и runtime не зависят от локального develop/reports/parameter-contract; канонические tracked имена приведены здесь и в каталоге ENV.
