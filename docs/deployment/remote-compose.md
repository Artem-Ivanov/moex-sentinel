# Удалённый Linux Docker Compose

## Границы готовности

Исторический снимок на 2026-10-02: приложение развёрнуто на VPS `135.136.178.252` (Ubuntu 24.04, amd64, Docker 29.7.2, Compose v5.4.0). Замеры того снимка: RAM1,92GiB, диск55,99GiB/available47,45GiB. Локальная история не переносилась; состояние Sandbox было загружено штатным API. Повторный SSH/sudo успешен; running `STRATEGY_ENABLED=false`, broker/Core/Worker ledger и доставка выборки сверены. Каталог/позиции/выход и session401 в живом браузере подтверждены; restart без дублей оставался открыт. Evidence: `develop/reports/vps-audit-20261002/`. Источники скопированы из проверенного рабочего дерева в root-owned релиз; локальный код ещё не был закоммичен. Ниже также приведена общая процедура для будущих релизов. Этот документ не обещает SLA.

База и внутренние API не имеют опубликованных портов. Текущий PROD UI слушает только `127.0.0.1:8081`; Nginx публикует его через `https://135.136.178.252/`. Backend требует учётные данные оператора; HTTP cookie для loopback на удалённом Compose выключена. Не открывать Docker API наружу.

На текущем VPS созданы новые внешние volumes PostgreSQL и Worker SQLite. Worker остаётся **один на брокерский scope во всех хостах вместе**; replica=1 не предотвращает второй процесс на другом сервере. Профиль `trading` отделяет его запуск от проверки UI. Worker имеет 120 секунд на SIGTERM, остальные сервисы — 90 секунд; Docker-логи ротируются. Worker помечает чистое завершение после закрытия runtime; код выхода/логи проверяются отдельно.

### Публикация по IP

На текущем VPS сертификат Let's Encrypt содержит IP в SAN и автоматически продлевается Certbot. Nginx проксирует только PROD loopback frontend (`127.0.0.1:8081`), закрывает `/internal/*` и ограничивает `/api/auth/login` по IP клиента: 10 запросов в минуту с burst до 5. Внешние проверки подтвердили HTTPS, отказ анонимному API, вход, защищённую cookie, CSRF, выход, отзыв сессии и HTTP 429 после превышения лимита.

Overlay использует [правила merge Docker Compose](https://docs.docker.com/reference/compose-file/merge/), включая `!override` (требует 2.24.4). [Профиль](https://docs.docker.com/compose/how-tos/profiles/) исключает Worker из обычного `up`, но явное указание имени сервиса запускает его даже без `--profile`.

## Развёрнутый VPS

### Актуальный PROD443 и выведенный TEST — 10.10.2026

Около14:54 МСК завершён выпуск `vps-20261010-quota-987a495d6c71`:
новые PROD Core/Worker/frontend, схема0005. Основной HTTPS443 теперь PROD8081;
8443 listener отсутствует, внешний TLS/HTTPS8443 недоступен.
AUTH_ALLOWED_ORIGIN=https://135.136.178.252 и __Host-moex-prod-session сохранены
в строгой конфигурации. Health/HTTPS/auth/CSRF/assets/позиции/новые поля настроек PASS.
Chrome в отдельном временном профиле отрисовал вход; карточка после входа вручную не проверялась.

На снимке14:55 работают шесть сервисов: PROD Core/Worker/frontend/Analytics и
общие PostgreSQL/collector. TEST Worker/Core/frontend/Analytics stopped/restart=no,
canonical TEST READ_ONLY/strategy=false. TEST volumes/history/outbox сохраняются.
Общие DB/collector принадлежат moex-sentinel-remote, но обслуживают PROD —
не выполнять TEST whole-project down и не запускать его приложения.
Исходные mounts/networks/caps/loopback bindings сохранены, OOM/restarts0.
В текущем PROD wrapper задавать явно REMOTE_CONTOUR=PROD и
REMOTE_ENV_FILE=/etc/moex-sentinel/production.env, путь /opt/moex-sentinel/production-current.

Штатная передача владельца: generation3/revision5, STOP_REQUIRED/recovery_only,
limits=null/effective_can_submit=false, свежая lease. ARM/reconcile не выполнялись;
база московского дня10.10 сохранена. Процент квоты пока NULL/settings_revision0.
Health не подтверждает реальную торговлю или свежесть рынка.

Original build/rollout FAIL и markers сохранены. Однократные продолжения завершили
только оставшиеся шаги без повторного применения0005. Рабочая БД не копировалась
и не восстанавливалась; integrity/финансовый граф не проверялись. Rollback подготовлен,
не запускался. Root evidence и independent rollout_ops_review:
`develop/reports/quota-prod-rollout-20261010/` — `complete-rollout.json`,
`final-runtime-proof.json`, `external-browser-smoke.json`, `result.json`/`final-review.json`.
Ниже — исторические снимки.

### Включение TEST/Sandbox торговли 08.10.2026

После отдельного явного разрешения владельца в TEST включены
`BROKER_ACCESS_MODE=TRADE` и `STRATEGY_ENABLED=true`. В canonical TEST env
изменены только эти два значения. Release `vps-20261008-62639a8`, images,
ограничения ресурсов, volumes, сети и shared PostgreSQL сохранены. PROD
остаётся `READ_ONLY`, `STRATEGY_ENABLED=false`; его env и container IDs
не менялись. Endpoint TEST подтверждён как `sandbox-invest-public-api.tbank.ru:443`.

Candidate прошёл preflight до изменения runtime. TEST Worker завершён чисто
с нулём активных intents, затем остановлены TEST Core и общий collector.
После изменения env сначала запущены Core/collector, затем единственный
TEST Worker на VPS. Краткая остановка общего collector задержала чтение
портфелей обоих контуров; PROD Core/Worker продолжали работать.

Worker стартовал в 17:42:09 UTC (20:42:09 МСК). Все 12 последних проверенных
решений получены после старта, `STRATEGY_DISABLED` отсутствует; есть
`SELL_PART/LIFO_LOT_TAKE_PROFIT`. Финальный снимок 17:44:13 UTC подтвердил
5 TEST orders `FILLED`, 2 `CANCELLED` и 5 executions в Core; последняя
execution — 17:42:52 UTC. В истории брокера появились новые TEST операции,
последняя в проверенной выборке — 17:42:51 UTC. PROD orders/executions=0.

HTTPS/auth/CSRF/assets/health и свежий `CONTROL_PROGRESS/COMPLETED` прошли
в обоих контурах. 9 контейнеров работают без OOM и перезапусков. Delivery
за 7,5 секунды подтвердила ACK границы Worker и рост Core/Worker sequences
всех 6 TEST automation; FAILED нет. Captured cohort пуст, поэтому конкретные
UUID событий этой проверкой не сверялись; 9 новых pending выше cutoff допустимы.
Общий diagnostics остаётся `UNKNOWN`; открытые S1/S2/B1 и полный benchmark
этим включением не закрыты. Singleton проверен на VPS, другие хосты не проверялись.

Рабочие данные не восстанавливались. Резервная копия deploy ниже относится
к состоянию до новых Sandbox сделок. После появления заявок возврат в
READ_ONLY требует чистой остановки Worker и сверки intents/брокера;
восстановление прежней рабочей БД не является способом отката торговли.
Evidence: `develop/reports/test-vps-deploy-20261008/` —
`server-test-trade-preflight-final.json`, `server-test-trade-stop.json`,
`server-test-trade-core-start.json`, `server-test-trade-worker-start.json`,
`server-test-trade-fresh-decisions.json`, `server-public-after-test-trade.json`,
`server-test-trade-delivery.json`, `server-test-trade-final-runtime.json`
и `test-trade-acceptance.json`.

Независимый `recent_completeness_audit` принял фактические receipts и этот
раздел: C0/I0/M0. Ревью выполнено без SSH по безопасным результатам root-проверок;
приватный VPS baseline рецензент не читал. Результат:
`test-trade-final-review.json` в том же каталоге evidence.

### Применение сохранённого PROD токена 08.10.2026

По запросу владельца на боевой запуск проверено актуальное PROD подключение:
запись обновлена в 21:06:47 МСК, а Worker был запущен до изменения. В
21:14:19 МСК чисто перезапущен тот же PROD Worker в `READ_ONLY` с
`STRATEGY_ENABLED=false`. Перед стартом подтверждены exit0, отсутствие активных
intents, чистое завершение SQLite и отсутствие незавершённых WAL/journal.
Container ID, полное окружение, image, caps и volume сохранены; остальные
контейнеры и canonical env не менялись. TEST продолжает торговлю без перезапуска.

Сохранённые credentials проверены прямыми безопасными RPC `GetAccounts`,
`GetPortfolio`, `GetPositions`, `GetOrders`, `GetStopOrders`: выбранный счёт
доступен, позиции читаются, blocked balances и активные обычные/стоп-заявки
отсутствуют. `GetAccounts` вернул для счёта `FULL_ACCESS`; это не приёмка
торгового допуска или проверка прав реальной мутацией. Значение токена агент
не менял и не выводил; `updated_at` подтверждает изменение записи, а не
сравнение старого и нового токена. Настройки стабильны до и после restart/RPC.

Проверки 21:15–21:17 МСК подтвердили свежий `CONTROL_PROGRESS/COMPLETED`,
health/auth и нулевые PROD app orders/executions. Боевой алгоритм не запускался:
семь действующих guards запрещают PROD+TRADE. Независимая source-проверка
подтвердила отсутствие P3 admission/lease, численных ограничений и live
arming/revocation перед post/retry. Для ограниченного запуска нужны реализация
P3, принятая свежая P4 сверка и P5 с конкретным UID/бюджетом владельца.
Запрошены UID, max order notional, exposure, outstanding orders и daily loss;
значения не подставлялись автоматически. Общий diagnostics остаётся `UNKNOWN`.

Evidence в `develop/reports/test-vps-deploy-20261008/`:
`server-prod-token-metadata-before.json`, `server-prod-token-worker-restart.json`,
`server-prod-token-safe-rpc-retry.json`, `server-prod-launch-public-after.json`,
`server-prod-token-final-financial-scope.json`, `prod-token-acceptance.json`.
Первый RPC probe отказал до запуска из-за типа stdin диагностической команды;
исправлен только транспорт, первоначальный failure сохранён отдельно.
Независимый `recent_completeness_audit` принял применение credentials и этот
раздел: C0/I0/M0; evidence `prod-token-final-review.json`. Это ревью локальных
receipts без SSH и новых тестов, с сохранением env/config по безопасным
root assertions. Боевой допуск этим ревью не принят.

### Исторический снимок после deploy 08.10.2026 — до включения торговли

По явному разрешению владельца обновлены TEST и PROD до commit
`62639a8655cf439c1b1eea6a83a14ae1f14dd33b`, release `vps-20261008-62639a8`.
Финальная проверка 16:32:03 UTC подтвердила обе current links, замену только
`RELEASE_TAG` в root-only canonical env и точные новые Python image IDs.
Frontend использует прежний image: его входы не менялись. Нативные source
hashes, imports из `/app/src`, UID, зависимости и конфигурация образов проверены;
448 source/test файлов совпадают с ранее проверенным snapshot (1991 backend PASS).

9 контейнеров работают без OOM и перезапусков. Shared PostgreSQL сохранила ID,
volume и schema `0003_user_broker_archive`; default transaction isolation —
`read committed`. Legacy PROD database остановлена. Worker volumes, scopes,
порты, сети, UID и прежние ограничения ресурсов сохранены; миграций нет.
Оба контура `READ_ONLY`, `STRATEGY_ENABLED=false`, orders/executions/intents=0.
Collector после обновления записал свежие TEST/PROD snapshots без ошибок.

HTTPS, index/JS/CSS, health/schema, вход, защищённые cookies, CSRF, выход и
отзыв сессии прошли в обоих контурах. Worker показывает свежий
`CONTROL_PROGRESS/COMPLETED` после запуска. Общий diagnostics status остаётся
`UNKNOWN`: это не подтверждение полной готовности всех зависимостей.
Проверка доставки за 11,3 секунды подтвердила Core ACK зафиксированной границы
Worker и рост последовательностей всех 6 TEST automation; PROD достиг своей
границы. Captured outbox cohort был пуст в обоих контурах, поэтому конкретные
UUID событий этой проверкой не сверялись. 32 новых pending выше cutoff
допустимы; FAILED отсутствует. Это проверка границы, а не всей истории доставки.

Согласованная резервная копия общей PG и обеих Worker SQLite проверена
в изолированных хранилищах. Private backup:
`/root/moex-sentinel-backups/vps-20261008-62639a8`; manifest SHA256
`f07295db83a1e40d0033f21825b8bc70e0e71c5113415e4cb894609ddc830efe`.
PG dump 487875983 bytes; schema, scopes, bootstrap graph и outbox обеих
сохранённых Worker копий согласованы. Restore clone остановлен; рабочие данные
не восстанавливались. Backup относится к первой остановке; более поздние
READ_ONLY журналы остались в рабочих volumes. Старые images/env сохранены.
Первый restore отказал из-за readiness временного initserver, следующая
проверка — из-за синтаксиса paired SQL. Старые сервисы возобновлялись на время
проверки; исправленный запрос проверен на уже восстановленной isolated PG.
Первый delivery probe не завершился за 90 секунд; отдельная проверка по
sequences прошла. Failed receipts сохранены, original helpers
не переписаны. Возобновление collector сбросило его исторический restart
counter 1→0; новый baseline зафиксировал фактический 0.

Отсутствие новых TEST сделок объясняется READ_ONLY и выключенной стратегией:
Worker делает `WAIT/STRATEGY_DISABLED`, а UI показывает историю операций брокера.
Последняя TEST операция подтверждена: 29.09.2026 14:03:53.398 UTC, read errors=0.
Deploy не включает торговлю. Открытые S1/S2/B1 из
[аудита](../audits/service-isolation-completeness-20261008.md) остаются;
shared data network `Internal=true` не обеспечивает полной изоляции сервисов.
Evidence: `develop/reports/test-vps-deploy-20261008/`, прежде всего
`server-installed-final.json`, `server-runtime-both-new.json`,
`server-public-both-new.json`, `server-delivery-fast-both-new.json` и
`server-verified-backup.json`. Browser click-through, TRADE и полный benchmark
в этот deploy не входили.

### Смена PROD токена и готовность к торговле — проверка кода 08.10.2026

Сохранённое подключение редактируется в PROD UI: `/brokers`, двойной клик по
строке, замена поля `token`, «Сохранить», затем «Проверить». PUT настроек
разрешён в READ_ONLY после авторизации, с Origin и CSRF. Для подключения с уже
выбранным счётом account/API/environment/FQDN закреплены; смена токена не
создаёт новый scope и не требует удаления подключения или истории.

Core читает новый токен из сохранённых settings; market gateway пересоздаёт
источник при изменении подключения, collector читает settings в каждом цикле.
Работающий streaming Worker сохраняет SDK session со старым токеном и не
пересоздаёт её только из-за PUT. После смены токена нужен контролируемый
перезапуск PROD Worker с сохранением SQLite volume; мгновенный hot reload не
реализован. «Проверить» читает доступные счета и наличие выбранного account,
не отправляет пробную заявку и не доказывает торговые права токена.

Торговый режим и общие настройки стратегии читаются из окружения при старте,
через форму брокера не меняются. В release `62639a8` PROD+TRADE отвергают Core,
Worker, order adapter и deploy preflight: замены токена и env недостаточно.
Допуск, лимиты, контролируемое владение account и аварийное отключение из
[P3–P5](../superpowers/plans/production-api.md#p3-допуск-торговли-и-восстановление)
требуют реализации и проверки до старта реальных сделок. Эта проверка кода не
меняла VPS, токен или READ_ONLY и не запускала торговые действия.

### Исторический снимок после deploy 04.10.2026

По разрешению владельца установлен commit
`f29472f405542ba172af4401f81d192246e04f43`, версия сервисов `0.2.0`, release
`vps-20261004-f29472f`. Обе ссылки current указывают на новый release.
Delivery завершена в 12:42:22 UTC: 9 running контейнеров, включая Worker TEST
и PROD. Shared PostgreSQL сохранила ID, volume и schema
`0003_user_broker_archive`; legacy PROD database остаётся stopped.
Миграция в этом выпуске отсутствует. Существующие Worker SQLite volumes,
ограничения ресурсов, scopes и порты сохранены. В canonical env заменён только
RELEASE_TAG. Оба контура READ_ONLY, `STRATEGY_ENABLED=false`; TRADE не включён.

Committed source и свежий frontend dist проверены до остановки. Новые Linux
образы построены из прежних образов соответствующих сервисов с проверкой
неизменности dependency manifests и полной заменой application source;
фактические imports/source hashes/UID и frontend assets проверены нативно.
Это не закрывает полную веху M1 по образам, памяти и production latency.

HTTPS с проверкой сертификата и exact index/CSS/JS hashes прошёл в обоих
контурах. Авторизованные accounts/portfolio/automation reads, вход/выход и
изоляция cookie прошли. Непустой каталог проверен через существующий фильтр
FUND/RUB: по 82 элемента в TEST и PROD; это размер выборки, не всего каталога.
В PROD незавершённый bootstrap завершился: sequence6, quartet4,
bootstrap_pending=false; количество, средняя цена и валюта согласованы между
Broker/Core/Worker, orders/executions/intents/allocations0.

Первая отдельная strict postverification сохранена как FAILED: TEST очередь
PENDING8 в первом снимке и CLEAR0 во втором, failed0 и control progress есть;
текущие Core/Worker sequences читались неатомарно. Это не доказывает потерю
доставки и не служит разрешением recovery. Отдельный single read-only followup
13:07:57–13:08:09 UTC прошёл: для всех6 TEST automation Core sequence достигла
зафиксированного Worker high-water, старый outbox prefix отсутствует, финансовые/
scope/no-orders проверки сохранены. На момент capture pending cohort пуст;
точные UUID/kinds первоначальных8 событий этой проверкой не доказаны.
Поздние8 pending выше cutoff учтены отдельно, FAILED0. Это подтверждение
зафиксированной границы доставки, не постоянно пустой очереди и не всей истории.
Исходный strict FAILED не переписан. HTTPS/API/read-only bootstrap и указанная
граница доставки проверены; browser click и длительное наблюдение не выполнялись.
PROD read-only bootstrap подтверждён двумя наблюдениями, торговый допуск P3–P5
остаётся OPEN. Итоговое независимое runtime/doc review фиксируется отдельно.

Первый quiesce остановился на Analytics exit143: installed Uvicorn0.54.0
после graceful shutdown повторно поднимает SIGTERM. Исправлен только
operational gate:143 допускается для Core/Analytics при подтверждённых маркерах
текущей остановки, прочие роли требуют0. Адресное продолжение прошло независимое
review и23 offline checks; БД и journals не исправлялись. Первый сбой сохранён.
Evidence: `develop/reports/deploy-20261004/`; exact server delivery receipt SHA
`f588364af1211e0b080e176ef5ba75e65607ebf9a3aed1d2ac6eb4af631399fa`.
Итоговый native снимок:9 running/no OOM/no restarts, exact IDs/images/scopes/
mounts/caps/env hashes/links; RAM available693MiB, disk free43.7GiB. Это текущий
снимок, не benchmark экономии памяти.

### Исторический снимок после deploy 03.10.2026

По `develop/reports/vps-release-20261003/root-native-receipts.json` (17:14 UTC):
commit `be84ed4f047734ccacfba9b6ccb531af51ba4b94`, release
`vps-20261003-be84ed4`; `/opt/moex-sentinel/current` и
`/opt/moex-sentinel/production-current` указывают на
`/opt/moex-sentinel/releases/vps-20261003-be84ed4`. Schema `0003_user_broker_archive`;
8 containers running: TEST — Core, database, frontend, Analytics, portfolio collector,
Worker; PROD — Core и frontend. TEST Core/Worker/Analytics сообщают `0.2.0`; PROD Core
сообщает `0.2.0`, PROD Worker — UNKNOWN, PROD Analytics — NOT_CONFIGURED. Наличие версии
или UNKNOWN не подтверждает торговую готовность. TEST и PROD доступны только в
`READ_ONLY`, `STRATEGY_ENABLED=false`; PROD Worker не запущен и TRADE не разрешён.

Отдельная post-deploy HTTPS verification прошла: в обоих контурах index/assets, health,
diagnostics/auth, brokers/accounts/catalog, portfolio и trading GET получили ожидаемые
статусы; login/logout и изоляция сессий прошли. Accounts: по 1, errors 0; catalog GET
вернул `items=2460` TEST и `items=2462` PROD (размер ответов, не полный размер каталога);
portfolio/trading проверки PASS. HTTP receipt не включает browser acceptance или archive
действия; отдельная post-HTTP проверка `root-archive-timing.json` показывает 3 broker records,
1 archived и 0 archived active, последний `archived_at` — 17:13:34 UTC. Actor неизвестен;
эта запись не приписывается rollout/HTTP проверке, root не выполнял DELETE/PUT. Снимки
получены разными read запросами, не одной транзакцией. Это не browser acceptance или
длительное наблюдение. TEST/PROD env
изменялись только по release tag; volumes сохранены. TEST dump перед миграцией: 116246851
bytes, SHA и structural list проверены, owner-only; paired Worker backup/restore не выполнялся.

### Исторические operational сведения на 02.10.2026

- UI: `https://135.136.178.252/`; оператор: `operator`. Пароль создан случайно на VPS и хранится в `/root/moex-sentinel-operator-password` (root-only, 600). Владелец сервера считывает его через свой административный SSH-доступ командой `sudo cat /root/moex-sentinel-operator-password`, сохраняет в менеджере паролей и удаляет временный файл `sudo rm /root/moex-sentinel-operator-password`. Не пересылать пароль в чат или Git.
- Исторический снимок 02.10: исходники `/opt/moex-sentinel/current` → `/opt/moex-sentinel/releases/vps-20261001-sandbox-ui-r2`; Compose env: `/etc/moex-sentinel/remote.env` (root-only, 600). Старый r1 и env backup сохранены; тогда обновлён только Frontend, остальные сервисные image IDs не изменены. Отключение временного пользователя `codex-deploy` не удаляет эти файлы и Docker volumes.
- Запущены PostgreSQL, Backend, Analytics, portfolio-snapshot-worker, Frontend и `trading-automaton`. Broker token и единственный активный Sandbox account настроены; синхронизация добавила4332 инструмента, RUB выборка2460. API/браузер показывают6 IN_WORK позиций, qty/avg совпадают с broker и Worker; snapshot свежий. Running strategy=false, Worker intents0, Core orders/executions0, broker active обычных/stop orders0. Доставка 12 конкретных pending событий принята Core и ACK удалил их из Worker; свежие события продолжают поступать. Поддерживается только T-Invest TEST Sandbox; новую торговлю не включать по результату одного health.
- Nginx: `/etc/nginx/conf.d/moex-sentinel.conf`, исходник `deploy/remote/nginx-ip.conf`. 80/tcp обслуживает ACME и переводит на HTTPS; 443/tcp обслуживает приложение. Let's Encrypt IP-сертификат действует около 160 часов, поэтому нужны работающий `snap.certbot.renew.timer` и hook `/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh`. Проверка: `sudo /snap/bin/certbot renew --dry-run --run-deploy-hooks --no-random-sleep-on-renew` и `sudo nginx -t`. Контролировать срок сертификата отдельно: HTTP `/api/health` его не проверяет.
- Запуск Compose с серверным env: `sudo env REMOTE_ENV_FILE=/etc/moex-sentinel/remote.env sh /opt/moex-sentinel/current/deploy/remote/compose.sh ps --all`. Не выводить обычный `compose config` в терминал или журнал — он содержит пароль БД.
- С VPS проверены TLS/HTTP2/gRPC и авторизованные Sandbox чтения: accounts/portfolio/positions/catalog. Сборка SDK использует проверенную CA-цепочку только во время pip install; её происхождение записано в `docker/certs/README.md`.

Пароль оператора можно сменить: сгенерировать новый verifier через `python -m moex_sentinel.api.auth hash-password`, заменить только `AUTH_PASSWORD_HASH` в root-only env и пересоздать Backend. Существующие сессии после пересоздания Backend исчезнут. Перед обновлением БД или запуском торговли требуется согласованная резервная копия PostgreSQL и Worker SQLite по процедуре ниже; оба volumes уже используются. До первого bootstrap создан root-only PG dump; его restore и согласованная копия текущей пары пока не проверены.

Для пустого каталога сначала настроить token, проверить подключение, выбрать
активный Sandbox account и сохранить broker ACTIVE, затем синхронизировать
инструменты. Пока активного подключения нет, UI r2 показывает переход к
настройкам. Данные открытых позиций доходят до qty/avg UI после Worker bootstrap.
Новый PROD план, наблюдаемость и сроки хранения описаны отдельно:
[переход](../superpowers/plans/production-api.md),
[диагностика](observability-plan.md), [ёмкость](storage-capacity.md).

## Общая БД TEST/PROD: пакет перехода

В текущем пакете TEST владеет одной PostgreSQL/database и миграциями.
`compose.production.sh` добавляет PROD overlay и использует отдельный
`.env.production`: второй database service/PG volume, migrations и collector
в нём удалены. Оба Core подключаются через alias `sentinel-shared-postgres`
к общей внешней private network `moex-sentinel-data`; application сети,
root-only env и браузерные sessions остаются отдельными. Единственный TEST
collector remote overlay имеет `PORTFOLIO_SNAPSHOT_ALL_ENVIRONMENTS=true`.
Worker сохраняет собственный SQLite intent/outbox; перенос журнала не выбран.

Конфигурация и source guards проходят независимую проверку; это не live PASS.
Пользователь сам коммитит после gates. Любые приведённые ниже запуск/stop/restore
команды выполняются только после его явной отмашки на deployment.
До первой PROD записи обновить TEST backend и collector, принять capacity и
согласованный backup/restore. Общую network создать с `--internal` только после
разрешения, проверить существующую network без её удаления. Существующие TEST
database/credentials/volume сохранить; не заменять их примерными значениями.
Первый PROD запускает только backend/frontend, без Worker/Analytics/collector.
Точные wrappers/preflight и открытые token gates — в
[PROD runbook](production-cutover.md).

Все инструкции backup/restore ниже относятся к общей PG и durable Worker
хранилищам вместе. Если PROD уже развёрнут, остановить также его Core и Worker;
остановки только TEST writers недостаточно. Helper `develop/scripts/paired_backup.py`
принимает повторяемый `--worker`, требует `--writers-quiesced` и приватный новый
`--output`; сам процессы не останавливает. Защищённые PG credentials передавать
через PGPASSFILE/env, не argv. Restore — только в новые изолированные test volumes,
без запуска торгового Worker. Price repair journals и pending typed facts/ACK
проверяются в rehearsal, не удаляются как cache. Использовать `pg_dump` и
`pg_restore` major16, совпадающий с server; SKIP при другом major не является PASS.

### Файлы конфигурации

- `deploy/remote/compose.remote.yml`: overlay, закрытые порты, теги образов, singleton Worker.
- `deploy/remote/.env.example` и `.env.production.example`: TEST/PROD профили; PROD пример включает baseline-only daily preparation при пустом canary UID.
- [Каталог ENV](../configuration/environment.md): все сервисные параметры, alias, defaults, secret/restart правила и fixed invariants.
- `deploy/remote/compose.sh`: единый wrapper с явными compose/env путями; запускать через `sh`.
- `deploy/remote/compose.production.sh` и `compose.production.yml`: явный PROD wrapper и overlay общей БД.
- `deploy/remote/preflight.py`: read-only проверка разрешённой конфигурации; не выводит её секреты.
- `deploy/remote/nginx-ip.conf`: HTTPS reverse proxy для текущего IP и rate limit входа.
- `deploy/remote/reload-nginx.sh`: hook Certbot после успешного продления сертификата.

Команды ниже выполнять из корня выбранного checkout. `REMOTE_ENV_FILE` может указать другой абсолютный путь к env. Не использовать текущий локальный `.env` для случайного запуска второго Worker.
Заполняемый remote env держать вне Git с режимом `0600`; обычный вывод `compose config` содержит DB и service credentials.

## Подготовка релиза

Подготовить сборку и упаковку до остановки сервисов; результаты разработки повторно
проверять в cutover не требуется. Во время короткого VPS cutover выполнять необходимые
dump/migration и минимальные Core readiness gates до последовательного запуска Worker;
любой gate failure останавливает процедуру. Расширенные HTTPS auth/API/status проверки
выполнить отдельно после переключения, затем провести независимый review по receipts.
Для выпуска от 03.10 packaging gates выявили directories mode `0700` и public nginx config
mode `0600`; включать явную проверку доступности файлов сервисными UID в будущие preflight
изменения отдельным планом.

### SSH и клонирование

До клонирования изменения приложения и файлы `deploy/remote/` должны быть закоммичены и отправлены в Git: локальные незакоммиченные файлы на сервер не попадут. Выбрать проверенный commit. Значения `USER`, `HOST`, `REPOSITORY_URL` и `REVIEWED_COMMIT` ниже заменить своими; адрес Git может использовать SSH или HTTPS независимо от способа доступа к серверу.

С рабочей станции:

```sh
ssh USER@HOST
```

Далее на сервере с установленными Git, Python 3 и Docker Compose:

```sh
git clone REPOSITORY_URL moex-sentinel
cd moex-sentinel
git checkout --detach REVIEWED_COMMIT
git rev-parse HEAD
test -f deploy/remote/compose.remote.yml
```

Сверить выведенный commit с выбранным релизом. Все следующие команды выполнять в этом checkout на сервере, кроме проверки HTTPS с рабочей станции. Клонирование переносит исходники; существующие БД и локальная конфигурация переносятся отдельно по разделу ниже.

### Конфигурация и сборка вручную

1. Завершить recovery-кросс-ревью и тесты выбранного commit. Сохранить идентификатор предыдущего релиза. Проверки выполняются оператором; CI/CD не является условием ручного запуска.
2. На целевом Linux-хосте установить Docker Engine/Compose из официального источника и проверить `docker info`, `docker compose version`, `uname -m`, `df -h`, `free -h`, синхронизацию UTC/NTP и доступ к зависимостям. Команда `docker info` должна указывать целевой Linux daemon. Не использовать случайный удалённый Docker context.
3. На рабочей станции в checkout с установленным окружением проекта сгенерировать verifier командой `uv run python -m moex_sentinel.api.auth hash-password` или `.venv/bin/python -m moex_sentinel.api.auth hash-password`. Введите пароль не короче 16 UTF-8 байт дважды; команда печатает только verifier. Безопасно перенесите на сервер только готовый hash и сохраните его в env-файле оператора:

   ```sh
   cp deploy/remote/.env.example deploy/remote/.env
   chmod 600 deploy/remote/.env
   ```

   Заполнить `RELEASE_TAG` уникальным commit/release, `AUTH_USERNAME` и сгенерированный `AUTH_PASSWORD_HASH`. Для `POSTGRES_PASSWORD` использовать минимум 32 случайных байта в hex (например, `openssl rand -hex 32`). Также задать уникальные `COMPOSE_PROJECT_NAME` и имена volumes. Не включать `AUTH_INSECURE_LOOPBACK` в remote env: overlay жёстко устанавливает `false`. При переносе существующей торговли скопировать точные действующие параметры стратегии через защищённый канал. Значения стратегии в примере не являются новой рекомендуемой стратегией. Брокерские credentials находятся в данных Core и переносятся с защищённой резервной копией; не добавлять их в Git.

4. Проверить конфигурацию без запуска контейнеров и раскрытия env:

   ```sh
   python3 deploy/remote/preflight.py
   sh deploy/remote/compose.sh config --quiet
   ```

   Не сохранять обычный `compose config` в отчёт: он содержит пароль БД. Preflight не проверяет существование volumes, диск, broker connectivity или работоспособность контейнеров.

5. Собрать на целевой архитектуре. Сначала базовый образ: дочерние Dockerfiles используют жёсткое имя `moex-sentinel-python-base:local`. Не выполнять конкурентные сборки разных релизов в одном daemon.

   ```sh
   sh deploy/remote/compose.sh --profile build build python-base
   sh deploy/remote/compose.sh --profile migrations --profile trading build migrations backend analytics portfolio-snapshot-worker trading-automaton frontend
   ```

   Теги приложения сохраняют предыдущие образы для отката. Зависимости базового Python образа сейчас заданы диапазонами, а base/frontend/PG images — mutable tags; сборка commit не гарантирует одинаковые байты. Сохранить image IDs и архив `docker image save` всех образов приложения предыдущего релиза в защищённом хранилище. Строгая повторяемость сборки — отдельная задача плана улучшений.

## Перенос существующего состояния: два хоста

**Не запускать чистый bootstrap поверх существующей торговли.** Перенести согласованную пару PostgreSQL + весь каталог Worker SQLite, включая WAL/SHM. Не выполнять `docker compose down -v`, `volume prune` и не копировать живой `.db` отдельно от WAL.

На исходном хосте использовать его действующую Compose-команду (обычный `docker compose -f compose.yml`, если remote overlay там не применялся). Для переноса остановить торговлю до снятия резервной копии и оставить исходный Worker остановленным до завершения переключения. Остановленный Worker не отменяет уже принятые брокером заявки.

### Согласованная резервная копия

Пример для хоста, уже использующего wrapper; на исходной локальной установке заменить только Compose-префикс. Каталог backup находится вне checkout. Remote env и backup дополнительно исключены из Git и Docker context; не переносить дампы в каталог исходников:

```sh
umask 077
BACKUP_DIR="${HOME}/moex-sentinel-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR/worker"
sh deploy/remote/compose.sh stop trading-automaton
sh deploy/remote/compose.sh stop frontend portfolio-snapshot-worker analytics backend
```

Перед продолжением проверить `compose ps --all`: все перечисленные writers остановлены. Проверить завершение Worker (код выхода 0, `APPLICATION_STOPPED`, отсутствие принудительного SIGKILL); при ошибке сохранить диагностику и провести recovery до обычного запуска. Рабочая PostgreSQL остаётся включённой.

При наличии PROD повторить остановку его `trading-automaton`, frontend, analytics
и backend через `compose.production.sh` с точным PROD env. Подтвердить отсутствие
всех TEST/PROD writers до dump и последовательного копирования Worker; обе среды
остаются остановленными до окончания согласованной процедуры.

```sh
sh deploy/remote/compose.sh exec -T database sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$BACKUP_DIR/core.dump"
WORKER_CONTAINER=$(sh deploy/remote/compose.sh ps --all --quiet trading-automaton)
test -n "$WORKER_CONTAINER"
docker cp "$WORKER_CONTAINER:/app/data/." "$BACKUP_DIR/worker/"
sh deploy/remote/compose.sh images > "$BACKUP_DIR/images.txt"
git rev-parse HEAD > "$BACKUP_DIR/commit.txt"
```

Убедиться, что `pg_dump` завершился успешно и `core.dump` непуст. Сохранить env и manifest отдельно с правами 600/700, шифровать при хранении и передаче: дамп содержит broker credentials. Сделать SHA-256 manifest файлов (`sha256sum` на Linux), сверить после передачи. Резервная копия считается проверенной только после восстановления в новые тестовые volumes и проверки обеих БД. Архив/копия должен включать все файлы Worker, а не только файл основной SQLite.

### Восстановление на целевом хосте

Имена ниже — примеры из env. Если изменены, использовать точные выбранные имена. Для rehearsal взять отдельный project, другие имена volumes и порт, а также изолировать исходящий доступ к брокеру; production credentials при rehearsal заменить тестовыми до запуска приложений. **Торговый Worker в rehearsal не запускать.**

```sh
docker volume create moex-sentinel-remote-postgres-data
docker volume create moex-sentinel-remote-automaton-data
sh deploy/remote/compose.sh up -d --wait database
sh deploy/remote/compose.sh exec -T database sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error --no-owner --no-privileges' < "$BACKUP_DIR/core.dump"
```

`pg_restore` применяется только в **новую пустую БД**. Не использовать `--clean` на рабочей БД. При частичном неудачном восстановлении выбрать новые тестовые volumes и повторить; прежние volumes сохранить.

Worker image уже содержит Python и каталог с UID/GID 10001. Задать `WORKER_IMAGE` равным точному `moex-sentinel-trading-automaton:<RELEASE_TAG>` и `WORKER_VOLUME` равным env; выполнить копирование в новый пустой volume:

```sh
docker run --rm --network none --user 0:0 \
  --mount "type=volume,src=$WORKER_VOLUME,dst=/app/data" \
  --mount "type=bind,src=$BACKUP_DIR/worker,dst=/backup,readonly" \
  --entrypoint sh "$WORKER_IMAGE" \
  -c 'test ! -e /app/data/trading_automaton.db && cp -a /backup/. /app/data/ && chown -R 10001:10001 /app/data'
```

Проверить SQLite `PRAGMA integrity_check` в восстановленном тестовом volume до запуска Worker, а в PostgreSQL — текущую Alembic revision и контрольные количества automation/order/execution. Сверить scope, automation IDs, sequence Core/Worker и активные intent с исходным manifest через существующий audit skill, не публикуя credentials. Успешный `pg_restore` сам по себе не доказывает согласованность двух хранилищ.

### Миграция и проверка перед включением Worker

```sh
sh deploy/remote/compose.sh --profile migrations run --rm migrations
sh deploy/remote/compose.sh up -d --wait backend analytics portfolio-snapshot-worker frontend
curl --fail --silent http://127.0.0.1:8080/api/health
```

Миграция должна завершиться с кодом 0. Backend требует актуальный Alembic head. При ошибке не запускать Worker и не обходить проверку схемы. Для нового пустого приложения первые шаги восстановления пропускаются, но volumes, migration и проверка UI обязательны.

С рабочей станции открыть интерфейс через настроенный HTTPS на IP сервера (на текущем VPS — `https://135.136.178.252/`). Прямой HTTP на loopback пригоден только для `/api/health`: Secure cookie удалённого режима не позволяет войти через `http://127.0.0.1:8080`. До торговли проверить broker connection, открытые заявки и позиции, исполненные за время переноса заявки, совпадение scopes и отсутствие активного исходного Worker. HOLD/UNCERTAIN разбирать штатным recovery, не повторять отправку заявки вручную. Затем явное включение:

```sh
sh deploy/remote/compose.sh --profile trading up -d --no-deps trading-automaton
```

`--no-deps` здесь предполагает уже здоровые Core/Analytics. Проверить heartbeat, исчезновение pending outbox, продвижение sequences и совпадение Broker/Core/Worker, отсутствие повторной отправки известных intent. `/api/health` не доказывает здоровье торгового цикла. Исходный Worker оставить остановленным, автоматический рестарт старого хоста отключить на уровне эксплуатации до его вывода из работы.

## Обновление и откат

Для планируемого GitHub CI/CD согласован ручной запуск раскатки PROD кнопкой
**Run workflow** с выбором готового проверенного релиза (решение 10.10.2026).
Workflows пока не внедрены; ниже описан действующий порядок обновления.

1. Подготовить новый уникальный tag и проверить миграцию с rollback-скриптом на
   отдельной тестовой БД. Сохранить старые образы, параметры запуска и volumes.
2. Остановить Worker, затем остальные необходимые writers; проверить graceful
   shutdown. Обычное совместимое обновление не требует нового backup/restore.
3. Применить проверенную миграцию непосредственно к рабочей БД. Запустить
   совместимые Core/Analytics/UI и collector, затем единственный Worker.
   После смены PROD Worker выполнить штатную передачу владения; она оставляет
   `STOP_REQUIRED`/`recovery_only`, новый торговый допуск не выдаётся автоматически.
4. Приёмка после запуска: health checks, ping и открытие базового интерфейса.
   Проверка целостности, сравнение финансового графа и репетиция восстановления
   БД в обычный deployment не входят.
5. При аварии выполнить предусмотренный безопасный rollback схемы Core и
   приватной SQLite, затем вернуть совместимые прежние образы и параметры
   запуска (`up --no-build`). Не пересобирать старый tag из новых исходников.
   Если downgrade отказывает, сохранить данные и схему с совместимой версией
   приложения; не запускать старый Core с несовместимым Alembic head.

Backup/restore и перенос между хостами выше — отдельные эксплуатационные
процедуры. Они не повторяются при каждом обновлении. Существующие резервные
копии сохраняются; исполненные брокерские действия откатом БД не отменяются.

Для выпуска квоты `0005_order_quota_percent` откат подготовлен в самой
Alembic-миграции и в `develop/position-quota/rollback_worker_quota.py`.
Сначала остановить PROD Worker/Core и общий collector, сохранить доказательство
остановки владельца. Запускать downgrade до `0004_trading_admission` новым Core
образом с действующим `DATABASE_URL` из PROD Compose, через Alembic Python API;
сырой `alembic upgrade/downgrade` без явного URL использует SQLite из ini.
Затем выполнить Worker-скрипт с единственным аргументом — существующим путём
приватной SQLite из `LOCAL_DATABASE_URL`, на исходном volume. Оба скрипта
отказывают при сохранённом ненулевом `order_quota_percent`; настройки не удалять
ради прохождения проверки. При частичном откате схем сначала вернуть совместимый
head0005 новым образом, а не запускать несовместимый старый Core.
После успешного отката обеих схем вернуть проверенную пару прежних image IDs,
запустить collector/Core/UI и Worker, передать владение без ARM. TEST приложения
остаются выключены. Публичный маршрут при откате приложения сохраняет PROD443,
`AUTH_ALLOWED_ORIGIN=https://135.136.178.252` и PROD-cookie; порт8443 не возвращать.
Старые параметры сохраняются как частные входы операции, секреты не выводить.

## Эксплуатационные проверки

Контролировать свежесть heartbeat и котировок, старейший pending outbox/retries, расхождение sequence и позиции, UNCERTAIN/HOLD, свободный диск обоих volumes и размер Docker logs. Не считать закрытую биржу отказом приложения. Сохранять причину UNKNOWN отдельно от легитимного WAIT. Нужна проверенная периодичность резервных копий и off-host хранение; их расписание и срок хранения определяются с владельцем сервера после измерения объёма данных.
