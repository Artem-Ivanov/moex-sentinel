# Удалённый Linux Docker Compose

## Границы готовности

Подтверждённый сценарий: оператор подключается по SSH, клонирует проект из Git и вручную выполняет сборку и запуск на сервере. CI/CD для этого развёртывания не требуется. Подготовлен overlay поверх `compose.yml`; удалённый запуск не выполнен. ОС и характеристики сервера пока не сообщены. Допущения: один Linux-хост, локальный SSD, Docker Engine и Compose >= 2.24.4, разрешён исходящий HTTPS/gRPC к брокеру и registry. CPU, RAM, свободное место, архитектуру CPU, hostname, пользователя SSH и объём текущих данных надо сверить перед переносом. Выделение ресурсов выбирается после замера; этот документ не обещает SLA.

База и внутренние API не имеют опубликованных портов. UI слушает только `127.0.0.1:8080`: доступ через SSH-туннель. Приложение не имеет пользовательской аутентификации; опубликованный в интернете UI/API требует отдельного решения аутентификации перед reverse proxy. До него сохранять loopback. Не открывать Docker API наружу.

Используются существующие внешние volumes PostgreSQL и Worker SQLite. Worker остаётся **один на брокерский scope во всех хостах вместе**; replica=1 не предотвращает второй процесс на другом сервере. Профиль `trading` отделяет его запуск от проверки UI. Сервисы имеют 90 секунд на SIGTERM и ограниченную ротацию Docker-логов. Worker помечает чистое завершение после закрытия runtime; код выхода/логи проверяются отдельно.

Overlay использует [правила merge Docker Compose](https://docs.docker.com/reference/compose-file/merge/), включая `!override` (требует 2.24.4). [Профиль](https://docs.docker.com/compose/how-tos/profiles/) исключает Worker из обычного `up`, но явное указание имени сервиса запускает его даже без `--profile`.

## Файлы

- `deploy/remote/compose.remote.yml`: overlay, закрытые порты, теги образов, singleton Worker.
- `deploy/remote/.env.example`: имена переменных и пример стратегии; не содержит реальных credentials.
- `deploy/remote/compose.sh`: единый wrapper с явными compose/env путями; запускать через `sh`.
- `deploy/remote/preflight.py`: read-only проверка разрешённой конфигурации; не выводит её секреты.

Команды ниже выполнять из корня выбранного checkout. `REMOTE_ENV_FILE` может указать другой абсолютный путь к env. Не использовать текущий локальный `.env` для случайного запуска второго Worker.

## Подготовка релиза

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

Сверить выведенный commit с выбранным релизом. Все следующие команды выполнять в этом checkout на сервере, кроме явно обозначенного SSH-туннеля с рабочей станции. Клонирование переносит исходники; существующие БД и локальная конфигурация переносятся отдельно по разделу ниже.

### Конфигурация и сборка вручную

1. Завершить recovery-кросс-ревью и тесты выбранного commit. Сохранить идентификатор предыдущего релиза. Проверки выполняются оператором; CI/CD не является условием ручного запуска.
2. На целевом Linux-хосте установить Docker Engine/Compose из официального источника и проверить `docker info`, `docker compose version`, `uname -m`, `df -h`, `free -h`, синхронизацию UTC/NTP и доступ к зависимостям. Команда `docker info` должна указывать целевой Linux daemon. Не использовать случайный удалённый Docker context.
3. Подготовить env, доступный только оператору:

   ```sh
   cp deploy/remote/.env.example deploy/remote/.env
   chmod 600 deploy/remote/.env
   ```

   Заполнить `RELEASE_TAG` уникальным commit/release, пароль — минимум 32 случайных байта в hex (например, `openssl rand -hex 32`), уникальные `COMPOSE_PROJECT_NAME` и имена volumes. При переносе существующей торговли скопировать точные действующие параметры стратегии через защищённый канал. Значения в примере не являются новой рекомендуемой стратегией. Брокерские credentials находятся в данных Core и переносятся с защищённой резервной копией; не добавлять их в Git.

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

С рабочей станции:

```sh
ssh -N -L 8080:127.0.0.1:8080 USER@HOST
```

Открыть `http://127.0.0.1:8080`. До торговли проверить broker connection, открытые заявки и позиции, исполненные за время переноса заявки, совпадение scopes и отсутствие активного исходного Worker. HOLD/UNCERTAIN разбирать штатным recovery, не повторять отправку заявки вручную. Затем явное включение:

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
6. Если после backup уже были новые брокерские действия, откат данных к backup может потерять intent и привести к повторной торговле. Сначала остановить Worker и провести reconciliation с брокером; обычное восстановление старого backup запрещено как автоматическая процедура. Брокерское исполнение откатом БД не отменяется.

## Эксплуатационные проверки

Контролировать свежесть heartbeat и котировок, старейший pending outbox/retries, расхождение sequence и позиции, UNCERTAIN/HOLD, свободный диск обоих volumes и размер Docker logs. Не считать закрытую биржу отказом приложения. Сохранять причину UNKNOWN отдельно от легитимного WAIT. Нужна проверенная периодичность резервных копий и off-host хранение; их расписание и срок хранения определяются с владельцем сервера после измерения объёма данных.
