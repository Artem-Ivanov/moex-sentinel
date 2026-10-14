# Порядок выпуска и включения торговли

Локальная реализация 14.10 готовится по [плану](../superpowers/plans/2026-10-14-prod-trading-start.md) и [дизайну](../superpowers/specs/2026-10-14-prod-trading-start-design.md). 14.10 пользователь изменил порядок: разрешён выпуск проверенного checkout на VPS до merge. Merge и Git-коммиты выполняет пользователь после выпуска. Сам runbook не подтверждает runtime или торговую приёмку.

## Конфигурация Core

В приватном `deploy/remote/.env.production` при отдельной настройке уведомлений и подготовки дня задать:

```dotenv
DAILY_ADMISSION_PREPARATION_ENABLED=false
DAILY_ADMISSION_CANARY_UID=
TELEGRAM_ALERTS_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Файл не включать в Git, не печатать `docker compose config` с реальными секретами в отчёт. Telegram Bot Token получить через официальный [BotFather](https://core.telegram.org/bots/features#botfather); chat ID — выбранный личный/групповой получатель. Токен хранится только в приватном конфиге Core. После задания корректных token/chat ID включить `TELEGRAM_ALERTS_ENABLED=true`, применить конфигурацию контролируемым перезапуском Core и проверить доставку согласованного тестового инцидента/восстановления. На этапе локальных тестов реальных отправок нет.

Daily preparation использует только существующий `INTERNAL_WORKER_BROKER_ID` в PROD с живым owner/lease. Для включения задать `DAILY_ADMISSION_PREPARATION_ENABLED=true`; пустой CANARY UID означает только базу дня. Непустой UID задаёт read-only сверку выбранного инструмента, без ARM. Новая база не снимает STOP_REQUIRED/recovery или HOLD; требуется явное восстановление допуска оператором. Same-day повторы не сбрасывают базу, дневной убыток, ARM и лимиты. Suggested limits в интерфейсе — preview.

## Выпуск проверенного checkout

Зафиксировать source manifest проверенного checkout, зависимости и текущую схему; собрать проверенные Core/Worker/frontend артефакты через существующие canonical wrappers. Pending DB улучшения 0006/0007/Worker revision1 имеют отдельную приёмку; состав релиза определяется до выпуска. Пользовательские данные, исходные volumes и HOLD сохраняются. TEST остаётся retired; shared PostgreSQL и collector нужны PROD.

Если меняется Worker runtime UUID, выполнить штатную controlled transfer с подтверждённой остановкой прежнего owner; restart не передаёт ownership. Приёмка deployment: health/ping нужных сервисов, открытие UI, корректный runtime и базовое отображение позиции/допуска. Совместимая проверенная migration применяется непосредственно; новый полный backup/restore не является обязательным шагом обычного выпуска. Working downgrade отдельно разрешается и соблюдает ограничения сохранения финансовой истории.

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

## До ARM и Resume

Зачесть актуальные доказательства и закрыть оставшуюся read-only P4 приёмку, включая полный день/закрытый рынок, выбранный счёт и факты/outbox, получателя Telegram, проверенную остановку и процедуру UNKNOWN. Внутренний sender не наблюдает смерть целого Core/VPS; внешний uptime probe остаётся отдельной операционной проверкой.

Затем fresh current MSK-day baseline, CANARY reconciliation с `can_arm=true`, действующие owner/gen/lease/config и явные limits по свежим данным. После истечения 30с сверки нужен новый operator reconcile для нового ARM; подготовка не продлевает этот срок. Existing CLI `python -m moex_sentinel.operator_admission` / защищённые admission API поддерживают view, baseline, reconcile, arm, revoke; сохранять ожидаемую revision из свежего view.

ARM и Resume выполняются только по явной команде владельца, после принятия read-only gates. Для первой штатной операции применяется существующий Worker strategy→permit→SDK→outbox→Core; подтверждаются terminal/partial fill, комиссии и отсутствие дубликата. При UNKNOWN сохранять резерв и permanent key, сначала read-only recovery; не создавать повторную заявку или принудительную сделку ради проверки. Deployment и локальные тесты не закрывают P5 live canary.
