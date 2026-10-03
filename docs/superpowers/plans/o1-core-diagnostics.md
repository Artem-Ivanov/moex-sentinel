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
