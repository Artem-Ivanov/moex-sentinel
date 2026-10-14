# PROD: изоляция, выкладка и допуск торговли

Текущий снимок10.10,14:55 МСК: PROD Core/Worker TRADE/strategy=true,
Analytics healthy; после штатной передачи владельца допуск STOP_REQUIRED/recovery_only,
отправка заявок закрыта. Основной `https://135.136.178.252/` ведёт в PROD,
listener8443 отключён; TEST приложения выведены из эксплуатации.
Прежнее разрешение на базу дня, сверку и ARM сохраняется для отдельной задачи
с актуальными gates; [результаты до rollout](#включение-торговли-prod--10102026)
ниже являются историей.

Код P1/P2 реализован и прошёл независимое code review и изолированные проверки.
Первоначальный PROD prototype остановлен по требованию общей БД.
Объединение Core и initial READ_ONLY deployment выполнены 02.10; независимое
initial READ_ONLY operational acceptance — **PASS** (C) по final inventory/deployed
receipt16:57 UTC: TEST6+PROD2, одна Core PG/БД, короткие observations. Пользователь
разрешил поэтапный переход READ_ONLY; реальные торговые действия требуют
отдельного допуска.
На начальном этапе обязательны были `APPLICATION_ENVIRONMENT=PROD`,
`BROKER_ACCESS_MODE=READ_ONLY` и `STRATEGY_ENABLED=false`.
Сейчас реальная отправка PROD заявок требует проверенного допуска P3 и ARM.
Sandbox среда сохраняет свой env и volumes.

## Релиз квоты и вывод TEST — 10.10.2026

Совместно обновлены PROD Core/Worker/UI до `vps-20261010-quota-987a495d6c71`,
применена0005 непосредственно к рабочей PostgreSQL. Health/HTTPS/auth/CSRF/assets,
позиции и новые поля настроек PASS; отдельный Chrome отрисовал форму входа на443.
Полная ручная проверка карточки после входа, реальные заявки, финансовый граф,
DB integrity и backup/restore в эту операцию не входили.

Owner `5e36c0ad-9722-4e03-9bb8-2a4cf1d1d3c5`, generation3/revision5,
STOP_REQUIRED/recovery_only, limits=null/effective_can_submit=false; lease свежая
на снимке14:55. База10.10 `3be41509-62f8-452a-a20d-87258b01ac24` сохранена.
Процент квоты пока NULL/settings_revision0; значение автоматически не задавалось.

TEST Worker/Core/frontend/Analytics stopped/restart=no, canonical TEST READ_ONLY/strategy=false.
Shared PostgreSQL и collector остаются в TEST Compose project и обслуживают PROD:
не делать whole-project down, не удалять volumes и не запускать TEST приложения повторно.
PROD Analytics сохранён прежним. PROD frontend127.0.0.1:8081, Nginx443→PROD;
8443 не слушает, внешний TLS/HTTPS8443 недоступен.
AUTH_ALLOWED_ORIGIN=https://135.136.178.252, cookie __Host-moex-prod-session;
security headers, ограничение входа и закрытые internal routes сохранены.

Original build/rollout FAIL и markers сохранены. Проверенные однократные
продолжения завершили только недостающие шаги; миграция/остановки не повторялись.
[Подготовленный откат](remote-compose.md#обновление-и-откат) не выполнялся.
Evidence: `develop/reports/quota-prod-rollout-20261010/` — `complete-rollout.json`,
`final-runtime-proof.json`, `external-browser-smoke.json`, `result.json`/`final-review.json`.
Последующие секции о начальном размещении — исторические снимки.

## Статус объединения БД — 02.10

### Актуальный release snapshot — 03.10.2026

По deployment receipt `develop/reports/vps-release-20261003/root-native-receipts.json`
(17:14 UTC): deploy commit `be84ed4f047734ccacfba9b6ccb531af51ba4b94`, release
`vps-20261003-be84ed4`, active path `/opt/moex-sentinel/releases/vps-20261003-be84ed4`
для TEST и PROD; applied schema `0003_user_broker_archive`. Finalize подтвердил 8 running
containers, image IDs, лимиты памяти и swap, mounts, networks и scope. TEST доступ READ_ONLY и
`STRATEGY_ENABLED=false`; PROD Core запущен READ_ONLY, PROD Worker остановлен; TRADE не
разрешён. Current runtime versions и отдельные HTTPS проверки записаны в
[снимке VPS](remote-compose.md#текущий-снимок-после-deploy-03102026). Эти сведения не
закрывают остальные операционные gates.

### Открытая позиция PROD: снимок 03.10, 18:00 UTC

Ограниченные HTTPS GET подтвердили READ_ONLY и один автомат HOLD/sequence0;
отдельный READ ONLY aggregate подтвердил HOLD/BOOTSTRAPPING, полный исходный
snapshot и отсутствие цикла. Это ожидание завершения первоначального учёта,
которое Worker обрабатывает без пользовательского Resume. Диагностика Worker
UNKNOWN/NOT_OBSERVED не подтверждает его готовность.

Локальный read contract дополнен `bootstrap_pending`; UI показывает причину
ожидания и неподтверждённые финансовые значения как «—». Этот fix ещё не
развёрнут и не завершает bootstrap. READ_ONLY/strategy=false сохранены;
возобновление торговли остаётся запрещённой в этом режиме командой. На момент
этого снимка PROD Worker для сверки ещё не был запущен; торговый допуск открыт.
Доказательства: `develop/reports/prod-resume-20261003/`.

### Оперативное завершение первоначальной сверки — в работе

Повторное требование пользователя исправить текущий импорт и прежний допуск
согласовать broker state разрешают адресно запустить один уже установленный
be84ed4 Worker READ_ONLY/strategy=false после capacity/token/bootstrap gates.
Независимый архитектор принял этот scope. Новые образы/локальные изменения,
Analytics, TRADE и TEST restart в операцию не входят. Для нового кода сохраняются
commit владельца и отдельное разрешение deployment.

Изолированная composed PROD проверка подтвердила bootstrap при Analytics outage
и lost ACK/restart/replay: scoped claim, один cycle/lot, четыре факта, отсутствие
orders/executions. Root fresh 1 PASS; независимый test/probe review C0/I0/M0.
Метаданные 18:41 UTC: Core/PG healthy, approved installed Worker совпадает TEST,
PROD volume существует, около 776MiB доступной RAM. После проверки пустого volume
его owner исправлен на UID/GID установленного Worker, содержимое сохранено.
Native preflight и start PASS около19:40 UTC; отдельная verify дала
BOOTSTRAP_NOT_COMPLETE и остановила только созданный PROD Worker. Fresh HTTPS
19:46 UTC: HOLD/sequence0 сохраняется, TEST6 IN_WORK, broker reads доступны.
Metadata 20:01 UTC: штатная остановка exit0, OOM=false/restart0, ошибки рабочего
цикла. Structured read-only counters 20:08 UTC: 40 `ValidationError` при
connection/commands HTTP200. Installed internal connection DTO теряет выбранный
account и environment/access mode; Worker не проходит validation до heartbeat.
Локальный fix трёх DTO-полей и real HTTP roundtrip PROD READ_ONLY/TEST
READ_ONLY/TRADE готовы: root fresh29 PASS, Ruff/diff PASS, независимый
prod_resume_architect source/contract review C/I/M0. Код NOT DEPLOYED.
Повторного запуска нет, восстановление пока НЕ принято. Новый код требует owner commit и
отдельного deployment GO; READ_ONLY/strategy=false и токен только для чтения
сохраняют запрет заявок. Доказательства:
`develop/reports/prod-bootstrap-20261003/`.

Абзацы ниже — историческое состояние на 02.10 и сохранены для аудита.

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
задать PROD значения из таблицы ниже. Начальный режим — READ_ONLY и
strategy=false; переход TRADE выполняется по разрешённой процедуре допуска,
как в снимке 10.10 ниже;
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
общий TEST/PROD target allowlist отвергает подмену FQDN. Отправка PROD заявок
запрещена до свежей успешной сверки и явного ARM. Режим TRADE сам по себе
не даёт разрешения на отправку.
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
и принять capacity gate. Применение миграций и их rollback проверять на отдельной БД.

Для обычного совместимого обновления каждая миграция имеет проверенный
rollback-скрипт; новый полный backup и полная репетиция восстановления БД
перед каждым deployment не требуются. Приложение откатывается на совместимый
образ с сохранением текущих данных; downgrade выполняется только в пределах
явно проверенных условий миграции. Резервное копирование и аварийное
восстановление ведутся отдельно. [Правило миграций](../development.md#правила-миграций).

Проверенная миграция применяется непосредственно к рабочей БД. После deployment
проверять health/ping сервисов, открытие интерфейса и отображение базового
функционала. Проверки целостности БД, сравнение всех данных и репетиция
восстановления не входят в обычную приёмку. При аварии использовать
предусмотренный безопасный откат.

После пользовательского commit и явной отмашки на deployment создать общую
network, если её ещё нет, с `docker network create --internal moex-sentinel-data`.
Существующая network должна быть проверена как private/internal, без её слепого
пересоздания. Сначала обновить TEST stack, сохранив PG volume/имя database;
TEST migrations выполняются один раз после проверки применения и rollback-скрипта.
При нездоровом TEST owner PROD не запускать. Затем ограниченный initial PROD:

```sh
REMOTE_ENV_FILE=/secure/prod.env sh deploy/remote/compose.production.sh up -d --wait backend frontend
```

Не запускать PROD Worker/Analytics до следующего capacity/token/bootstrap gate.
Не выполнять PROD migration/database/collector команды. Перед обновлением и
изменением сетей остановка writers согласовывается, если она нужна для миграции
или переключения; общая БД и Worker volumes
сохраняются. Успешная конфигурация не разрешает restart Sandbox.

Если отдельный сценарий восстановления требует согласованной backup, её
снимает tracked `develop/scripts/paired_backup.py` после
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

Расширенная приёмка ниже относится к первоначальному подключению PROD P1/P2
и отдельной подготовке реального торгового допуска. Она не является DoD
обычного совместимого deployment или текущего P3 READ_ONLY rollout: для них
действует базовая проверка health/ping и интерфейса, указанная выше.

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
общую PG и Worker volumes. Откат общей схемы затронет TEST: выполнять его
предусмотренным rollback-скриптом только при выполнении условий безопасности
миграции и согласованной остановке затрагиваемых writers. Брокерские действия после backup
требуют reconciliation, а не автоматического restore старого состояния.
Sandbox project/env/volumes не менять без согласованного общего перехода. Не удалять durable
volumes и финансовые факты. Реальная торговля, lease/arming/лимиты/canary P3–P5
требуют отдельной реализации, проверки и допуска владельца.


## Включение торговли PROD — 10.10.2026

Пользователь разрешил базу дня, сверку, режим TRADE и явный допуск для
существующей позиции GOLD: broker instrument UID
`8ab18c51-1f14-4c24-9c05-279bc6548821`, 18 единиц, lot size 1.
Внутренний ID каталога отличается от broker UID; для canary используется
broker UID. Параметры стратегии TEST и PROD совпадают: покупка 1 лота,
stop loss 5%, take profit 6%; параметры стратегии не менялись.

Утверждена формула лимитов допуска в RUB:

- Максимум заявки — свежие свободные деньги счёта, за вычетом blocked cash.
- Максимальная gross exposure GOLD — свежая стоимость счёта.
- Максимум одновременно активных заявок — число положительных торговых
  позиций, без денежных позиций.
- Максимальный дневной убыток — 5% принятой базы текущего московского дня.

Это отдельный дневной предел счёта, а не stop loss отдельной позиции.
Снимок 10.10: 5001,10 / 5052,94 RUB, одна торговая позиция,
дневной предел 252,647 RUB. Перед ARM значения вычисляются заново;
формула сама по себе не означает автоматическое изменение уже сохранённых
лимитов. ARM требует свежей успешной сверки и действующего владельца.

Исправлена ошибка risk adapter: валютная позиция RUB в GetPortfolio
не обязана присутствовать среди securities GetPositions. Проверки money,
blocked cash и количества реальных ценных бумаг сохранены. Совместимый
Core patch `vps-20261010-cashfix-b172ff4958eb` выложен без миграции,
backup или проверки целостности БД; health и базовые HTTPS функции прошли.
`production-current` указывает на patch, TEST `current` сохраняет P3 release.
База 10.10 принята в 11:48 МСК: equity 5052,94 RUB, внешний денежный поток 0.
Повторно заменять принятую базу этого дня не требуется.

Core и Worker запущены в TRADE/strategy=true; PROD Analytics healthy.
Первое выполнение
остановилось до owner-stopped: регистрация PENDING Worker появилась через
шесть секунд после проверки готовности контейнера. Продолжение завершило
передачу владения, без повторного deploy; исходный FAILED сохраняется.
Новый OWNER generation 2 подтверждён штатным API, lease обновляется.
Финальная canary сверка в 12:12 МСК выполнена: 18 единиц GOLD,
активных ordinary/stop orders и unresolved reservations — 0. Ответ HTTP 200,
но `status=DATA_UNKNOWN`, `can_arm=false`: результат сохранён в истории,
не выбран как доверенный anchor. Admission остаётся DISARMED/recovery_only,
effective_can_submit=false, финансовые limits ещё не установлены.
Свежий risk snapshot в 12:06 МСК сообщает `MARK_PRICE_STALE`: broker last price
за 09.10 23:47 МСК, limit/market orders недоступны на момент проверки.
Допуск ожидает свежих рыночных данных. Затем нужны база текущего московского
дня (существующую принятую базу дня переиспользовать), успешная canary сверка,
свежий расчёт утверждённых лимитов и явный ARM в срок действия сверки.
Повторного разрешения пользователя на эти шаги не требуется.
В 12:15 МСК десять сервисов running без OOM/рестартов; health, HTTPS UI/assets,
вход/CSRF/logout, runtime, positions/operations/automations прошли в TEST/PROD.
Проверки целостности БД и пробные реальные заявки не выполнялись.

Автор adapter/TRADE/owner helper — `capacity_contingency`, интеграция — root.
Независимый `resume_local_checks` принял adapter и OPS helpers C0/I0;
`rollout_ops_review` принял reconciliation helper и фактическую передачу
владения C0/I0/M0. Root: 54 связанных risk/admission теста, 10 TRADE tests,
3 owner continuation tests, 8 reconciliation selfchecks PASS; Ruff/Black/diff
PASS. Длительная приёмка и реальный боевой ARM остаются открытыми.
Доказательства: `develop/reports/prod-trading-enable-20261010/`
(`cashfix-result.json`, `baseline-after-cashfix.json`,
`trade-failure-current.json`, `trade-owner-completion.json`,
`final-reconciliation.json`, `final-health.json`, `risk-trade-diagnostic.json`,
`approved-limits.json`).

## P3: подготовка допуска торговли — снимок 09.10.2026

Выкладка P3 завершена 09.10 в 21:55 МСК: release
`vps-20261009-p3-41dd35003304`, общая схема `0004_trading_admission`.
Миграция применена непосредственно к рабочей БД; все девять целевых сервисов
работают без OOM и рестартов. Обе Core, PostgreSQL, Analytics и frontend
healthy; Workers и collector running. Старый отдельный PROD PostgreSQL
остаётся остановленным. Исходные shared PG/volumes, caps и контейнеры
Analytics/frontend сохранены; обе canonical ссылки указывают на новый release.
TEST TRADE возобновлён; PROD READ_ONLY/strategy=false/DISARMED,
effective_can_submit=false. HTTPS UI/assets, health, вход, runtime и базовые
positions/operations/automations прошли в обоих контурах.

Независимый `rollout_ops_review` принял окончательный helper/launcher и
фактические receipts/runtime: C0/I0/M0. Автор helper — `capacity_contingency`,
операционная интеграция — root; документную политику проверил
`resume_local_checks` без существенных замечаний. Root: 10 новых целевых тестов
PASS, Ruff/Black/diff PASS; ранее оба backend suite по 2230 PASS без пропусков.
Доказательства: `develop/reports/prod-admission-20261009/basic-deploy.json`,
`basic-migration.json`, `server-basic-runtime.json`, `basic-root-tests.log`.
Приёмка не утверждает полную целостность БД или длительную стабильность TRADE;
O2/P4/P5 и реальный торговый ARM остаются открытыми.

Актуальный scope после указания пользователя 09.10: непосредственная миграция
рабочей общей PostgreSQL до `0004_trading_admission`, запуск сервисов и проверка
health/ping, базового интерфейса и read-only страницы допуска `DISARMED`.
Полные проверки целостности, финансовых графов, новый backup и restore rehearsal
исключены из DoD этого rollout. Старые этапы ниже — история уже проведённых
операций, их продолжение не требуется. Сохранённые копии и volumes остаются.
Рабочая миграция, запуск и базовая приёмка выполнены; rollout **DONE**.

На 17:12 МСК QUIESCE завершён, все пять writers остановлены.
Пользователь отдельно разрешил краткий простой TEST portfolio collector.
Core exit 143 принят только со свежими тремя shutdown markers; Worker/collector
exit 0, обе Worker SQLite без active intents/unclean runs/WAL. Узкий dispatcher
переиспользует SHA-проверенные OPS/frozen helpers; независимый review C0/I0/M0,
root 71 OPS/wrapper PASS, Ruff/Black PASS. Оба полных backend прогона —
2230 PASS без пропусков. PG dump и обе Worker копии
сохранены, isolated PostgreSQL restore с полным графом 17 таблиц прошёл.
Legacy compatibility helper отклонил фактический shared DB hostname
`sentinel-shared-postgres`, ожидая `database`. Последующее продолжение остановилось
на ожидании `DATABASE_URL` в env-файле, хотя Compose формирует его из отдельных
переменных. Эта репетиция прекращена по новому scope; финального backup receipt
нет. PROD сохраняет READ_ONLY/strategy=false.

Во время ожидания выросли данные TEST и capacity gate перестал проходить.
Одна проверенная операция удалила 43 записи только приватного BuildKit cache,
не использовавшегося более 168 часов: exact ID selector, без `--all`, с
проверкой всех образов/tags, контейнеров, volumes, метаданных резервных копий и
хешей source/helper/env до и после. Технический бюджет копирования обновлён
с 23,5 до 24 млрд байт; исходный preflight сохранён отдельно byte-for-byte.
Миграция/restore рабочей БД не выполнялись; финансовые лимиты и окружение
сервисов не менялись. На 16:50 МСК
свободно 23 811 391 488 байт при потребности 23 672 153 502 байта: gate PASS,
но запас динамический и проверяется снова перед любой остановкой. Эта операция
не заменяет непосредственную миграцию, запуск и базовую проверку и не закрывает P3.
Локальные доказательства: `develop/reports/prod-admission-20261009/`
(`resume-ops-final-tests.log`, `server-resume-cache-receipt-and-runtime.json`).

Пользователь разрешил проектирование, реализацию и выкладку P3. Подробные
контракты и критерии приёмки: [проектирование](../superpowers/specs/2026-10-09-production-admission-design.md)
и [план реализации](../superpowers/plans/2026-10-09-production-admission.md).
Этот раздел описывает текущий план выкладки; факт выполнения подтверждается
отдельной записью с release tag и результатами проверок ниже.

Новый Core хранит исключительного владельца счёта, поколение Worker, lease,
явное вооружение допуска, финансовые лимиты и навсегда потреблённые ключи
заявок. Истечение lease не передаёт владение следующему процессу. После
контролируемой остановки прежнего владельца требуется отдельная передача,
сверка счёта и явное вооружение. Перезапуск Worker создаёт новый runtime UUID;
его состояние PENDING нельзя устранять удалением истории или сбросом таблиц.
Внутренние PROD API требуют scoped service token, runtime UUID и runtime secret;
обычный операторский session cookie не заменяет идентичность Worker.

Выкладка P3 09.10 сохраняла PROD `READ_ONLY`, `STRATEGY_ENABLED=false`,
`DISARMED`. Никаких ARM, реальных post/cancel или численных торговых defaults
при деплое не было. На тот момент пользователь ещё должен был задать
разрешённый instrument UID и
численные пределы notional, exposure, outstanding и дневного убытка.
Полный торговый день P4 и боевой canary P5 остаются открытыми. PROD Analytics
не запущен в исходной проверенной топологии; до включения стратегии отдельно
проверить эту зависимость и её ресурсные лимиты. P3 READ_ONLY не доказывает
готовность полного цикла стратегии на реальном счёте.

Общая PostgreSQL получает аддитивную миграцию
`0003_user_broker_archive` → `0004_trading_admission`. Поэтому вместе с PROD
Core/Worker обновляются TEST Core и portfolio collector. TEST Worker остаётся
на прежнем проверенном образе с Sandbox TRADE; работающие Analytics и frontend
сохраняют свои контейнеры. Оригинальные PostgreSQL/SQLite volumes, финансовая
история и пользовательские broker credentials сохраняются. Артефакт создаётся
из проверенного незакоммиченного working tree с SHA256 manifest; HEAD 62639a8
не является идентификатором нового P3 кода. Коммиты выполняет пользователь.

Первоначальный root-operated helper сохранён в локальном
`develop/reports/prod-admission-20261009/admission_ops.py` как доказательство
исторических операций. Его backup/fullgraph-фазы не используются в новом scope.
Непосредственный rollout выполняет `deploy_basic.py`, переиспользующий проверенные
операции запуска Compose и проверки исходников, образов, scope, mounts,
networks и ресурсных лимитов. Уже проведённые тесты миграции и rollback на
отдельной БД приняты. После миграции receipt подтверждает применённую ревизию;
проверка сервисов подтверждает health/ping и базовый интерфейс. Полная
целостность БД этим receipt не утверждается.

После миграции откат выполняется только заменой кода на заранее проверенные
старые Core/collector образы с head 0004. Восстановление рабочей БД из старой
копии и downgrade запрещены после новых операций TEST. Такой fallback
восстанавливает ограниченный READ_ONLY runtime, но старый Core не предоставляет
P3 admission API: результат явно `NOT_AVAILABLE`, `p3_ready=false`. Это не
успешная выкладка P3. Перед запуском fallback остановить затронутые сервисы
и сохранить исходные volumes; после запуска выполнить базовую проверку.

Новый release устанавливает root-only operations anchor, хешированный marker
и отдельные role/capacity/image overlays. Обе canonical ссылки указывают на
новый release, в том числе при code-only fallback. Штатные Compose wrappers
проверяют marker и точные образы; потеря или подмена marker закрывает запуск.
Для штатных команд применять canonical wrapper с соответствующим env и
действующим operations mode, а не прежний raw image tag из env.
