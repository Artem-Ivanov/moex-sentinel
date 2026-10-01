# Текущая точка продолжения

Обновлено 2026-09-28 (МСК). Перед действиями сверить checkout и текущий
runtime: исторические отчёты не подтверждают состояние сервисов сегодня.

## Веха 0.9.8 принята

W1, W2a, W2b и V0 завершены. Сквозные Worker SQLite → HTTP → отдельный Core
PostgreSQL тесты покрыли bootstrap, selective ACK, retry-before-due, обычную
доставку и потерю ACK с restart/replay. Backlog 100/10k/50k завершился без
потерь: 1/10/50 запросов, нулевой outbox, равные конечные sequence/revision.
На двух seeds old/current selector drain совпал по count, остатку и порядку;
второй seed — 9/9 persisted пар. Adversarial cap глобальных кандидатов
исправлен после RED (peak 47 при пределе 30). Суммарный bootstrap metadata
stream на many-bootstrap 50k — 1 274 970 строк; внутренние чтения SQLite
не измерялись.

Финальный backend pytest на отдельной PostgreSQL: **1334 passed, 0 skipped,
17 warnings, exit 0**. Ruff, Black и `git diff --check` прошли.
Независимые `/root/review_v0`, `/root/review_drain`, `/root/review_pg_backlog`
и итоговый `/root/review_v0_integration` — PASS после исправления замечаний;
финальные документы также прошли независимую проверку. Тестовый контейнер
`moex-sentinel-v0-test-pg` остановлен, он не имел mounts. Рабочие Compose,
БД и persistent volumes не затрагивались.

Подробности и ограничения — [приёмка 0.9.8](../docs/milestone-0.9.8-outbox-delivery.md)
и локальный `develop/reports/0.9.8-v0/README.md`; полный лог —
`develop/reports/0.9.8-v0/pytest-final-v2-postgresql.txt`.

## Следующий шаг

R0 — отдельная открытая оперативная проверка свежего runtime после recovery.
Снимок 19.09 не подтверждает текущую торговлю. Перед R0 прочитать относящиеся
к ней пункты [актуального плана](../docs/phase-1-implementation-plan.md) и
[границы сервисов](../AGENT_BRIEF.md), затем проверить текущие сервисы
read-only. Никаких действий по восстановлению торговли без отдельного анализа
и соответствующего скилла.

Существующие пользовательские изменения в dirty checkout не стадировать и не
коммитить без отдельной задачи. История сессий — в `develop/WORK_LOG.md`.
