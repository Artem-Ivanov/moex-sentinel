# Проектирование GitHub CI/CD — 10.10.2026

## Цель и авторизация
Предложить минимальную конкретную схему CI/CD для текущего Docker Compose/VPS по запросу «Нужно подумать как организовать CI/CD в гитхаб для раскатки». Только исследование и проектирование; workflows, SSH, deployment, GitHub secrets и торговые операции не разрешены этим запросом.

## Свежая сверка вехи root — 15:08 МСК
- Код: GitHub remote Artem-Ivanov/moex-sentinel, master; .github отсутствует. Docker Compose и deploy/remote уже есть; значительная часть последнего релиза в dirty checkout. GitHub может собрать только сохранённый пользователем commit.
- Выполнено ранее: quota rollout и TEST retirement приняты 10.10; CURRENT и docs/deployment описывают последний проверенный runtime, не свежую проверку сейчас.
- Устаревшее: одноразовые frozen rollout helpers не являются универсальным CI deploy entrypoint; сборка на VPS и TEST-приложения не нужны будущему процессу.
- Ограничения: direct migration + проверенный rollback, без обязательных backup/restore/проверки целостности рабочей БД; smoke health/ping/basic UI. TEST-приложения выключены; общий PostgreSQL/collector сохраняются. Торговый ARM отдельный допуск. Ветки/stage/commit/push запрещены агентам.
- Недостающее: GitHub trigger preference (задан необязательный вопрос), immutable release manifest, общий deploy entrypoint и изолированные CI проверки. Docker Python base использует local tag и диапазоны зависимостей.
- Источники: develop/CURRENT.md; docs/phase-1-implementation-plan.md:9–65; AGENT_BRIEF.md:73–77; docs/deployment/remote-compose.md:497+; pyproject.toml, frontend/package.json, compose.yml, docker/*.Dockerfile; git status и sanitized remote metadata.

## Scope и DoD
Выбрать подход; описать CI, сборку/публикацию, запуск PROD, секреты, миграции/rollback и smoke; назвать необходимые изменения существующих файлов. Проверить текущие возможности по официальной документации GitHub/Docker. Независимый агент проверяет проект на сохранение контрактов и ограничения эксплуатации; существенные замечания закрываются. Результат сообщается пользователю и сохраняется в локальном контексте. Продуктовый код не меняется.

## Фазы
1. Сверка проекта и границ — выполнено.
2. Read-only анализ существующего deploy и официальных docs — выполнено.
3. Выбор минимальной схемы и независимое кросс-ревью — выполнено.
4. Проект решения подготовлен к передаче, контекст обновлён — выполнено.

## Следующий шаг
Пользователь выбрал manual Run workflow; остальной проект остаётся предложением к оценке. Реализация не начата; для первого GitHub релиза пользователь сохраняет/push-ит current source. Отдельное продолжение — approved design/spec и implementation plan; не выполнять deploy/ARM автоматически.

## Кросс-ревью
quota_core: read-only проверка текущих Compose/Docker/source guards и migration/rollback entrypoints. rollout_ops_review: независимые ops/security/artifact/ownership/rollback invariants, initial I1/I2 исправлены в проекте и повторно проверены; final C0/I0/M0. Это review проекта решения, не реализации и не fresh runtime. 10.10 в 18:40 МСК получен ответ пользователя: Run workflow вручную. Зафиксирован trigger preference; workflows ещё не созданы.

## Уточнение пользователя — 10.10.2026 18:40 МСК
Root сверил CURRENT, git status и текущий раздел обновления: ответ выбирает только ручной trigger, не расширяет scope до внедрения/SSH/deploy. Изменение ограничено записью согласованного trigger в инструкции, плане и локальном контексте; DoD — отсутствие прежнего pending в актуальных указателях и независимое doc review. Продуктовый код/runtime/Git не меняются.

Независимый rollout_ops_review проверил manual-trigger абзац docs/deployment/remote-compose.md:499 и согласованность CURRENT/plan: C0/I0/M0. Изменения только документации; разрешение до implementation/deploy не расширено.
