# Порядок выпуска и включения торговли

> **Последняя проверка 19.10.2026, 10:47 МСК.** Общий допуск открыт,
> lease продлевается, база19.10 принята09:18МСК. Оба автомата сейчас HOLD9 с
> активными UNCERTAIN: исход прежних заявок не удалось однозначно сверить.
> Последние принятые Core решения10:33МСК — WAIT/ACTIVE_INTENT, не текущий tick.
> В логах повторяется BROKER_ORDER_NOT_FOUND/NOT_FOUND; связь с исходными
> заявками ещё не доказана. Сегодняшние исполнения в полученной странице
> операций не найдены; полная история не проверена. Без Resume/clear/repost.
> Свежий аудит: develop/reports/trading-status-20261019/audit-report.md,
> задачи: tasks.md в том же каталоге. Приёмка выпуска18.10 — историческая;
> она не подтверждает сегодняшний торговый цикл.

Дополнительно API проверен с VPS **19.10 в11:03МСК**: PROD endpoint,
проверенный TLS с CA SDK, GetAccounts и GetPortfolio доступны; настроенный счёт
найден, reportedFULL_ACCESS. Текущие ошибки логов — именно
GetOrderState/NOT_FOUND/50005. Это поиск заявки, не отказ доступности API.
Стандартная TLS-проверка с системным CA store возвращает code19, но SDK со
встроенным НУЦ CA проходит проверку; доверие и конфигурацию не меняли.
Причина отсутствия каждого исходного request ID пока не установлена;
ограничение GetOrderState для старых заявок не является terminal proof.
Подробности: `develop/reports/broker-not-found-20261019/report.md`.
Эта проверка сети не обновляет снимок состояний автоматов10:47МСК выше.

Исторический пакет 14.10 описан в [плане](../superpowers/plans/2026-10-14-prod-trading-start.md) и [дизайне](../superpowers/specs/2026-10-14-prod-trading-start-design.md); записи о выпусках 14.10 и 15.10 ниже сохранены как история. Допуск по `IN_WORK` описан в [спецификации](../superpowers/specs/2026-10-16-position-driven-admission-design.md). Успешная техническая приёмка rollout не означает приёмку реальной торговли.


## Последние фактические решения стратегии

Через обычную operator-auth сессию доступен read-only запрос:

```text
GET /api/trading-automations/{automation_id}/decision-reasons?limit=5
```

Допустимы1–20событий, по умолчанию5. Запрос ограничен broker/automation и
STRATEGY_DECISION_MADE; ответ содержит captured_at и items с event_id,
occurred_at, decision, reason_code. Некорректные legacy decision/reason
возвращаются null, последнее событие не подменяется более старым.

captured_at — время чтения, occurred_at — время Worker-события. Это последние
по occurred_at события, уже принятые Core; ответ не является watermark и не
доказывает отсутствия более свежего локального события Worker. Сопоставляйте
возраст события с command_sync и outbox. Не читать PROD-таблицы напрямую.

В22:32МСК18.10 SIBN вернул свежий WAIT/BUY_WINDOW_UNCONFIRMED при IN_WORK и
работающем цикле. Guard срабатывает при отсутствии индикаторов либо
buy_window_confirmed=false: нужны корректные метрики и последовательное окно
завершённых минутных свечей. Конкретный gap, stale input или invalid metric из
одного reason_code установить нельзя. Исторические GOLD STOP_LOSS16.10 не
считать текущим решением или доказательством разрешения UNCERTAIN.

## Конфигурация Core

Полный перечень ENV, defaults и правил размещения описан в
[каталоге конфигурации](../configuration/environment.md). Приватный
`deploy/remote/.env.production` остаётся отдельным от шаблонов и не изменяется
этой документацией. PROD пример включает дневную подготовку базы, но это не
означает активацию на существующем VPS.

Для отдельной настройки уведомлений и подготовки дня используются:

```dotenv
DAILY_ADMISSION_PREPARATION_ENABLED=false
TELEGRAM_ALERTS_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Файл не включать в Git, не печатать `docker compose config` с реальными секретами в отчёт. Telegram Bot Token получить через официальный [BotFather](https://core.telegram.org/bots/features#botfather); chat ID — выбранный личный/групповой получатель. Токен хранится только в приватном конфиге Core. После задания корректных token/chat ID включить `TELEGRAM_ALERTS_ENABLED=true`, применить конфигурацию контролируемым перезапуском Core и проверить доставку согласованного тестового инцидента/восстановления. На этапе локальных тестов реальных отправок нет.

`prepare_once` запускает daily preparation только при одновременно заданных
`DAILY_ADMISSION_PREPARATION_ENABLED=true`, `APPLICATION_ENVIRONMENT=PROD`,
`BROKER_ACCESS_MODE=TRADE` и `INTERNAL_WORKER_BROKER_ID`; при невыполненном
условии сервис возвращает `SCOPE_UNAVAILABLE`. PROD шаблон намеренно остаётся
`READ_ONLY`, поэтому флаг сам по себе не включает торговый режим. Эта процедура
не меняет `BROKER_ACCESS_MODE` и не разрешает переключать его. Подготовка создаёт
только дневную базу; заявка проверяется по `IN_WORK`, owner/lease, `TRADE`, свежим
брокерским данным и account-level лимитам. Same-day повторы не сбрасывают базу,
дневной убыток или лимиты.

## Отдельная активация дневной базы

Сохраните текущий приватный env и credentials. Выполняйте эту процедуру только
если `APPLICATION_ENVIRONMENT=PROD`, `BROKER_ACCESS_MODE=TRADE` и
`INTERNAL_WORKER_BROKER_ID` уже заданы в согласованной конфигурации. Тогда
измените только `DAILY_ADMISSION_PREPARATION_ENABLED=true`. Если текущий режим `READ_ONLY` или broker ID
отсутствует, остановитесь: изменение одного флага даст `SCOPE_UNAVAILABLE`;
этот runbook не переключает режим и не задаёт broker ID. Создайте приватный env
из PROD шаблона только при первичной настройке; защитите файл режимом `0600`.
Проверьте Compose через штатный wrapper с quiet-режимом, не печатая развёрнутый
config и секреты. Затем выполните контролируемый перезапуск Core по действующей
процедуре. Проверьте Core health, итог daily preparation и базу текущего дня
через штатные read-only интерфейсы. Изменение приватного файла само по себе не
меняет работающий процесс; этот runbook не заявляет, что настройка уже
активирована на VPS. ARM и Resume к этой процедуре не относятся.

## Выпуск проверенного checkout

При размещении приватного ENV в `/etc/moex-sentinel/production.env` каждому
вызову штатного wrapper передаётся этот путь. Без него wrapper ищет
`deploy/remote/.env.production` внутри текущего release:

```sh
REMOTE_ENV_FILE=/etc/moex-sentinel/production.env \
  sh deploy/remote/compose.production.sh --profile trading config --quiet
```

Зафиксировать source manifest проверенного checkout, зависимости и текущую схему; собрать проверенные Core/Worker/frontend артефакты через существующие canonical wrappers. Pending DB улучшения 0006/0007/Worker revision1 имеют отдельную приёмку; состав релиза определяется до выпуска. Пользовательские данные, исходные volumes и HOLD сохраняются. TEST остаётся retired; shared PostgreSQL и collector нужны PROD.

Если меняется Worker runtime UUID, выполнить штатную controlled transfer с подтверждённой остановкой прежнего owner; restart не передаёт ownership. Приёмка deployment: health/ping нужных сервисов, открытие UI, корректный runtime и базовое отображение позиции/допуска. Совместимая проверенная migration применяется непосредственно; новый полный backup/restore не является обязательным шагом обычного выпуска. Working downgrade отдельно разрешается и соблюдает ограничения сохранения финансовой истории. После canonical записи лимитов `ACCOUNT_GROSS` старый single-UID application fallback несовместим: выпускать Core/Worker/frontend согласованным набором; откат приложения возможен только на версию, понимающую canonical account scope и per-UID anchors, со схемой и данными без downgrade.

Для owner transfer после чистой остановки старого Worker Core запускается с новой версией, затем новый Worker регистрируется как `PENDING`. Candidate UUID читается только через operator-authenticated `GET /api/brokers/{broker_id}/trading-admission/pending-runtime?worker_id=...&registered_after=<stop-UTC>`: ответ ограничен точным broker/worker и временем регистрации после clean stop, содержит только публичные runtime metadata, не секреты. `404` означает, что кандидат ещё не зарегистрирован и допускает ограниченное ожидание; `409` означает неоднозначность и требует остановить rollout. Не запрашивать candidate прямым SQL к PROD-БД. Сверить неизменность container ID/restart count между lookup и единственным `owner-stopped` POST. Если ответ POST потерян, повторять его нельзя: stop proof сохраняется и continuation требует отдельной сверки owner через штатный API.

`owner-stopped` сохраняет уже настроенные лимиты. После transfer проверить их точное совпадение с исходными; повторный PUT при сохранённых лимитах не нужен. `recovery_only` — служебное происхождение восстановления: обычный успешный submit permit может сразу сбросить его в `false`. Оба boolean-значения допустимы после transfer; флаг не служит дополнительным ручным допуском. `effective_can_submit` проверяется вместе с фактическими причинами отказа, без требования `false` или отдельного ARM.

## Ограниченный rollout с сохранением Worker storage

Этот протокол задаёт условия ограниченного выпуска из независимо проверенного immutable package. Фактическое исполнение 18.10 и пределы подтверждённой приёмки указаны ниже.

### Фактический rollout 18.10.2026

На PROD установлен `vps-20261018-prod-437cc0757b90` (Core, Worker, Analytics и frontend). Rollout завершился `ACCEPTANCE_PASS` в 09:02:08 UTC: старый Worker завершился с кодом 0 без рестартов, новый owner получил generation 6, health checks прошли, UI отдал два базовых asset-файла; браузерная проверка подтвердила отображение страницы входа. См. [журнал rollout](../../develop/reports/prod-completeness-20261018/rollout-execution.jsonl), [итог проверок](../../develop/reports/prod-completeness-20261018/verification.json), [подготовленные image IDs](../../develop/reports/prod-completeness-20261018/prepare-retry-result.json), [DOM UI smoke](../../develop/reports/prod-completeness-20261018/prod-ui-smoke.html) и [скриншот](../../develop/reports/prod-completeness-20261018/prod-ui-smoke.png).

Передача owner завершилась для `401a0392-1f7e-4ec6-aa9e-96d643a8d8f4` с generation 6. На момент передачи caps были `null`; rollout не выполнял действий `HOLD`/`Resume`, а pre-transfer snapshot содержал `HOLD`/`IN_WORK`. Acceptance receipt помечает известный исторический `UNCERTAIN` как `HISTORICAL_NOT_RECHECKED`. Поэтому этот rollout подтверждает установку и ограниченные health/UI проверки, но не разрешение торговли, принятие новых заявок или закрытие исторической неопределённости. Свежий post-rollout API/runtime результат здесь не приводится.

Локальная проверка пакета завершилась: backend 2804 passed, 2 skipped только из-за отсутствия `pg_dump` для paired restore rehearsal; frontend 259 passed, typecheck/build прошли. Все перечисленные независимые reviews завершились `C0/I0/M0`; детали — в [итоговом verification receipt](../../develop/reports/prod-completeness-20261018/verification.json).

Для первоначального выпуска437 использовался preflight без настроенных лимитов: операторский read-only снимок подтверждал PROD/TRADE, Core health и совместимость схемы, worker diagnostics/outbox, owner UUID/generation/revision/live lease, `DISARMED`, `limits=null`, отсутствие ARM и `effective_can_submit=false`, а также состояния `HOLD`/`IN_WORK`. Этот исторический снимок не является шаблоном для обновления уже настроенного контура. Read-only helper сохраняет только allowlisted публичные поля, ENV checksum, четыре container/image IDs и whitelisted lifecycle SHA-256. Он не сохраняет значения приватного ENV, пароли, полную конфигурацию, SQL/SQLite данные или произвольный лог. Известный исторический `UNCERTAIN` остаётся `HISTORICAL_NOT_RECHECKED` с исходной отметкой времени; свежую запись или число intent по нему не заявлять.

Новый пакет включает проверенный Core, Worker, Analytics и frontend. Analytics reader обновляется вместе с producer-контрактом. Родительский Compose сохраняется с ровно двумя добавочными Worker/Core настройками: `BROKER_REQUEST_TIMEOUT_SECONDS=10` и `MARKET_TRADING_STATUS_TTL_SECONDS=30`; приватный ENV и остальные bindings, logging, topology и volumes остаются прежними. Все четыре image строятся локально на VPS из подтверждённых base image IDs с `--network=none` и без установки пакетов; active image tags не переиспользуются. Manifest связывает исходники, frontend dist assets, lock/build файлы, Dockerfiles и base IDs с тегом и archive checksum.

Непосредственно перед Worker stop заново сверить ENV checksum, runtime/admission/automation snapshot и pinned Worker ID/image/restart count. Установить ему `restart=no`, послать `SIGTERM` только pinned container ID и ждать ограниченное время. Продолжать можно только если после запроса тот же container завершился с exit code 0, без OOM, без рестартов/замены, с новым `FinishedAt` и свежим scoped `APPLICATION_STOPPED` log event. Log marker сам по себе остановку не доказывает. Timeout, SIGKILL/137, OOM, identity drift или отсутствие свежего события означают fail-stop до изменения production-current link, Core или owner. Не запускать второй Worker и не включать прежний auto-restart.

Полное соответствие `Type/Name/Source/Destination/RW` mounts и совпадение только SHA-256 `AUTOMATON_DATABASE_URL` обязательно для старого и нового Worker. Все durable active intents, reservations, outbox и `HOLD` остаются в той же SQLite volume; release не читает базу и не пересоздаёт volume. После clean stop поднять Core/UI и Analytics reader, дождаться ограниченных health checks, затем запустить один Worker candidate и проверить его container/image/restart count, storage mapping и отсутствие второго Worker с тем же storage/config identity.

После старта нового Worker найти только его scoped `PENDING` через operator API. Ограниченно ждать только `404`; при `409` или нескольких кандидатах остановиться. Сверить неизменность Worker container и исходного admission до единственного `owner-stopped` POST. При потерянном ответе выполнить только GET owner/generation и разрешить результат, если уже зарегистрирован ровно ожидаемый candidate; POST повторно не посылать. Обычный recovery продолжает `UNCERTAIN`/`HOLD`; deployment не разрешает `resolved`, Resume, ARM или новую заявку без отдельного штатного доказательства.

После изменения owner ограничить приёмку health/ping, runtime owner/generation и открытием UI с доступными базовыми assets. Не читать таблицы/SQLite, не сравнивать всю финансовую историю и не выполнять restore rehearsal. Старый single-UID fallback несовместим с account-level caps; при проблеме оставить unsafe Worker остановленным и исправлять вперёд совместимым приложением, не откатывая финансовую схему, данные или volume.

## Выпуск 15.10.2026 — ENV и автоматическая база

По явной команде пользователя выпущен `vps-20261015-env-4a71589e8766` до merge:
Core/Worker/Analytics, без новых миграций и зависимостей. Frontend, shared PG,
Collector, volumes и остановленный TEST сохранены. В приватном PROD ENV изменён
только daily flag на true; TRADE/strategy/credentials/canary сохранены. Worker
штатно остановлен с exit0; новый runtime получил generation5/revision8 через
owner-stopped в recovery-only. Приёмка health/UI/source hashes/progress PASS,
независимое review env_core_reviewer C0/I0/M0.

База за 15 октября принята автоматически в 19:06:54 МСК. На 19:08:49 МСК
счёт всё ещё STOP_REQUIRED/recovery-only, без reconciliation/ARM/limits;
новых операций в полученной выборке нет. Обе позиции IN_WORK. Подготовка базы
не заменяет явные сверку и ARM; торговая приёмка не закрыта. Отчёт:
`develop/reports/runtime-env-deploy-20261015/report.md`, runtime review `review-final.md`.

Предыдущий релиз с Core schema0007 сохранён как совместимый fallback. При
откате Worker требуется новый stop proof и controlled owner transfer; схема,
volumes и финансовая история сохраняются.

## Выпуск 14.10.2026

По отдельному указанию пользователя deployment выполнен до merge. На PROD
активен `vps-20261014-prod-start-b2f54029c729`: Core/Worker/frontend обновлены,
PostgreSQL0007 и WorkerSQLiterevision1 подтверждены. Обычная приёмка
health/progress/HTTPUI/read APIs PASS; независимый reviewer prod_release_architect
C0/I0/M0. HOLDrev3 и STOP_REQUIRED/recovery-only сохранены, daily/Telegramdisabled;
shared PG/collector/Analytics сохранены, TESTretired. Визуальный browser и торговая
приёмка не проводились. Receipt: `develop/reports/prod-trading-start-deploy-20261014/report.md`.

Прежний Core ожидает0005: подготовлен совместимый application fallback с прежним
Core code и проверенными migration files0006/0007. Он сохраняет schema0007 и данные;
для fallback Worker нужна новая подтверждённая остановка и controlledownertransfer
с актуальной generation. Working downgrade/restore не выполнялись и отдельно
разрешаются. Нельзя просто вернуть прежнюю production-current symlink.

## Историческая процедура для VPS до выпуска допуска по IN_WORK

Зачесть актуальные доказательства и закрыть оставшуюся read-only P4 приёмку, включая полный день/закрытый рынок, выбранный счёт и факты/outbox, получателя Telegram, проверенную остановку и процедуру UNKNOWN. Внутренний sender не наблюдает смерть целого Core/VPS; внешний uptime probe остаётся отдельной операционной проверкой.

Эти шаги относятся только к версии, где canary reconciliation и ARM ещё являются частью торгового допуска. Они не описывают исходный код после выпуска position-driven admission. Затем требовались fresh current MSK-day baseline, CANARY reconciliation с `can_arm=true`, действующие owner/gen/lease/config и явные limits по свежим данным. После истечения 30с сверки требовался новый operator reconcile для нового ARM; подготовка не продлевала этот срок. Existing CLI `python -m moex_sentinel.operator_admission` / защищённые admission API поддерживали view, baseline, reconcile, arm, revoke; требовалось сохранять ожидаемую revision из свежего view.

В старой версии ARM и Resume выполнялись только по явной команде владельца, после принятия read-only gates. Для первой штатной операции применяется существующий Worker strategy→permit→SDK→outbox→Core; подтверждаются terminal/partial fill, комиссии и отсутствие дубликата. При UNKNOWN сохранять резерв и permanent key, сначала read-only recovery; не создавать повторную заявку или принудительную сделку ради проверки. Deployment и локальные тесты не закрывают P5 live canary.
