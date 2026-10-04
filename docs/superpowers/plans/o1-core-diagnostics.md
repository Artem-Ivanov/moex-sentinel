# O1.1 — план реализации диагностики Core

> Для исполнителей: использовать superpowers:subagent-driven-development,
> test-driven-development и verification-before-completion. Пользователь выбрал
> исполнение через роли .agents; commit делает владелец. Рабочие отчёты и ledger
> находятся в develop/reports/o1-status-20261003/, а не в .superpowers/.

**Цель:** защищённый диагностический снимок Core и страница ручного просмотра.
**Архитектура:** existing readiness + captured runtime → usecase → auth GET → Vue.
**Стек:** Python/FastAPI/Pydantic, Vue3/TypeScript/Vitest; без новых dependencies.
**Spec:** [o1-core-diagnostics-design.md](../specs/o1-core-diagnostics-design.md).

## Общие ограничения

- Снимок строго по контракту spec; общий status UNKNOWN при готовом Core.
- Никаких live API/SSH, рабочей БД, trading commands, Worker storage migration.
- Проверки только synthetic SQLite; PG gate не нужен без изменения persistence.
- Исполнители не stage/commit/push, не меняют чужие файлы и общий журнал.
- Текущие37 dirty/untracked файлов сохранены в baseline.json; чужой delta не ревью
  этой вехи, но его неизменность проверяется при интеграции.
- Изолированный worktree develop/reports/o1-status-20261003/worktree отражает
  исходный checkout со всеми37 изменениями; .env/секреты туда не копировались.
- Модели: архитектура/review gpt-6.1-sol high, оба разработчика medium.

## Фокус ревью

Auth/no-store и sensitive exception suppression; UNKNOWN не превращается в OK;
capture clock/runtime не выдаются за heartbeat; синхронный SQL вне event loop;
ошибка/abort UI не оставляет старое подтверждение и не вызывает polling.

## Задачи и владельцы

| ID | Владелец | Результат и зависимость | Статус |
| --- | --- | --- | --- |
| A1 | moex_architect | Source trace и контракт O1.1 до реализации | DONE |
| R0 | moex_reviewer | Проверка spec/plan до реализации | PASS |
| B1 | moex_backend_developer | Защищённый status API и backend tests | DONE |
| F1 | moex_frontend_developer | API client, страница, навигация, UI tests | DONE |
| R1 | moex_reviewer | Пакеты и финальная интеграция/документы PASS | DONE |
| I1 | оркестратор | Root tests/интеграция и финальное ревью PASS | DONE |

B1 и F1 выполняются параллельно по фиксированному spec, область записи не
пересекается. R1 проверяет пакеты по готовности и затем объединённое состояние.

### B1 — API (единственный владелец Python API-контракта)

Create: src/moex_sentinel/usecases/diagnostics.py,
src/moex_sentinel/views/diagnostics.py, tests/usecases/test_diagnostics.py,
tests/api/test_diagnostics.py. Modify: src/moex_sentinel/api/app.py.

Produces: GetDiagnosticsStatusUsecase.execute(), JSON GET по spec. Consumes:
CheckReadinessUsecase.execute(), captured Settings и utc_now_ms/injected clock.

- [x] Написать RED тест: anonymous401, authenticated200/no-store; status absent
  сейчас404; три readiness состояния с точными schema/reason/status значениями.
- [x] Реализовать минимальный usecase/DTO/view/lifespan wiring, sync handler.
- [x] Проверить safe errors, UTC/ms, no schema call при DB down, old health503,
  captured runtime при изменении env после create_app, отсутствие broker calls.
- [x] GREEN focused pytest + API health/auth regression и Ruff/Black по files.
- [x] Отчёт backend.md с RED/GREEN командами/counts, diff/sha и ограничениями.

Commands (worktree cwd): BROKER_ACCESS_MODE=READ_ONLY PYTHONPATH=src .venv/bin/pytest
-q tests/usecases/test_diagnostics.py tests/api/test_diagnostics.py
tests/api/test_health.py tests/api/test_auth.py. Необходимая регрессия API выбирается
по фактическому diff; lint только созданные/изменённые Python files.

### F1 — интерфейс

Create: frontend/src/api/diagnostics.ts, diagnostics.spec.ts,
frontend/src/views/DiagnosticsView.vue, DiagnosticsView.spec.ts.
Modify: frontend/src/router.ts, App.vue, App.spec.ts.

Produces: fetchDiagnosticsStatus(signal?), route diagnostics и ссылка «Диагностика».
Consumes: status JSON spec, существующий apiFetch/auth guard, existing UI classes.

- [x] RED на маршрут/навигацию и получение снимка; backend ещё не нужен (synthetic fetch).
- [x] Реализовать типы/client/view и ручное обновление; точные состояния по spec.
- [x] Проверить network/non200/401, UNKNOWN при CoreOK, DBdown/schemanotready,
  очистку снимка при refresh/error, disabled кнопку, abort/late response/no polling.
- [x] GREEN focused Vitest, полный frontend при интеграции, typecheck и build.
- [x] Отчёт frontend.md с RED/GREEN, командами/counts, diff/sha и ограничениями.

Commands: npm --prefix frontend test -- src/api/diagnostics.spec.ts
src/views/DiagnosticsView.spec.ts src/App.spec.ts; npm --prefix frontend run
typecheck; npm --prefix frontend run build. Если npm недоступен, использовать
установленный node и локальные scripts без установки новых dependencies.

### R0/R1 — независимая приёмка

Read-only .agents/moex_reviewer.toml. Проверить spec/контракты/SOLID/чистоту/
поведенческие tests, actual diff + новые файлы, severity/place/scenario/fix.
Автор исправляет Critical/Important; reviewer повторно проверяет актуальный diff.
Отчёт координатор сохраняет в review.md; не объявлять PASS по прежним версиям.

### I1 — контроль интеграции

- [x] Root сверяет frozen manifests и реальные результаты авторов, прогоняет
  auth/health/newAPI и весь frontend/type/build, diffcheck; backend full если
  интеграция выявила дополнительный риск или required gate.
- [x] Переносит только7FE+5backend files после проверки, что исходные файлы в
  primary workspace не изменились; prior source/тесты сохраняются побайтово; AGENT_BRIEF/очередь дополняются
  оркестратором по этой задаче, прежний текст сохраняется.
- [x] Reviewer проверяет итоговую интеграцию и docs; root обновляет очередь,
  CURRENT/WORK_LOG/ledger и передаёт готовый локальный patch владельцу.
- [x] Worktree сохраняется до owner commit; никаких незапрошенных deploy/TRADE.

Критерий DONE: все B1/F1/R1/I1 checks PASS, открытых существенных замечаний нет.
Отдельный статус deployment NOT_DEPLOYED; полная O1 остаётся OPEN.

## Результат локальной приёмки

03.10.2026: DONE / NOT_DEPLOYED. Root66API/usecase и139FEtests PASS,
typecheck/build/Ruff/Black/diffcheck exit0. Независимый /root/o1_reviewer:
CODE/DOC PASS, Critical/Important/Minor0. Exact12files интегрированы;35prior
filesSHA сохранены,2contextdocs намеренно дополнены. Evidence в
develop/reports/o1-status-20261003/{root-verification.json,integration.json,final-review.md}.
Полный backend/PG suite, VPS/live browser/брокерский API не проверялись; полная
O1/O2/S1/M1 OPEN. Commit делает владелец; deployment после его отмашки.

## Deployment receipt — 03.10.2026

O1.1 status API deployment принят по
`develop/reports/vps-release-20261003/root-native-receipts.json` (commit
`be84ed4f047734ccacfba9b6ccb531af51ba4b94`, release `vps-20261003-be84ed4`,
17:14 UTC); applied schema `0003_user_broker_archive`. Отдельная post-deploy HTTPS
verification подтвердила diagnostics/status API и защищённые auth GET в TEST и PROD.
Observed Core TEST/PROD версии `0.2.0`; TEST Worker OBSERVED `0.2.0`, PROD Worker
UNKNOWN; TEST Analytics OBSERVED `0.2.0`, PROD Analytics NOT_CONFIGURED. UI build version
`0.2.0`. Browser acceptance не проводилась; отдельная архивная запись отражена только
агрегатным read receipt с неизвестным actor и не является частью O1.1 verification.
Версии/UNKNOWN не подтверждают торговую готовность. O1.1 deployment принят в пределах этих receipts;
full O1/O2/S1/M1, 24h/7d, PROD Worker и TRADE остаются OPEN.

## O1.2: control loop и outbox

Статус04.10: LOCAL DONE / NOT DEPLOYED. Актуальность подтверждена
root/`o12_relevance_architect` по коду; пользователь выбрал O1.2 вместе с M1.1
в первый patch. Предыдущие O1.1/V1 receipts сохраняют самостоятельный scope.

### Задачи и исполнители

1. Root: baseline/DoD/briefs/decomposition ACK; общая интеграция и контекст.
2. Senior `o12_backend_senior`, sol/medium: shared diagnostic DTO и existing
   heartbeat/Worker coordinator/outbox scalar query/Core atomic snapshot/read API.
   Единственный writer11backend source и9associated test files; согласованный
   список в local task-plan/briefs. Accepted PROD BrokerConnection поля сохранить.
3. Junior `o12_frontend_junior`, luna/low: existing diagnostics API types/view и
   их2spec files; четыре sole-owned файла. Страница и navigation уже существуют.
   No RAM dashboard, polling, dependencies или новых routes.
4. Независимый `o12_relevance_architect`, sol/high: actualfrozen code/contract/
   integration/performance methodology review; C/I исправить и перепроверить.
5. Root: fresh nonPG+frontend regression/typecheck/build/lint/diff, actualhashes,
   scalar aggregation benchmark, truthful context/doc acceptance. Deployment позже
   по commit и GO владельца, новый Core прежде нового Worker.

### DoD

- Completed control sync увеличивает counter/time; оба flush blocks не делают
  fake success; ошибка до broker session видна через heartbeat ERROR.
- Cancellation и telemetry failure не изменяют финансовый flow/primary error;
  один heartbeat task/3s, без нового lifetime timer.
- Outbox одна bounded scalar projection без payload/entity materialization,
  no mutations/retries changes. READ_FAILED UNKNOWN/null; realzero counts0.
- Shared identity/scope, legacy heartbeat; Core lock/volatile TTL30s/monotonic,
  strict newer heartbeat и допустимыйexisting5s clockskew, nestedtime/count
  consistency. Wrong scope/naive/old/future/rollback не продлевают TTL.
- Fresh frozen progress DEGRADED, stale/missing UNKNOWN; новый run без oldsuccess.
  Failed>0 или oldestpending>=60s DEGRADED; no overall tradingOK.
- UI oldpayload/fresh/error/blocked/stale/queuefail/nullzero/manualrefresh/auth,
  версия отдельно от control progress. No secrets/extra technical userflows.
- Изолированные tests/benchmark, независимое review и личная root acceptance.
  PG/Linux/liveRuntime только отдельными gates; не выдавать sourcePASS за VPS.

### Границы

SQLite durablehistory/intents/outbox/ACK, Core PostgreSQL/sharedDB, Analytics
market-only, readonly mode/strategyfalse/PRODTRADE hardguard сохранены. No schema
migration/Redis/Rabbit/SDKcalls/изменениятокена/допускаторговли. ПолныеO1/O2/S1/M1
остаютсяOPEN. Локальныедоказательства: develop/reports/o12-worker-diagnostics-20261003/
и root orchestration develop/reports/patch-o12-m1-20261003/.

### Локальная приёмка O1.2

Root лично прочёл diff и проверил frozen SHA; прежние PROD connection поля,
bootstrap tests/UI и пользовательский dirty набор сохранены. Fresh полный
backend без PostgreSQL1512 PASS/17warnings; frontend158 PASS/typecheck/build;
Ruff/Black22files, release metadata и diff PASS. Первый общий прогон1510PASS/2FAIL:
слово в документации нарушало architecture gate; миграция Alembic отключала
logger и ломала новый warning-capture test. Документация и изоляция testlogger
исправлены, actual order RED→GREEN, независимо перепроверены; продуктовая
финансовая логика не менялась. Независимый `o12_relevance_architect` проверил
backend/FE/shared contracts, C/I/M0; native/PG/runtime этим не подтверждаются.

Backend выполнял RED/GREEN по пакетам. Frontend сначала реализован до рабочего
NodePATH; затем отдельно воспроизведён baseline9FAIL/18PASS→candidate27PASS.
Это доказательство поведения, не строгая исходная TDD хронология. M1.1 имеет
отдельные code/memory доказательства; latency и fullM1 остаются OPEN.
