# Удалённый Linux Docker Compose

## Границы готовности

Исторический снимок на 2026-10-02: приложение развёрнуто на VPS `135.136.178.252` (Ubuntu 24.04, amd64, Docker 29.7.2, Compose v5.4.0). Замеры того снимка: RAM1,92GiB, диск55,99GiB/available47,45GiB. Локальная история не переносилась; состояние Sandbox было загружено штатным API. Повторный SSH/sudo успешен; running `STRATEGY_ENABLED=false`, broker/Core/Worker ledger и доставка выборки сверены. Каталог/позиции/выход и session401 в живом браузере подтверждены; restart без дублей оставался открыт. Evidence: `develop/reports/vps-audit-20261002/`. Источники скопированы из проверенного рабочего дерева в root-owned релиз; локальный код ещё не был закоммичен. Ниже также приведена общая процедура для будущих релизов. Этот документ не обещает SLA.

База и внутренние API не имеют опубликованных портов. UI слушает только `127.0.0.1:8080`; Nginx публикует его через `https://135.136.178.252/`. Backend требует учётные данные оператора; HTTP cookie для loopback на удалённом Compose выключена. Не открывать Docker API наружу.

На текущем VPS созданы новые внешние volumes PostgreSQL и Worker SQLite. Worker остаётся **один на брокерский scope во всех хостах вместе**; replica=1 не предотвращает второй процесс на другом сервере. Профиль `trading` отделяет его запуск от проверки UI. Worker имеет 120 секунд на SIGTERM, остальные сервисы — 90 секунд; Docker-логи ротируются. Worker помечает чистое завершение после закрытия runtime; код выхода/логи проверяются отдельно.

### Публикация по IP

На текущем VPS сертификат Let's Encrypt содержит IP в SAN и автоматически продлевается Certbot. Nginx проксирует только loopback frontend (`127.0.0.1:8080`), закрывает `/internal/*` и ограничивает `/api/auth/login` по IP клиента: 10 запросов в минуту с burst до 5. Внешние проверки подтвердили HTTPS, отказ анонимному API, вход, защищённую cookie, CSRF, выход, отзыв сессии и HTTP 429 после превышения лимита.

Overlay использует [правила merge Docker Compose](https://docs.docker.com/reference/compose-file/merge/), включая `!override` (требует 2.24.4). [Профиль](https://docs.docker.com/compose/how-tos/profiles/) исключает Worker из обычного `up`, но явное указание имени сервиса запускает его даже без `--profile`.

## Развёрнутый VPS

### Текущий снимок после deploy 03.10.2026

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
- `deploy/remote/.env.example`: имена переменных и пример стратегии; не содержит реальных credentials.
- `deploy/remote/compose.sh`: единый wrapper с явными compose/env путями; запускать через `sh`.
- `deploy/remote/compose.production.sh` и `compose.production.yml`: явный PROD wrapper и overlay общей БД.
- `deploy/remote/preflight.py`: read-only проверка разрешённой конфигурации; не выводит её секреты.
- `deploy/remote/nginx-ip.conf`: HTTPS reverse proxy для текущего IP и rate limit входа.
- `deploy/remote/reload-nginx.sh`: hook Certbot после успешного продления сертификата.

Команды ниже выполнять из корня выбранного checkout. `REMOTE_ENV_FILE` может указать другой абсолютный путь к env. Не использовать текущий локальный `.env` для случайного запуска второго Worker.

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

1. Собрать новый уникальный tag заранее; сохранить старые образы и точные env/commit.
2. Остановить Worker, затем остальные writers, проверить graceful shutdown. Сделать согласованную резервную копию обеих БД.
3. Применить миграции новой версии. Запустить Core/Analytics/UI, проверить health и audit. Включить единственный Worker последним.
4. Если приложение сломано **до возобновления торговли** и схема совместима, вернуть старый `RELEASE_TAG` и прежний checkout, запускать `up --no-build`; не выполнять rebuild старого tag из новых исходников.
5. При несовместимой схеме не делать слепой Alembic downgrade. Восстановить согласованную пару backup в **новые** volumes, переключить env на них, сохранив повреждённые volumes для расследования. Проверить совместимость версии Worker SQLite отдельно.
   Общая PG затрагивает обе среды: перед restore остановить все TEST/PROD writers;
   после переключения оба Core должны указывать на одну восстановленную database.
6. Если после backup уже были новые брокерские действия, откат данных к backup может потерять intent и привести к повторной торговле. Сначала остановить Worker и провести reconciliation с брокером; обычное восстановление старого backup запрещено как автоматическая процедура. Брокерское исполнение откатом БД не отменяется.

## Эксплуатационные проверки

Контролировать свежесть heartbeat и котировок, старейший pending outbox/retries, расхождение sequence и позиции, UNCERTAIN/HOLD, свободный диск обоих volumes и размер Docker logs. Не считать закрытую биржу отказом приложения. Сохранять причину UNKNOWN отдельно от легитимного WAIT. Нужна проверенная периодичность резервных копий и off-host хранение; их расписание и срок хранения определяются с владельцем сервера после измерения объёма данных.
