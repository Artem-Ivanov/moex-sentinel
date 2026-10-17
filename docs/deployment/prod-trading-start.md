# Порядок выпуска и включения торговли

> **Граница версий.** Локальная веха «допуск по `IN_WORK`» меняет рабочий исходный
> код и документацию, но сама по себе не обновляет VPS. Пока она отдельно не
> выпущена, для VPS на релизе `vps-20261015-env-4a71589e8766` действует старый
> canary/ARM/reconciliation путь из исторической секции ниже. После выпуска новой
> версии отдельные ARM и ручная сверка больше не требуются: заявки проверяются по
> `IN_WORK`, текущему TRADE/owner/config, рыночным и broker условиям и четырём
> account-level caps. Не считать локальный checkout фактом runtime-изменения.

Исторический пакет 14.10 описан в [плане](../superpowers/plans/2026-10-14-prod-trading-start.md) и [дизайне](../superpowers/specs/2026-10-14-prod-trading-start-design.md); записи о его выпуске ниже сохранены как история. Новая локальная реализация допуска по IN_WORK описана в [спецификации](../superpowers/specs/2026-10-16-position-driven-admission-design.md) и будет действовать на VPS только после отдельного выпуска. Этот runbook сам по себе не подтверждает runtime или торговую приёмку.

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

Зафиксировать source manifest проверенного checkout, зависимости и текущую схему; собрать проверенные Core/Worker/frontend артефакты через существующие canonical wrappers. Pending DB улучшения 0006/0007/Worker revision1 имеют отдельную приёмку; состав релиза определяется до выпуска. Пользовательские данные, исходные volumes и HOLD сохраняются. TEST остаётся retired; shared PostgreSQL и collector нужны PROD.

Если меняется Worker runtime UUID, выполнить штатную controlled transfer с подтверждённой остановкой прежнего owner; restart не передаёт ownership. Приёмка deployment: health/ping нужных сервисов, открытие UI, корректный runtime и базовое отображение позиции/допуска. Совместимая проверенная migration применяется непосредственно; новый полный backup/restore не является обязательным шагом обычного выпуска. Working downgrade отдельно разрешается и соблюдает ограничения сохранения финансовой истории. После canonical записи лимитов `ACCOUNT_GROSS` старый single-UID application fallback несовместим: выпускать Core/Worker/frontend согласованным набором; откат приложения возможен только на версию, понимающую canonical account scope и per-UID anchors, со схемой и данными без downgrade.

Для owner transfer после чистой остановки старого Worker Core запускается с новой версией, затем новый Worker регистрируется как `PENDING`. Candidate UUID читается только через operator-authenticated `GET /api/brokers/{broker_id}/trading-admission/pending-runtime?worker_id=...&registered_after=<stop-UTC>`: ответ ограничен точным broker/worker и временем регистрации после clean stop, содержит только публичные runtime metadata, не секреты. `404` означает, что кандидат ещё не зарегистрирован и допускает ограниченное ожидание; `409` означает неоднозначность и требует остановить rollout. Не запрашивать candidate прямым SQL к PROD-БД. Сверить неизменность container ID/restart count между lookup и единственным `owner-stopped` POST. Если ответ POST потерян, повторять его нельзя: stop proof сохраняется и continuation требует отдельной сверки owner через штатный API.

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
