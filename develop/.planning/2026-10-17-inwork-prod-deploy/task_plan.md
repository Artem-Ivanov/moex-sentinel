# PROD rollout — admission по IN_WORK — 17.10.2026

## Цель и scope
По явному запросу пользователя убрать реальные TEST-заглушки из production code path, сверить полноту реализации PROD по всему торговому потоку и только после устранения подтверждённых пробелов выпустить изменения на PROD с обычной технической приёмкой. Сохранять настоящий Sandbox/Test contour и тестовые doubles в tests; считать заглушкой только production wiring/ветку, которая подменяет, пропускает или делает неполным обязательное PROD поведение. Любая возможная торговля после релиза должна идти только по существующему `IN_WORK` + TRADE и уже заданным общим финансовым лимитам.

## Проверка актуальности до выполнения
- Кодовая веха 16.10 заявлена локально завершённой; общий independent review `C0/I0/M0`, Python 2515 PASS/196 PostgreSQL SKIP, frontend 257 PASS/typecheck/build PASS. См. `develop/reports/position-driven-admission-20261016/review-integration.md` и root logs.
- Текущий runbook говорит, что PROD остаётся на `vps-20261015-env-4a71589e8766` до отдельного релиза; он требует согласованный Core/Worker/UI, точный source manifest, сохранение volumes/HOLD/config и ограниченную health/UI приёмку.
- В checkout 66 изменённых tracked files + untracked docs/tests/plan и пользовательский PNG; HEAD/index не содержат локальную реализацию. Запрещены stage/commit/push и нельзя случайно включить PNG/секреты/нецелевой scratch.
- Изменение не содержит DDL/миграции. PostgreSQL-only тесты до релиза не прошли (отдельная TEST DB недоступна); блокировка/конкурентные свойства PG остаются ограничением.

## Фазы и gates
1. [done] Сверить release inputs с reviewed source, состав package и безопасные rollout wrappers; проверить локальную доступность build/SSH, не раскрывая credentials.
2. [pending] Сформировать уникальный tag/archive/source manifest только из явно перечисленного продукта, провести preflight против текущего PROD (release/schema/containers/config/trading state/active intents; read-only), затем независимое pre-execution review. Code-only prep разрешён при caps=null и ровно известной сохранённой UNCERTAIN; перед rollout остановиться при любом runtime drift, новых неопределённых intents, изменении HOLD/IN_WORK/лимитов или отсутствии clean-stop proof.
3. [pending] После review подготовить immutable Core/Worker/frontend images без изменения private ENV, caps, DB/volumes. Analytics/schema остаются прежними; миграции нет. Обновления compose должны быть точно объяснены и ограничены согласованным приложением/runtime defaults.
4. [done] Scoped operator-authenticated read-only GET для единственного PENDING runtime; strict intent-state allowlist; honest `NOT_RECHECKED` boundary; fail-closed admission/null-caps/arm/recovery checks; stable UNCERTAIN reference binding; lifecycle/failure-injection tests. Independent `env_core_reviewer` verdict C0/I0/M0: `develop/reports/position-driven-admission-deploy-20261017/protocol-fix-review.md`.
5. [in_progress] Новая пользовательская prerequisite-веха: независимая инвентаризация TEST-specific defaults/no-op/fallback и production composition; классифицировать допустимые Sandbox/test code paths vs реальные incomplete PROD paths; согласовать scope/design/DoD перед реализацией, выполнить только подтверждённые правки и тесты, independent cross-review.
6. [pending] Пересобрать immutable package с новым API и принятыми production fixes, получить свежее разрешённое application-level intent receipt, повторить exact preflight, выполнить deployment и ограниченную техническую приёмку. Не запускать STOP без свежего receipt; PROD DB/SQLite напрямую не читать.
7. [pending] Независимое post-deploy cross-review receipts; обновить tracked runbook/очередь и локальные CURRENT/WORK_LOG с точной границей того, что проверено.

## Acceptance / stop conditions
- Новый RELEASE_TAG / images и точные source/archive hashes подтверждены.
- Никаких изменений private env, численных значений четырёх caps, HOLD, финансовых строк/заявок/исполнений, schema/volumes, nginx/auth или TEST/shared services. Ожидаемы и проверяются отдельно только текущая дневная baseline, owner/generation/lease и admission revision/status, которые новый Core штатно готовит при старте/передаче.
- Worker clean stop и штатный owner/lease переход проверены непосредственно перед rollout; carry-through разрешён ровно для сохранённой UNCERTAIN intent `022634125f04`, любые новые/изменившиеся неизвестные операции — стоп. Не трактовать отсутствие broker ID/fill как разрешение.
- После rollout все целевые сервисы здоровы, endpoint/UI базово доступны, стратегия/режим/лимиты совпали с preflight; подтверждена только техническая готовность, не фактическая торговля.
- Не коммитить, не пушить, не менять Git index/ветки; сохранить пользовательский PNG.

## Открытые ограничения
- Есть свежий read-only observation на 17.10 в `observation-before.json`; потребуется новый перед остановкой Worker.
- Не проверены 196 PostgreSQL integration tests без отдельного тестового сервера.
- Текущий локальный checkout dirty; деплой допустим только из exact reviewed manifest, без упаковки всего git status.
- Предыдущий релизный helper для 15.10 ожидает STOP_REQUIRED/recovery-only и limits=null после owner transfer, что противоречит новой логике IN_WORK и не может быть запущен без адаптации и cross-review.
- Старый Core/Worker `vps-20261015-env-4a71589e8766` допускается как rollback только при точной сверке, отсутствии новых grants/position-anchor JSON/cap writes и сохранении исходной UNCERTAIN; иначе только forward-fix с сохранением state, без downgrade/очистки.
- Штатная runtime registration возвращает UUID только Worker; до owner transfer Core diagnostics не обновляются. Независимое review helper выявило I1 на этом шаге. Production DB lookup запрещён правилами проекта; ни один сервис не остановлен. Решение и доказательства — в progress.md и отчёте rollout helper; перед следующим deployment сохранить протокол в tracked cutover runbook.
- Дополнительный independent review выявил I2: исходный helper допускал другие активные intent counts вместе с одной известной UNCERTAIN и выдавал старый Worker snapshot за текущее состояние; также lifecycle review M1. До rollout закрыть I2 строгим intent-state allowlist и не заявлять post-stop row-state verification без разрешённого источника; добавить lifecycle tests и повторное независимое review.
