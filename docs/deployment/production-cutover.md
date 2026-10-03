# PROD: подготовка контура только для чтения (P1/P2)

Код P1/P2 реализован и прошёл независимое code review и изолированные проверки.
Первоначальный PROD prototype остановлен по требованию общей БД.
Объединение Core и initial READ_ONLY deployment выполнены 02.10; независимое
initial READ_ONLY operational acceptance — **PASS** (C) по final inventory/deployed
receipt16:57 UTC: TEST6+PROD2, одна Core PG/БД, короткие observations. Пользователь
разрешил поэтапный переход READ_ONLY; реальные торговые действия требуют
отдельного допуска.
`APPLICATION_ENVIRONMENT=PROD`, `BROKER_ACCESS_MODE=READ_ONLY` и
`STRATEGY_ENABLED=false` обязательны. PROD TRADE отвергается до появления
проверенного допуска P3. Текущая Sandbox среда сохраняет свой env и volumes.

## Статус объединения БД — 02.10

Владелец требует одну PostgreSQL/одну database. Первоначальный отдельный PROD
prototype PostgreSQL остановлен, его данные/volumes сохранены. Новый listener8443
обслуживает initial PROD на общей Core БД. Неизменяемый master `1b410a9` развёрнут
по commit и явной отмашке владельца: TEST443 — шесть сервисов, PROD8443 — два.
В текущем пакете добавлены shared-DB Compose/env/preflight и contour guards;
код и конфигурация прошли независимые reviews A/B/C и итоговую source integration.
Полный изолированный backend прогон: **1456 passed, 0 skipped**; frontend
**119 passed**, typecheck/build exit0. Shared-DB scope guards/collector/Compose
и dedup проверены. Paired restore fixtures на PG16 — **2 passed, 0 skipped**:
сохранение journals, lost ACK replay и отсутствие повторной отправки проверены
на тестовой паре. Рабочая парная backup/restore приёмка принята составным
доказательством: schema/данные/sequences совпали точно, 13 CHECK доказаны
эквивалентными через PostgreSQL parser roundtrip. Исходный literal FAILED receipt
сохранён. Старые 32308 decisions и identity/execution поля шести cycles/lots сохранены;
valuation поля обновлялись. Новые decisions
только WAIT при strategy=false. HTTP 41 checks PASS; публичный browser TLS/render
PASS. TEST SDK: каталог2460 RUB, selected=0, счёт1, брокерских позиций7;
Core контролирует6 отдельно. Повторная проверка16:55 UTC: SQLite integrity OK, FK violations=0,
frontier behind=0, Core prefix продвинулся, pending48 стабилен,
STARTED/RECONCILED160104 стабильны в Core/Worker; collector свежий, ошибок нет; настроен на оба контура; на момент этой проверки PROD RPC без токена
ещё не выполнялись.
Три RAM замера: cgroup735–787 MiB/working set518–609 MiB; swap вырос после
промежуточных restore репетиций. Длительная capacity/24ч приёмка не заявляется.
**Независимое initial READ_ONLY operational acceptance — PASS** (C), по final
inventory/deployed receipt16:57 UTC. Длительная и авторизованная PROD приёмка
остаются открытыми. В21:05 MSK02.10 принято исправление настроек: владелец ввёл
токен через UI, создано новое отдельное immutable подключение TINVEST PROD.
Scoped accounts/global summary: HTTP200, счёт1/errors0, точный selected API ID,
конечные RUB portfolio/free cash, runtime READ_ONLY. Старое уже отключённое
подключение и identity/каталог4335/sync1 сохранены. Positions/orders verification,
persistent portfolio/collector, bootstrap/capacity/admission ещё открыты;
PROD Worker/TRADE и repair не выполнялись. Диагностический bugfix шести файлов —
CODE PASS C/root (backend80/frontend3/typecheck/Ruff/Black), но не закоммичен и
не развёрнут. Active release остаётся `1b410a9`; новый rollout требует commit
владельца в master и отмашки. Исторические1456 тестов не подтверждают новый delta.
Локально также исправляется удаление broker settings: DELETE архивирует запись
и скрывает её из списка, сохраняя account scope и историю. Пакет требует
`0003_user_broker_archive` на общей БД; рабочая ревизия остаётся `0002`, новый
код пока не развёрнут. Перед rollout нужны обычная согласованная backup и
остановка writers; откат `0003` с архивными записями запрещён без явного
переноса archive metadata.
Worker storage отдельно проанализирован в
[аудите](../audits/orchestration-and-worker-storage.md); миграция не выбрана.

## Изоляция

Подготовить отдельный root-only env по `deploy/remote/.env.production.example`, явно
задать PROD значения из таблицы ниже, READ_ONLY и strategy=false;
не копировать токен Sandbox. Установить отдельный пароль оператора. Заполненный
файл хранить с правами0600; credentials не выводить в отчёты. PROD token
владелец уже ввёл через HTTPS UI; accounts/summary доступны, полный допуск
остальной PROD приёмки ещё не завершён;
Sandbox token/account/ledger в PROD не копируются.

| Ресурс | Sandbox | PROD |
| --- | --- | --- |
| Compose project | moex-sentinel-remote | moex-sentinel-prod |
| HTTPS origin | https://135.136.178.252 | https://135.136.178.252:8443 |
| UI loopback | 127.0.0.1:8080 | 127.0.0.1:8081 |
| Cookie | __Host-moex-session | __Host-moex-prod-session |
| PostgreSQL volume / database | Существующая общая PostgreSQL | Та же PostgreSQL/database; второй service не запускается |
| Worker volume | moex-sentinel-remote-automaton-data | moex-sentinel-prod-automaton-data |

Application сети создаются отдельно по Compose project. Общая внешняя
private data network — `moex-sentinel-data`; TEST database имеет alias
`sentinel-shared-postgres`. Оба Core используют одинаковые `POSTGRES_DB`,
credentials и `DATABASE_HOST=sentinel-shared-postgres`. В PROD overlay удалены
services `database`, `migrations`, `portfolio-snapshot-worker` и PG volume.
Миграциями владеет TEST; единственный TEST collector читает оба контура через
`PORTFOLIO_SNAPSHOT_ALL_ENVIRONMENTS=true`, установленный remote overlay.
Локальный default остаётся false. Старт второго collector запрещён.
Durable Worker volumes external;
существующие Sandbox volumes не переименовывать и не удалять. Analytics не
получает broker credentials или данные портфеля. Backend и его торговый Worker
должны иметь одинаковые contour/access mode. Collector остаётся внутри Core
boundary: его явное чтение обеих сред не даёт TEST API/Worker доступа к PROD scope.
Worker сверяет Core contour/mode до изменяющих HTTP-запросов и SDK session;
общий TEST/PROD target allowlist отвергает подмену FQDN. PROD TRADE запрещён.
После выбора любого account scope неизменяем, включая disabled подключение
и состояние до фактов; обновления сериализуются row lock PostgreSQL.

## Проверки до запуска

Проверить офлайн без запуска и вывода resolved env. Пути ниже — отдельные
root-only env, не shell source. `compose.production.sh` использует
`.env.production` по умолчанию и явно выбирает PROD overlay:

```sh
REMOTE_ENV_FILE=/secure/test.env sh deploy/remote/compose.sh config --quiet
REMOTE_ENV_FILE=/secure/prod.env sh deploy/remote/compose.production.sh config --quiet
REMOTE_CONTOUR=PROD REMOTE_ENV_FILE=/secure/prod.env TEST_REMOTE_ENV_FILE=/secure/test.env \
  PYTHONPATH=src python3 deploy/remote/preflight.py
```

PROD preflight требует TEST env для проверки владельца общей БД и collector;
проверяет shared alias/credentials, отсутствие второй PG/мигратора/collector,
READ_ONLY, strategy=false, cookie/origin, private network и loopback UI.
Он не доказывает существование network, работоспособность БД или live RPC.

Перед запуском двух stacks отдельно измерить RAM/disk и резерв на startup,
каталог и peaks Worker. Замер02.10 (~786MiB available, existing containers
~605MiB) не доказывает запас при двух runtime; сначала пересчитать peak budget
и принять capacity gate. Миграции и restore репетировать на отдельной БД.

После пользовательского commit и явной отмашки на deployment создать общую
network, если её ещё нет, с `docker network create --internal moex-sentinel-data`.
Существующая network должна быть проверена как private/internal, без её слепого
пересоздания. Сначала обновить TEST stack, сохранив PG volume/имя database;
TEST migrations выполняются один раз после согласованной резервной копии.
При нездоровом TEST owner PROD не запускать. Затем ограниченный initial PROD:

```sh
REMOTE_ENV_FILE=/secure/prod.env sh deploy/remote/compose.production.sh up -d --wait backend frontend
```

Не запускать PROD Worker/Analytics до следующего capacity/token/bootstrap gate.
Не выполнять PROD migration/database/collector команды. Перед обновлением и
изменением сетей writers останавливаются согласованно; общая БД и Worker volumes
сохраняются. Успешная конфигурация не разрешает restart Sandbox.

Согласованную backup снимает tracked `develop/scripts/paired_backup.py` после
остановки всех TEST/PROD Core writers, collector и Workers. Credentials PG
задать через защищённый `PGPASSFILE` или env, не аргументы. Helper сам не
останавливает процессы и не восстанавливает БД. Проверка пары включает весь
durable Worker state, outbox/intent/cycle и price repair journals; restore
и ACK replay выполняются только в изолированном тестовом окружении.
`pg_dump` и `pg_restore` должны совпадать по major с PostgreSQL server16;
client17 не является проверенным restore инструментом для этого пакета.
SKIP из-за несовпадения версии не подтверждает успешный restore.

Шаблон `deploy/remote/nginx-production-ip.conf` добавляется рядом с Sandbox
конфигурацией:8443 использует тот же действующий TLS certificate path, upstream
только127.0.0.1:8081. Общая login rate zone определяется Sandbox template.
Конфигурация установлена 02.10; `nginx -t`/reload и публичный TLS/render прошли.
Перед последующими изменениями повторить проверку срока/renewal сертификата и firewall.

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
Авторизованные accounts/summary проверены02.10; positions/orders и полный
portfolio/bootstrap допуск ещё не приняты.
Опциональный `PYTHONPATH=src python3 develop/scripts/prod_readonly_probe.py`
читает фиксированный root-only `/etc/moex-sentinel/prod-readonly-token`0600,
использует pinned PROD endpoint и выводит безопасные счётчики. Создание файла
и авторизованный запуск требуют отдельного решения владельца; отсутствующий
token не является пройденной проверкой. Проба не отправляет заявки.

Выбрать точный account ID через защищённый UI; сверить accounts, cash/blocked
balances, portfolio, positions, active orders/stop orders с кабинетом владельца.
Bootstrap допускает только ledger delivery; broker mutations запрещены.
Фактическая outbox доставка проверяет credential-free BrokerScope и
broker/account/environment/cache, включая CLOSED automation и disabled broker.
Adoption counts и диагнозы UI не подтверждают фактический переход в HOLD.
Проверить одновременно две UI, independent logout/session revocation, readonly
кнопки и отказ manual/retry/recovery writes. Внешние исполнения сверять отдельно,
не создавать execution facts из snapshot.

При откате приложения остановить только PROD project/Worker, сохранить env,
общую PG и Worker volumes для сверки. Не откатывать общую БД отдельно для PROD:
это затронет TEST и требует остановки всех writers, согласованной пары backup,
проверки совместимости и отдельного разрешения. Брокерские действия после backup
требуют reconciliation, а не автоматического restore старого состояния.
Sandbox project/env/volumes не менять без согласованного общего перехода. Не удалять durable
volumes и финансовые факты. Реальная торговля, lease/arming/лимиты/canary P3–P5
требуют отдельной реализации, проверки и допуска владельца.
