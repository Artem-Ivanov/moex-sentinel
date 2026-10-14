# Находки — GitHub CI/CD

## Подтверждённое локально
- GitHub Artem-Ivanov/moex-sentinel; текущая и default ветка master; .github отсутствует.
- Python 3.12; Ruff/Black/pytest; фронтенд Node 24, npm ci, Vitest/typecheck/build. PostgreSQL integration fixtures берут POSTGRES_TEST_DATABASE_URL и создают отдельные UUID schemas.
- Compose использует локальный общий Python base; Dockerfiles сервисов FROM moex-sentinel-python-base:local. Base читает pyproject/uv.lock, но зависимости устанавливает pip из диапазонов pyproject — lock не определяет установленные версии.
- Общая инфраструктура PostgreSQL/collector принадлежит прежнему TEST Compose project и обслуживает PROD. Отключены только четыре TEST приложения. Whole-project down/volume prune не подходят.
- Одноразовые завершённые rollout scripts привязаны к конкретным receipts, runtime/owner IDs и phases. Для GitHub потребуется короткий универсальный tracked deploy entrypoint, переиспользующий реальные guards и Compose helpers.
- Последний production source был упакован из dirty checkout. Для CI пользователь сначала сохраняет и push-ит исходники; агенты этого не делают.
- Миграция 0005 и private Worker downgrade отказывают при сохранённом quota percent. Откат не может безусловно возвращать старый несовместимый Core/Worker.

## Предварительная схема
GitHub-hosted runner: проверки → сборка одного manifest с digest образов → GHCR. Ручной запуск PROD deployment готового manifest: ограниченный SSH entrypoint, host lock/journal, preflight/pull до остановки, штатная остановка writers, direct migrations, запуск совместимого комплекта, health/basic UI. ARM отдельно. Без backup/restore/целостности рабочей БД.

## Неизвестное
GitHub repo visibility/plan и доступность Environment required reviewers не проверены. Live runtime сейчас не проверяется; используются последние датированные receipts только как контекст. Предпочтение trigger пока не получено.

## Официальные источники GitHub — проверены 10.10
- Ручная кнопка Run workflow требует workflow_dispatch и файла workflow в default branch; запуск доступен пользователям с write access: https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow
- GHCR поддерживает публикацию из Actions с GITHUB_TOKEN; для внешнего приватного pull нужен PAT classic с read:packages; pull по digest закрепляет конкретные байты: https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry
- GitHub пример публикации Docker: https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images
- Concurrency позволяет один активный запуск; cancel-in-progress:false не отменяет выполняющийся deploy. Есть актуальная queue:max, однако минимальному ручному процессу очередь из 100 deploy не нужна. Host lock остаётся нужен против других операторов: https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency
- Для private repo environment secrets/branch restrictions требуют Pro/Team/Enterprise; required reviewers на Free/Pro/Team доступны только public. Не обещаем environment approval без проверки плана: https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments
- Security: минимальные permissions, pin Actions к полному commit SHA; не checkout untrusted PR в privileged workflow. CI для pull_request без PROD secrets; publish job только trusted push master: https://docs.github.com/en/actions/reference/security/secure-use

## Упрощение workflow
Два файла достаточны: ci.yml (PR/master проверки; master build/publish зависит от тех же проверок) и deploy-prod.yml (manual deploy/rollback по exact successful CI run/release manifest). Отдельный workflow_run для публикации не нужен. Manifest содержит source SHA, CI run/artifact identity, digests полного совместимого комплекта и schema/rollback targets; previous deployed manifest хранится также на VPS. Повторная сборка на VPS отсутствует.

## Доставка manifest и тестовый PG
- CI artifact скачивается по exact run ID; стандартная retention 90 дней. Deploy допускает только существующий manifest успешного trusted master run, не «последний artifact». Текущий/предыдущий deployed manifest на VPS сохраняются независимо от Actions retention: https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts
- PostgreSQL service containers поддерживаются Ubuntu Actions runners; они отдельны от PROD/TEST VPS: https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers
- deploy/remote/preflight.py проверяет release/source и compose pair/shared DB ownership, но не доказывает DISARMED/live worker owner/outstanding work. Нужен отдельный bounded runtime guard в reusable deploy; не путать с отвергнутой user postdeploy DB integrity.
- moex_sentinel.migrations.schema main вызывает upgrade(head) через Settings DATABASE_URL. Raw alembic.ini не является гарантией правильного PostgreSQL URL; rollout migration command должен использовать resolved contour env/network. Отдельный rollback entrypoint/target должен проверяться тестами.

## Проект решения для независимого ревью
1. Варианты: (A) hosted Actions + GHCR + bounded SSH deploy, рекомендуется; (B) hosted Actions + SCP image archives, меньше registry auth, но тяжелее перенос/retention; (C) self-hosted runner на PROD, теснее связан с runtime/secrets и ресурсами, для текущего VPS не выбирается. Автодеплой по merge — позднее изменение trigger при той же архитектуре, первая версия manual.
2. Два workflows: ci.yml PR/master проверки + publish trusted master after checks; deploy-prod.yml manual deploy/rollback, exact successful trusted CI run ID. PR не имеет PROD secrets/publish permissions; deploy definition на master, immutable manifest validation исключает подмену source/run/images.
3. CI Python3.12 Ruff/Black/pytest + PG16 service отдельной тестовой БД; миграции SQLite/PG upgrade/downgrade/refusal; frontend Node24 npm ci/test/typecheck/build; built-image READ_ONLY import/health probe с тестовыми конфигами. Runtime deps согласовать с lock, общий Python base собирать на одном hosted Docker daemon до child images.
4. Manifest: release/run/source SHA, полный совместимый набор service images по digest, source/config bundle identity и schema/rollback targets. Теги SHA удобны человеку, реально deploy по digest. Pull готовых образов до downtime, build на VPS отсутствует. Предыдущий successful manifest/image pins сохраняются на VPS независимо от Actions artifact retention.
5. Host root-owned forced-command deploy entrypoint, dedicated SSH authorized_keys restrict+command (запрет shell/SFTP/PTY/forwarding/user-rc), pinned host key; SSH user не может изменять wrapper/Compose/privileged executable files; inputs валидируются и не интерполируются как shell. Только известные операции/сервисы/registry prefix. Host flock + durable phase journal; общая Actions concurrency для deploy/rollback cancel-in-progress:false. Разрыв связи/повторный Run workflow не повторяет уже выполненные stop/migration/ownership phases вслепую.
6. Перед мутациями свежий DISARMED/owner/lease/outstanding-work guard. ARMED/pending → отказ до изменений и отдельное штатное quiesce. Writer shutdown: Worker graceful, затем Core/необходимый shared collector кратко для schema migration; shared PostgreSQL остаётся running, volumes/resources/ports сохраняются. Exact services up --no-build --no-deps; TEST приложения не возвращаются.
7. Direct migration через фактически разрешённый PROD DATABASE_URL/network, не raw alembic.ini. Compatible Core/Worker/schema pair, durable Worker SQLite сохраняется. Typed stopped-owner proof и поколение владения; STOP_REQUIRED/recovery_only после передачи, не auto ARM.
8. Smoke health/ping, SPA/login/assets и базовые readonly API из VPS с private existing auth. Ни секреты, ни cookies в GitHub artifacts/logs. Дополнительная целостность/financial graph/backup/restore не входят.
9. Rollback выбранного previous compatible manifest + tested paired Core/Worker scripts. При refusal downgrade сохранить head-compatible app/data; оба schema targets журналируются отдельно, writers остаются остановленными при любом partial rollback; до запуска любого приложения вернуть обе схемы к проверенному совместимому target, при невозможности оставить writers остановленными; не безусловный auto old tag. Публичный PROD443/8443off и TESTretirement сохраняются.
10. Secret placement: VPS broker/DB/auth env остаётся private; GitHub только deploy SSH credential/host metadata и ephemeral GITHUB_TOKEN publish. Private GHCR pull PAT classic read:packages хранится на VPS. Actions pin full SHA. GitHub Environment reviewers лишь если тариф/visibility позволяют, ручной trigger не зависит от них.
11. Минимальные будущие изменения: два YAML workflows, tracked reusable deploy entrypoint+manifest/image override contract, существующие Compose/preflight/build deps/probe/rollback scripts adaptation и операционная инструкция. Current dirty deployed source сначала commit/push пользователь. Никакой реализации сейчас.

## Архитектурный read-only результат quota_core
- Переиспользовать compose.sh/compose.production.sh, их операции/hash/image-ID guards и preflight.py. Полный release bundle содержит metadata frontend и source operations, иначе source release validation не пройдёт.
- Image-only override: digest-aware inputs либо pull digest → immutable release local tag → verify image IDs; up/run --no-build --no-deps только named targets. Не добавлять второй database/collector.
- PROD Compose удаляет migrations/collector/database; migration запускается одноразово existing shared-owner migrations service с --no-deps или equivalent release Core image с explicit resolved PROD URL/data network. Ни один TEST application не запускается. Core entrypoint sets Alembic URL из MigrationSettings; default без env — SQLite.
- CI собирает общий Python base → Core/Worker/Analytics/UI/migrations/collector на hosted daemon; source/probe/schema/worker rollback utilities фиксируются hashes. Core downgrade runner ещё отсутствует; Worker rollback helper должен войти в versioned release artifact, не зависеть от ignored develop на VPS.
- Итоговые digests закрепляют собранные байты; не обещаем identical rebuild, пока весь build input не pinned/lock consistent. Private env/resources/volumes/routes сохраняются.

## Независимое ops review — замечания и закрытие проектом
- rollout_ops_review I1: restricted SSH без server-enforced forced command может дать обычный shell при утечке key. Проект дополнен root-owned authorized_keys command+restrict, запретом shell/SFTP/forwarding/PTY/user-rc и write к исполняемым операциям.
- rollout_ops_review I2: Core PG и Worker SQLite rollback не атомарны. Проект явно держит writers stopped и журналирует обе schema targets; до любого старта восстанавливает совместимый target обеих схем. При невозможности automatic start запрещён. Reviewer подтвердил, что первоначальная partial-head repair закрывала смысл замечания; дополнено явное состояние остановки.
- Final re-review запрошен после этих уточнений. Финального PASS ещё нет.

## Итог кросс-ревью — 15:14 МСК
rollout_ops_review перепроверил пункты5/9 после исправлений: C0/I0/M0, открытых замечаний нет. Область: проект CI/CD и связанные контракты; без реализации/tests/runtime. Архитектурный анализ quota_core учтён. Ограничения SSH дополнительно подтверждаются https://man.openbsd.org/sshd.8 (authorized_keys restrict и command).

## Согласованный trigger — 10.10.2026 18:40 МСК
Ответ пользователя на optional async question: «Вручную кнопкой Run workflow». Ручной запуск PROD выбран; ранние записи pending описывают время до ответа. Остальной design/implementation не утверждены этим ответом. Decision сохраняется также docs/deployment/remote-compose.md в разделе обновления.
