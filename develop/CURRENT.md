# Текущая точка продолжения

Обновлено 02.10.2026. Сверять checkout и runtime; исторические отчёты не подтверждают текущее состояние.

## Активная задача

Переход T-Invest TEST→PROD поэтапно READ_ONLY→отдельный допуск торговли. Пользователь уточнил: одна PostgreSQL/одна БД для сервисов, без отдельной PROD БД. Сейчас прежде дальнейшей реализации нужен целостный архитектурный анализ: кто оркестратор, какие данные Worker восстановимы из брокерского API, можно ли убрать локальное хранилище. Redis/RabbitMQ пока обсуждаются, перенос Worker и замена HTTP не выбраны.

## Подтверждённое и незавершённое

- Базовый код P1/P2 интегрирован в dirty checkout с сохранением исходных изменений и index. Независимый `/root/prod_integration_review_retry` code PASS. Backend: 1378 PASS + отдельный PASS оставшегося documentation gate и PG concurrency; frontend119 PASS/build0. Детали и ограничения: `develop/reports/prod-api-20261002/`.
- Первоначальный отдельный PROD prototype прекращён после уточнения владельца: 02.10 08:03 UTC остановлены только его frontend/backend/database, Nginx8443 отключён, volumes/credentials сохранены; Sandbox не затронут. Evidence `shared-db-transition-stop.json`. Прежняя operational приёмка отдельной БД superseded; PROD с общей БД пока не развёрнут.
- Shared Core PostgreSQL scout нашёл недостающие contour guards: broker mutations, command/status queries, fact ingress, summary и collector. Их реализация ещё не начата. Sandbox r2 нужно обновить до первой PROD записи в общей БД.
- По отдельному поручению реализовано подавление одинакового успешного reconciliation audit до создания sequence/outbox; сама сверка выполняется каждый проход. Автор `/root/prod_worker_impl`: 81 focused/regression PASS; independent review `/root/prod_integration_review_retry` PASS/81, root44 PASS. Все6 файлов интегрированы с SHA256, index неизменен; deployment ещё открыт.
- Worker SQLite — durable execution state: intents, allocations/lots/cycles, outbox, состояние стратегии. Торговый оркестратор Worker, Core хранит конфигурацию и принятый префикс. Full lost-DB restore из Core/broker не реализован; API даёт текущие финансовые данные, но не internal IDs/strategy/delivery. Итоговый root анализ `docs/audits/2026-10-02-orchestration-and-worker-storage.md`; crossreview UIagent PASS, независимый integration reviewer PASS после двух исправленных Minor (analysis/docs/dedup integration). Report: `develop/reports/architecture-storage-20261002/independent-review.md`. Рекомендация одной PG/ownWorker tables пока не утверждает перенос.
- Реальный PROD токен не настроен; READ_ONLY network gate с неверным синтетическим токеном подтвердил TLS/gRPC, не авторизованные чтения. PROD TRADE запрещён до лимитов и отдельного допуска.

## Runtime, доступ, данные

VPS `codex-deploy@135.136.178.252`, временный sudo действует. Sandbox UI `https://135.136.178.252/`, оператор `operator`. Секреты root-only; их содержимое не выводить. Sandbox release `/opt/moex-sentinel/current`→`vps-20261001-sandbox-ui-r2`; стратегия false. Последняя подробная read-only проверка: 6 holdings, catalogue4332/2460 RUB, intents/orders/fills0, ACK delivery и браузер PASS — `develop/reports/vps-audit-20261002/`; перед новым runtime выводом повторить проверку.

Перед upgrade нужен согласованный PG+Worker SQLite backup и restore на отдельном тестовом экземпляре. Ранее создан PG dump до bootstrap, restore не выполнен. Volumes не удалять, локальный Docker не запускать. SSH/sudo закрыть после всех разрешённых серверных задач.

## Следующий конкретный шаг

Передать независимо проверенный анализ пользователю. Затем выполнить разделение Core контуров в одной БД, paired backup/restore и новую live приёмку с dedup; решение о переносе Worker принять по карте владения, не заменять durable данные кэшем. Общая регрессия старого P1/P2 не подтверждает пока не реализованный sharedDB ingress/collector.

O1/O2 наблюдаемость и S1/S2 хранение ещё открыты: короткий рост Core условно24,55/147,28GiB за30/180дней не заменяет24ч/7дней замеров. Retention/история не очищались.

## Источники и ограничения

Актуальная очередь: `docs/phase-1-implementation-plan.md`; постоянные границы: `AGENT_BRIEF.md`. PROD design/план/runbook и AGENT_BRIEF обновлены по одной БД; runbook явно закрыт до исправления старых Compose/env/preflight. Worktree docs пока прежние — источником актуальных архитектурных поправок являются root docs и audit report. Исходная расширенная точка продолжения сохранена в `develop/reports/prod-api-20261002/current-before-storage-audit.md`; история в `develop/WORK_LOG.md`. Auth backend/frontend на VPS реализованы ранее, кросс-ревью PASS; 0.9.8 ранее принята (1334 PASS отдельной PG), R0 и restart acceptance остаются открыты. Пользовательские staged/unstaged изменения не коммитить/не стадировать.
