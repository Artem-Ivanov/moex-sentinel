# Переменные окружения

Этот каталог описывает ENV Core, Worker, Analytics, Docker Compose и локального
Vite dev server. Проверка покрытия в `tests/test_environment_configuration_contract.py`
сверяет поля Pydantic Settings, каталог, три шаблона и Compose. Значение настройки
обычно считывается при старте процесса; после изменения перезапускайте сервис,
который её читает. Значения в примерах — placeholders и безопасные defaults, а не
текущее содержимое VPS.

Обозначения: Core — backend и API; Worker — `trading-automaton`; Collector —
`portfolio-snapshot-worker`; Analytics — рыночные расчёты без credentials и
портфеля. «Secret» отмечает значения, которые нельзя выводить в логи или хранить
в открытом tracked-файле. Для приватного удалённого ENV используйте права `0600`.

## Core

| Переменная (service / alias контейнера) | Тип / единица | Default и проверка | Назначение | Secret | Restart | Example |
|---|---|---|---|---|---|---|
| `APPLICATION_ENVIRONMENT` (Core, Worker, Collector) | enum | Settings default `TEST`; Compose требует значение; допустимы `TEST` и `PROD`; примеры задают `TEST`/`PROD` | Контур данных и торговых guard | Нет | сервисы | `TEST` |
| `BROKER_ACCESS_MODE` (Core, Worker, Collector) | enum | Settings и Compose требуют значение; допустимы `READ_ONLY` и `TRADE`; примеры безопасно задают `READ_ONLY` | Глобальная блокировка брокерских операций процесса | Нет | сервисы | `READ_ONLY` |
| `DATABASE_URL` (Core, Collector) | URL | Native Core: `sqlite:///data/moex_sentinel.db`; Compose строит PostgreSQL URL из `POSTGRES_*` и `DATABASE_HOST` | Хранилище Core и снимков | Да, содержит пароль | Core и Collector | `postgresql+psycopg://user:password@database:5432/moex_sentinel` |
| `DATABASE_POOL_SIZE` (Core, Collector) | integer / connections | `5`; больше 0 | Размер пула PostgreSQL | Нет | Core и Collector | `5` |
| `DATABASE_MAX_OVERFLOW` (Core, Collector) | integer / connections | `10`; не меньше 0 | Дополнительные соединения сверх пула | Нет | Core и Collector | `10` |
| `DATABASE_POOL_TIMEOUT_SECONDS` (Core, Collector) | float / seconds | `5`; конечное значение больше 0 | Ожидание свободного соединения | Нет | Core и Collector | `5` |
| `SANDBOX_RETRY_LIMIT` (Core, Worker) | integer / retries | `5`; не меньше 0 | Число повторов sandbox-действия после первой попытки | Нет | Core и Worker | `5` |
| `PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS` (Collector) | integer / seconds | `60`; не меньше 60 | Период сбора согласованных снимков | Нет | Collector | `60` |
| `PORTFOLIO_SNAPSHOT_ALL_ENVIRONMENTS` (Collector) | boolean | локальный default `false` | Собирать ли TEST и PROD | Нет | Collector | `false` |
| `PORTFOLIO_SNAPSHOT_RETRY_LIMIT` (Collector) | integer / retries | `3`; не меньше 0 | Число повторов сбоя снимка | Нет | Collector | `3` |
| `PORTFOLIO_SNAPSHOT_RETRY_BASE_SECONDS` (Collector) | float / seconds | `1`; конечное значение больше 0 | Базовая задержка повторов | Нет | Collector | `1` |
| `LOG_LEVEL` (Core, Worker) | enum/string | `INFO` | Уровень логирования | Нет | соответствующий сервис | `INFO` |
| `LOG_FORMAT` (Core, Worker) | enum | `json`; `json` или `console` | Формат структурированных логов | Нет | соответствующий сервис | `json` |
| `CORE_ANALYTICS_URL` (host alias → Core `ANALYTICS_URL`) | HTTP(S) URL/string | local/TEST: unset использует `http://analytics:8001`; явная пустая строка отключает наблюдение; PROD unset/empty отключает | Адрес Analytics для чтения версии Core; отделён от Worker `ANALYTICS_URL` | Нет | Core | `http://analytics:8001` |
| `AUTH_SESSION_COOKIE_NAME` (Core) | string | `__Host-moex-session`; PROD требует отдельное имя с `__Host-` | Имя защищённой session cookie | Нет | Core | `__Host-moex-prod-session` |
| `AUTH_ALLOWED_ORIGIN` (Core) | HTTPS origin | native Settings default пусто; Compose требует значение; PROD требует точный HTTPS origin; examples задают адрес UI | Проверка Origin для операторской сессии | Нет | Core | `https://example.invalid` |
| `AUTH_USERNAME` (Core) | string | пусто в native Settings; Compose требует значение | Имя оператора | Нет | Core | `operator` |
| `AUTH_PASSWORD_HASH` (Core) | scrypt verifier | пусто в native Settings; Compose требует сгенерированный verifier | Хэш пароля оператора; генерируется командой из README | Да | Core | `replace-with-generated-scrypt-verifier` |
| `AUTH_INSECURE_LOOPBACK` (Core) | boolean | `false` | Разрешить HTTP cookie только для локального loopback | Нет | Core | `false` |
| `INTERNAL_WORKER_ID` (Core, Worker) | string | пусто; в PROD обязательно и равно `AUTOMATON_WORKER_ID` | Идентификатор Core owner для Worker | Нет | Core и Worker | `worker-prod-1` |
| `INTERNAL_WORKER_BROKER_ID` (Core, Worker) | UUID | пусто; в PROD обязателен | Выбранный broker identity Worker | Нет | Core и Worker | `11111111-2222-4333-8444-555555555555` |
| `INTERNAL_WORKER_TOKEN` (Core, Worker) | 64 lowercase hex | пусто; в PROD обязателен | Внутреннее подтверждение Worker identity | Да | Core и Worker | `replace-with-64-lowercase-hex-secret` |
| `DAILY_ADMISSION_PREPARATION_ENABLED` (Core) | boolean | `false`; при `true` также нужны `APPLICATION_ENVIRONMENT=PROD`, `BROKER_ACCESS_MODE=TRADE` и заданный `INTERNAL_WORKER_BROKER_ID`, иначе результат `SCOPE_UNAVAILABLE` | Разрешить планировщику готовить базу торгового дня | Нет | Core | `false` |
| `DAILY_ADMISSION_CHECK_INTERVAL_SECONDS` (Core) | integer / seconds | `60`; больше 0 | Интервал планировщика подготовки | Нет | Core | `60` |
| `DAILY_ADMISSION_ATTEMPT_TIMEOUT_SECONDS` (Core) | integer / seconds | `30`; больше 0 | Максимальное время одной подготовки | Нет | Core | `30` |
| `DAILY_ADMISSION_RETRY_DELAYS_SECONDS` (Core) | JSON array / seconds | `[60,120,300]`; непустой список конечных значений больше 0 | Задержки повторов подготовки | Нет | Core | `[60,120,300]` |
| `READINESS_ALERT_POLL_INTERVAL_SECONDS` (Core) | integer / seconds | `10`; больше 0 | Частота проверки сигналов готовности | Нет | Core | `10` |
| `READINESS_ALERT_STARTUP_GRACE_SECONDS` (Core) | integer / seconds | `60`; больше 0 | Пауза после старта перед readiness-уведомлениями | Нет | Core | `60` |
| `READINESS_ALERT_HEARTBEAT_CONFIRMATION_SECONDS` (Core) | integer / seconds | `30`; больше 0 | Срок подтверждения heartbeat | Нет | Core | `30` |
| `READINESS_ALERT_CONTROL_CONFIRMATION_SECONDS` (Core) | integer / seconds | `30`; больше 0 | Срок подтверждения control progress | Нет | Core | `30` |
| `READINESS_ALERT_OUTBOX_CONFIRMATION_SECONDS` (Core) | integer / seconds | `60`; больше 0 | Срок подтверждения доставки outbox | Нет | Core | `60` |
| `READINESS_ALERT_RETRY_DELAYS_SECONDS` (Core) | JSON array / seconds | `[30,60,300]`; непустой список конечных значений больше 0 | Задержки повторной отправки readiness alert | Нет | Core | `[30,60,300]` |
| `READINESS_ALERT_QUEUE_LIMIT` (Core) | integer / alerts | `16`; диапазон 1–1024 | Ограничение очереди readiness alert | Нет | Core | `16` |
| `TELEGRAM_ALERTS_ENABLED` (Core) | boolean | `false`; при `true` обязательны token и chat ID | Включает доставку readiness alert | Нет | Core | `false` |
| `TELEGRAM_BOT_TOKEN` (Core) | Telegram token | пусто; проверяется формат Telegram token | Credential бота | Да | Core | `replace-with-telegram-token` |
| `TELEGRAM_CHAT_ID` (Core) | string | пусто; максимум 128 символов, без control chars | Получатель readiness alert | Да | Core | `replace-with-chat-id` |
| `TELEGRAM_REQUEST_TIMEOUT_SECONDS` (Core) | float / seconds | `5`; конечное значение больше 0 | Timeout запроса Telegram | Нет | Core | `5` |
| `MARKET_RECOVERY_RETRY_BASE_SECONDS` (Core) | float / seconds | `1`; конечное значение больше 0 | Базовая задержка восстановления рынка | Нет | Core | `1` |
| `MARKET_RECOVERY_PROBE_INTERVAL_SECONDS` (Core) | integer / seconds | `60`; больше 0 | Интервал проверки восстановления рынка | Нет | Core | `60` |
| `MARKET_OPERATION_TIMEOUT_SECONDS` (Core) | float / seconds | `10`; конечное значение больше 0 | Timeout market operation | Нет | Core | `10` |
| `MARKET_CLOSE_TIMEOUT_SECONDS` (Core) | float / seconds | `5`; конечное значение больше 0 | Timeout закрытия рыночного соединения | Нет | Core | `5` |
| `MARKET_QUIET_SECONDS` (Core) | float / seconds | `30`; конечное значение больше 0 | Окно тишины перед восстановлением | Нет | Core | `30` |
| `MARKET_CANDLES_REFRESH_SECONDS` (Core) | float / seconds | `60`; конечное значение больше 0 | Период обновления завершённых свечей | Нет | Core | `60` |
| `ANALYTICS_VERSION_HTTP_TIMEOUT_SECONDS` (Core) | float / seconds | `1`; конечное значение больше 0 | HTTP timeout чтения версии Analytics | Нет | Core | `1` |

## Worker и стратегия

| Переменная (service / alias контейнера) | Тип / единица | Default и проверка | Назначение | Secret | Restart | Example |
|---|---|---|---|---|---|---|
| `CORE_URL` (Worker) | HTTP(S) URL with host | `http://backend:8000`; без userinfo, валидный port | Внутренний Core API | Нет | Worker | `http://backend:8000` |
| `ANALYTICS_URL` (Worker) | HTTP(S) URL with host | `http://analytics:8001`; без userinfo, валидный port; независим от Core alias | Внутренний API рыночных данных | Нет | Worker | `http://analytics:8001` |
| `AUTOMATON_DATABASE_URL` (Worker) | SQLite URL | `sqlite:///data/trading_automaton.db` в native; Compose задаёт постоянный container path | Durable SQLite Worker; существующий DB path и volume identity сохраняются при обычном обновлении | Путь не secret | Worker | `sqlite:////app/data/trading_automaton.db` |
| `AUTOMATON_WORKER_ID` (Worker) | string | host override; иначе `INTERNAL_WORKER_ID`, затем `trading-automaton-1`; PROD должен совпасть с Core ID | Имя owner lease | Нет | Worker | пусто для fallback |
| `AUTOMATON_ITERATION_SECONDS` (Worker) | float / seconds | `1`; конечное значение больше 0 | Интервал цикла Worker | Нет | Worker | `1` |
| `AUTOMATON_HEARTBEAT_INTERVAL_SECONDS` (Worker) | float / seconds | `3`; конечное значение больше 0 | Интервал heartbeat | Нет | Worker | `3` |
| `AUTOMATON_CORE_HTTP_TIMEOUT_SECONDS` (Worker) | float / seconds | `10`; конечное значение больше 0 | Timeout вызовов Core | Нет | Worker | `10` |
| `AUTOMATON_ANALYTICS_HTTP_TIMEOUT_SECONDS` (Worker) | float / seconds | `1`; конечное значение больше 0 | Timeout вызовов Analytics | Нет | Worker | `1` |
| `AUTOMATON_BROKER_RECOVERY_PROBE_SECONDS` (Worker) | float / seconds | `60`; конечное значение больше 0 | Интервал broker recovery probe | Нет | Worker | `60` |
| `AUTOMATON_ACCOUNT_SNAPSHOT_TTL_SECONDS` (Worker) | float / seconds | `60`; конечное значение больше 0 | TTL кэша broker account snapshot | Нет | Worker | `60` |
| `FACT_OUTBOX_BATCH_SIZE` (Worker) | integer / events | `100`; больше 0 | Максимальный batch typed outbox | Нет | Worker | `100` |
| `FACT_OUTBOX_DEADLINE_MS` (Worker) | integer / milliseconds | `1000`; больше 0 | Deadline доставки outbox | Нет | Worker | `1000` |
| `STRATEGY_ENABLED` (Worker) | boolean | `false` | Общий переключатель алгоритма | Нет | Worker | `false` |
| `STRATEGY_BUY_ORDER_LOTS` (Worker) | integer / lots | `1`; больше 0 | Размер базовой заявки | Нет | Worker | `1` |
| `STRATEGY_STOP_LOSS_PERCENT` (Worker) | decimal / percent | `5`; диапазон `(0,100]` | Стоп-лосс стратегии | Нет | Worker | `5` |
| `STRATEGY_TAKE_PROFIT_PERCENT` (Worker) | decimal / percent | `6`; диапазон `(0,100]` | Тейк-профит стратегии | Нет | Worker | `6` |
| `STRATEGY_AVERAGING_STEP_PERCENT` (Worker) | decimal / percent | `0.5`; диапазон `(0,100]` | Шаг усреднения | Нет | Worker | `0.5` |
| `STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT` (Worker) | decimal / percent | `0.5`; диапазон `(0,100]` | Частичная фиксация прибыли | Нет | Worker | `0.5` |
| `STRATEGY_PARTIAL_SELL_PERCENT` (Worker) | decimal / percent | `25`; диапазон `(0,100]` | Доля частичной продажи | Нет | Worker | `25` |
| `STRATEGY_MAX_PARTIAL_SELL_STEPS` (Worker) | integer / steps | `3`; не меньше 0 | Число частичных продаж | Нет | Worker | `3` |
| `STRATEGY_ORDER_TTL_SECONDS` (Worker) | integer / seconds | `10`; больше 0 | TTL заявки стратегии | Нет | Worker | `10` |
| `STRATEGY_ORDER_RETRY_LIMIT` (Worker) | integer / retries | `3`; не меньше 0 | Повторы действия стратегии | Нет | Worker | `3` |
| `STRATEGY_CORE_RETRY_LIMIT` (Worker) | integer / retries | `5`; не меньше 0 | Повторы запросов Core | Нет | Worker | `5` |

## Analytics

| Переменная (service / alias контейнера) | Тип / единица | Default и проверка | Назначение | Secret | Restart | Example |
|---|---|---|---|---|---|---|
| `ANALYTICS_CORE_URL` (Analytics) | HTTP URL | `http://backend:8000`; credentials запрещены | Core read API для market-only Analytics | Нет | Analytics | `http://backend:8000` |
| `ANALYTICS_CORE_HTTP_TIMEOUT_SECONDS` (Analytics) | float / seconds | `2`; конечное значение больше 0 | Timeout чтения Core | Нет | Analytics | `2` |

## Compose и локальная разработка

| Переменная (потребитель) | Тип / единица | Default и проверка | Назначение | Secret | Restart | Example |
|---|---|---|---|---|---|---|
| `POSTGRES_DB` (database, Core, Collector) | PostgreSQL identifier | Compose requires value with no fallback; examples set `moex_sentinel` | Имя общей базы | Нет | Compose stack | `moex_sentinel` |
| `POSTGRES_USER` (database, Core, Collector) | string | Compose requires value with no fallback; examples set `moex_sentinel` locally and `sentinel` remotely | Пользователь PostgreSQL | Нет | Compose stack | `sentinel` |
| `POSTGRES_PASSWORD` (database, Core, Collector) | string | Compose requires a nonempty secret; no fallback; examples contain placeholders | Пароль общей базы | Да | Compose stack | `replace-with-random-secret` |
| `POSTGRES_PORT` (local database) | integer / port | `55432`; loopback bind | Локальный опубликованный порт PostgreSQL | Нет | Compose stack | `55432` |
| `DATABASE_HOST` (Core, Collector, migrations) | DNS hostname | Compose fallback `database` in either contour; remote examples/private env set `sentinel-shared-postgres` | Адрес общей PostgreSQL | Нет | сервисы | `sentinel-shared-postgres` |
| `POSTGRES_VOLUME_NAME` (local database) | Docker volume | `moex-sentinel-postgres-data` | Существующий volume PostgreSQL | Нет | Compose stack | `moex-sentinel-postgres-data` |
| `AUTOMATON_VOLUME_NAME` (Worker) | Docker volume | `moex-sentinel_automaton-data` local | Durable Worker SQLite volume | Нет | Compose stack | `moex-sentinel_automaton-data` |
| `APP_PORT` (frontend) | integer / port | `8080`; bind только `127.0.0.1` | Локальный порт UI | Нет | frontend | `8080` |
| `COMPOSE_PROJECT_NAME` (remote Compose) | string | Если переменная не задана, Compose берёт имя из project directory; remote TEST/PROD examples явно задают `moex-sentinel-remote` / `moex-sentinel-prod` | Изолирует имена сетей и ресурсов контуров | Нет | Compose stack | `moex-sentinel-prod` |
| `RELEASE_TAG` (remote images) | string | обязательный уникальный reviewed tag | Выбирает pinned release images | Нет | Compose stack | `replace-with-reviewed-git-commit` |
| `DOCKER_LOG_MAX_SIZE` (remote/local runtime) | Docker size | `10m` | Размер файла JSON log до ротации | Нет | контейнеры | `10m` |
| `DOCKER_LOG_MAX_FILES` (remote/local runtime) | integer / files | `5`; положительное целое | Число файлов JSON log | Нет | контейнеры | `5` |
| `DEV_API_PROXY_TARGET` (Vite Node dev server only) | HTTP(S) origin | `http://localhost:8000`; credentials/path/query/fragment запрещены | Origin Core для dev proxy `/api` | Не помещать секреты | dev server | `http://localhost:8000` |

Vite читает `DEV_API_PROXY_TARGET` только в Node-конфигурации dev server. Vite не
встраивает это значение в browser bundle; браузер по-прежнему обращается к
same-origin `/api/runtime`. Dev target должен содержать только scheme и origin.

## Фиксированные контракты и бизнес-данные

Следующие параметры намеренно остаются кодовыми инвариантами: owner lease 30 s,
permit/mark 1 s, срок reconciliation, ceiling market TTL 2000 ms, минутные
свечи, календарь MSK, freshness диагностики Worker, Telegram HTTPS authority,
отсутствие proxy и redirect, лимит текста 4096 символов и ответа 64 KiB,
ограничения auth/crypto, SyncExecutor 4, singleton Worker и один процесс backend.
Не публикуются внутренние порты; действующие volume/network layout, release pins,
Worker stop grace и security topology не являются произвольными ENV настройками.
SQLite path и volume identity Worker сохраняются при обычном обновлении; смена
хранилища требует согласованной миграции durable данных.
Удалённый Compose всегда передаёт `PORTFOLIO_SNAPSHOT_ALL_ENVIRONMENTS=true`,
так как TEST и PROD используют одну Core БД.

Состав портфеля, broker token и immutable broker identity пользовательского
подключения, дневная база, reconciliation, ARM, лимиты и revisions — доменные
данные в Core/Worker-хранилищах. Их нельзя переносить в process ENV. Глобальные
broker credentials для них не вводятся. Алгоритмические формулы принадлежат
версии стратегии и не заменяются operational knobs.

## Безопасные профили и применение daily preparation

`.env.example` и remote TEST пример оставляют `DAILY_ADMISSION_PREPARATION_ENABLED=false`.
Remote PROD пример включает `true` для автоматической подготовки дневной базы.
Параметр не выбирает canary-инструмент и не является ручным ARM или допуском:
фактическая заявка остаётся привязана к `IN_WORK`, owner/lease, режиму `TRADE`,
свежим брокерским данным и четырём account-level лимитам.

Для отдельной локальной активации автоматической базы на существующем VPS
сохраните текущий приватный env и credentials. Изменяйте флаг только в рамках
согласованной PROD-конфигурации. Новый файл создавайте из шаблона только при
первичной настройке. Установите права `0600` и проверьте выбранный Compose файл
без вывода секретов. Затем выполните контролируемый перезапуск только Core по
действующей процедуре проекта. Проверьте Core health и состояние подготовки через
штатные read-only интерфейсы. До этих шагов флаг в tracked примере не означает, что
функция активна на VPS. Не выполняйте ARM/Resume как часть этой настройки.

Шаблоны: [локальный Compose](../../.env.example), [remote TEST](../../deploy/remote/.env.example),
[remote PROD](../../deploy/remote/.env.production.example). Подробнее: [remote Compose](../deployment/remote-compose.md)
и [проверка допуска PROD](../deployment/prod-trading-start.md).
