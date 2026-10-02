# PROD: подготовка контура только для чтения (P1/P2)

Код P1/P2 реализован и прошёл независимое code review и изолированные проверки.
Первоначальный PROD prototype остановлен по требованию общей БД.
Объединение Core, новый capacity gate и live приёмка ещё открыты. Пользователь
разрешил поэтапный переход READ_ONLY; реальные торговые действия требуют
отдельного допуска.
`APPLICATION_ENVIRONMENT=PROD`, `BROKER_ACCESS_MODE=READ_ONLY` и
`STRATEGY_ENABLED=false` обязательны. PROD TRADE отвергается до появления
проверенного допуска P3. Текущая Sandbox среда сохраняет свой env и volumes.

## Статус объединения БД — 02.10

Владелец требует одну PostgreSQL/одну database. Первоначальный отдельный PROD
prototype остановлен, listener8443 отключён, данные/volumes сохранены.
Этот runbook сейчас **не готов к запуску общей БД**: Compose/env template/
preflight ещё предполагают отдельную PROD PostgreSQL. До запуска нужно
исправить их, завершить scope guards/summary/collector и обновить Sandbox
backend до первой PROD записи. Worker storage отдельно проанализирован в
[аудите](../audits/2026-10-02-orchestration-and-worker-storage.md); миграция не выбрана.

## Изоляция

Подготовить отдельный root-only env по `deploy/remote/.env.production.example`, явно
задать PROD значения из таблицы ниже, READ_ONLY и strategy=false;
не копировать токен Sandbox. Установить отдельный пароль оператора. Заполненный
файл хранить с правами0600; credentials не выводить в отчёты. PROD token
владелец вводит через HTTPS UI. Наличие и права реального токена пока неизвестны;
Sandbox token/account/ledger в PROD не копируются.

| Ресурс | Sandbox | PROD |
| --- | --- | --- |
| Compose project | moex-sentinel-remote | moex-sentinel-prod |
| HTTPS origin | https://135.136.178.252 | https://135.136.178.252:8443 |
| UI loopback | 127.0.0.1:8080 | 127.0.0.1:8081 |
| Cookie | __Host-moex-session | __Host-moex-prod-session |
| PostgreSQL volume / database | Существующая общая PostgreSQL | Та же PostgreSQL/database; второй service не запускается |
| Worker volume | moex-sentinel-remote-automaton-data | moex-sentinel-prod-automaton-data |

Сети создаются отдельно по Compose project. Durable volumes external;
существующие Sandbox volumes не переименовывать и не удалять. Analytics не
получает broker credentials или данные портфеля. Backend, snapshot worker
и торговый Worker должны иметь одинаковые contour/access mode.
Worker сверяет Core contour/mode до изменяющих HTTP-запросов и SDK session;
общий TEST/PROD target allowlist отвергает подмену FQDN. PROD TRADE запрещён.
После выбора любого account scope неизменяем, включая disabled подключение
и состояние до фактов; обновления сериализуются row lock PostgreSQL.

## Проверки до запуска

Запустить офлайн preflight с отдельным `REMOTE_ENV_FILE` через
`deploy/remote/preflight.py`: он читает `docker compose config`, не запускает
контейнеры и не выводит resolved env. Текущая версия проверяет прежние отдельные project/volumes и требует изменения для общей БД; она проверяет
режим, strategy=false, cookie/origin и loopback UI; внутренние ports закрыты.

Перед запуском двух stacks отдельно измерить RAM/disk и резерв на startup,
каталог и peaks Worker. Замер02.10 (~786MiB available, existing containers
~605MiB) не доказывает запас при двух runtime; сначала пересчитать peak budget
и принять capacity gate. Миграции и restore репетировать на отдельной БД.

Шаблон `deploy/remote/nginx-production-ip.conf` добавляется рядом с Sandbox
конфигурацией:8443 использует тот же действующий TLS certificate path, upstream
только127.0.0.1:8081. Общая login rate zone определяется Sandbox template.
Перед запуском проверить `nginx -t`, срок/renewal сертификата и firewall. Сейчас конфигурация не установлена.

Backend доверяет configured Origin, включая порт, перед login/logout и всеми
изменяющими browser API. X-Forwarded-* не выбирает trusted origin. Cookie
Secure/HttpOnly/SameSite=Strict, Path=/, без Domain; наличие двух cookie и
CSRF не разрешает cross-origin запрос. Диагностические клиенты обязаны задавать
точный Origin своей среды. `/api/runtime` доступен только после авторизации.

## Приёмка чтения и rollback

После интеграционной проверки и capacity gate проверить DNS, TLS chain и
авторизованные PROD RPC из фактического SDK image. Открытый TCP443 не доказывает RPC; TLS verification
не отключать. Ошибка локального trust store требует выяснения цепочки и SDK
roots. SDK1.49.3 включает официальный российский root; реальный AsyncClient
с синтетическим неверным токеном получил UNAUTHENTICATED. Это подтверждает
TLS/gRPC, но не авторизованные RPC; evidence:
`develop/reports/prod-api-20261002/prod-sdk-network.json`.
До ввода владельцем реального read-only токена live чтения не проверены.

Выбрать точный account ID через защищённый UI; сверить accounts, cash/blocked
balances, portfolio, positions, active orders/stop orders с кабинетом владельца.
Bootstrap допускает только ledger delivery; broker mutations запрещены.
Фактическая outbox доставка проверяет credential-free BrokerScope и
broker/account/environment/cache, включая CLOSED automation и disabled broker.
Adoption counts и диагнозы UI не подтверждают фактический переход в HOLD.
Проверить одновременно две UI, independent logout/session revocation, readonly
кнопки и отказ manual/retry/recovery writes. Внешние исполнения сверять отдельно,
не создавать execution facts из snapshot.

При откате остановить только PROD project/Worker, сохранить его env и обе
базы для сверки. Sandbox project/env/volumes не менять. Не удалять durable
volumes и финансовые факты. Реальная торговля, lease/arming/лимиты/canary P3–P5
требуют отдельной реализации, проверки и допуска владельца.
