# V1 — версии сервисов, актуализировано 03.10.2026

## Цель и проверка актуальности

Оператор видит собственную build-time версию frontend и фактически полученные
версии Core, Worker и Analytics. Сборка/выпуск блокируются при рассогласовании
метаданных внутри пакета. Текущая версия обоих пакетов 0.2.0; их независимые
следующие версии допустимы. Metadata/Core этап уже выполнен, без повторной работы.
Проверка checkout/очереди/договорённостей/зависимостей/DoD архитектором и root:
develop/reports/milestone-relevance-20261003/decision.md. V1 актуальна и локально
принимается целиком; полные O1/S1/M1, LIVE приёмка и deployment остаются отдельно.

## Контракт и ограничения

- Общая Python версия `SERVICE_VERSION` в `sentinel_contracts.version`;
  `moex_sentinel.__version__` — совместимый alias. Analytics не импортирует Core.
- Старый heartbeat worker_id/occurred_at и его ACK сохраняются. Optional nested
  `runtime_version`: version (safe MAJOR.MINOR.PATCH), UUID instance_id, environment
  TEST/PROD, access_mode READ_ONLY/TRADE. Worker создаёт instance один раз в CoreClient.
- Core хранит один volatile version observation, принадлежащий этому Core runtime,
  без БД/истории. Контур/режим должны совпасть с captured settings. Older/duplicate
  heartbeat не продлевает TTL; future >5s/возраст входящего heartbeat >=30_000ms не создаёт
  свежее observation.
  Server UTC и monotonic clock; age >=30_000ms UNKNOWN, version=null. После restart
  Core UNKNOWN. Само наличие версии не подтверждает готовность Worker/стратегии.
- Analytics `/health`: status=ok, service=analytics, shared version. Core получает
  version только с captured явного ANALYTICS_URL этого контура, private GET без
  credentials/proxy env/redirects. Не задан URL — UNKNOWN. Общая deadline 1000ms,
  body <=4096bytes; invalid status/JSON/service/version, timeout и ошибки дают
  safe UNKNOWN. Настройки URL не управляются внешним запросом.
- Diagnostics добавляет `service_versions.worker/analytics`:
  observation=OBSERVED|UNKNOWN, version=null|string, received_at=null|UTC-ms-string,
  age_ms=null|nonnegative integer, reason (allowlist). Worker: OBSERVED/NOT_OBSERVED/
  STALE; Analytics: OBSERVED/NOT_CONFIGURED/UNAVAILABLE/INVALID_RESPONSE.
  Core/runtime/status/awaiting_observations и auth/no-store остаются совместимыми.
  Healthy Core по-прежнему общий UNKNOWN; O1 trade observations неизвестны.
- UI отдельно подписывает версии интерфейса/Core/Worker/Analytics. Собственная
  версия не зависит от сетевого ответа; устаревшая/неизвестная версия не показана
  текущей. Manual refresh/abort/safe errors сохраняются.
- Stdlib release guard сравнивает pyproject.version / moex-sentinel entry uv.lock /
  shared literal; проверяет alias moex_sentinel.__version__. Shared version module
  допускает только module docstring и одно простое literal assignment; Core init
  сохраняет единственный shared alias, без дополнительного binding его версии. Для frontend сравнивает
  package.json / root package-lock / packages[''].version. Python/npm равенство
  не требуется. Missing/duplicate local lock entry и mismatch блокируют release.
  Python base Docker build вызывает guard с --python-only, frontend Docker build
  вызывает Vite npm gate, remote preflight проверяет оба пакета полностью. Vite
  build блокирует npm metadata mismatch. Документирована локальная команда проверки до выпуска.

## Задачи, зависимости, владельцы и приёмка

1. **Правила актуальности** — relevance_rules_junior (gpt-6-luna/low): минимальные
   вставки в AGENTS.md, orchestration docs и global coordinator skill. Candidate
   review hierarchy_profile_senior PASS; root устанавливает/проверяет skill.
2. **Backend/runtime/release** — hierarchy_profile_senior в роли senior backend
   (gpt-6.1-sol/medium). До кода root ACK точной декомпозиции/файлов. Single writer
   shared version/DTO, Core heartbeat/diagnostics/config/composition, Worker client,
   Analytics health, release guard, base Docker/preflight/Compose, связанные tests.
   UI package source не изменяет. Контракт выше закреплён до параллельной работы.
3. **Frontend/version UI/build** — relevance_rules_junior в роли junior frontend
   (gpt-6-luna/low), direct root узкий бриф: build-time own version; additive typed
   diagnostics service_versions; отображение OBSERVED/UNKNOWN; Vite npm gate и
   соответствующие specs. Single writer frontend files, без API/financial/auth changes.
4. **Интеграция/контекст** — root: принимает actual diff/evidence, переносит только
   проверенные файлы из isolated worktree, обновляет queue/development/observability/
   AGENT_BRIEF/CURRENT/WORK_LOG. Индекс владельца сохраняется, commit делает владелец.
5. **Независимое ревью** — milestone_relevance_architect в роли reviewer
   (gpt-6.1-sol/high), не пишет реализации; фактический code/tests/docs/integration,
   C/I исправляются авторами и перепроверяются. Root выполняет финальные проверки.

Параллельны 2 и3 после root ACK общего контракта; docs4 и review5 по завершении.
В worktree копируется текущий пользовательский patch без env/secrets, deps linked;
замороженный O1 worktree с0.1 не используется как источник.

## Root ACK файлов исполнителей

Backend A–D: `src/sentinel_contracts/{version,runtime_versions,release}.py`,
`src/moex_sentinel/__init__.py`, `views/schemas/automations.py`,
`views/internal_automaton.py`, `services/runtime_versions.py`,
`adapters/analytics_version.py`, `config.py`, `api/app.py`,
`usecases/diagnostics.py`, `views/diagnostics.py`,
`src/trading_automaton/adapters/core_client.py`, `src/market_analytics/app.py`;
`docker/python-base.Dockerfile`, `deploy/remote/preflight.py`, `compose.yml`,
`deploy/remote/compose.remote.yml`, `deploy/remote/compose.production.yml`,
`.env.example` (существующие owner изменения сохраняются).
Tests: `tests/services/test_runtime_versions.py`,
`tests/adapters/test_analytics_version.py`, `tests/test_release_versions.py`,
`tests/api/test_runtime_versions.py`, существующие diagnostics usecase/API,
health только при необходимости, Worker core_client, Analytics app, config,
Compose и production_preflight. Product files только в isolated worktree.

Frontend: `frontend/src/api/diagnostics.ts` и `.spec.ts`,
`frontend/src/views/DiagnosticsView.vue` и `.spec.ts`, `frontend/vite.config.ts`;
optional маленький `frontend/src/version.ts`/`.spec.ts` только при необходимости.
Версия интерфейса в diagnostics видна независимо от API; App/metadata/deps не меняются.
Других writer нет. Изменение списка файлов/риска требует внутреннего root ACK.

## Проверки и DoD

- Детерминированный Worker→Core→diagnostics с иной версией; legacy ACK; TTL
  before/exact/after, duplicate/older/future/scope mismatch, новая instance,
  Core restart и safe UTC. Нет persistence/health или trading побочных эффектов.
- Analytics actual health + HTTP adapter mock: timeout/malformed/oversize/error,
  never broker RPC; отдельная dependency/isolation проверка Analytics.
- Auth/no-store/oldhealth/readiness-threadpool сохраняются; отсутствующие PROD
  Worker/Analytics нормально UNKNOWN, TEST URL не подставляется PROD.
- UI own version при API failure; different runtime versions/UNKNOWN; manual
  refresh/abort; frontend test/typecheck/build.
- Намеренный mismatch каждого metadata source → release guard/Vite build FAIL,
  восстановление → PASS. Docker/remote entrypoints реально подключены к guard.
- Затронутые Python suites/Compose tests, Ruff/Black/diff, frontend suites/build;
  DB тесты только изолированные, PostgreSQL для данного изменения не требуется.
- Все обязательные задачи приняты, независимое CODE/DOC/INTEGRATION review без
  C/I; метаданные0.2, immutable scope и финансовые журналы сохранены.
- Только тогда V1 DONE / NOT_DEPLOYED. Это не свежая проверка VPS или допуск TRADE.

## Итоговая приёмка 03.10.2026

V1 **DONE / NOT_DEPLOYED**. Все обязательные задачи приняты: root35 exact reviewed
SHA в primary, nonPG1460PASS/0skip/17warnings; frontend142PASS/26files,
typecheck/build, Ruff/Black/releaseguard/diff PASS. Авторы backend/frontend указаны
выше; независимый milestone_relevance_architect Final CODE/DOC/INTEGRATION PASS,
C/I/M0 после исправления releaseguard override/rebinding и уточнения документации.
Предыдущий CHANGES REQUIRED и прогоны до исправления сохранены отдельными receipts.
Доказательства: develop/reports/v1-service-versions-20261003/{root-verification.json,
final-review.md}. 639 priorfiles и stagedmanifest владельца сохранены.

PostgreSQL suites исключены из данного прогона (схема/SQL не менялись); Compose
TEST/PROD проверены offline suites с реальным config resolution. Native Docker
build, live API, SSH и deployment не выполнялись. Source версия0.2 не подтверждает
версию VPS; обновление после commit владельца и отдельной отмашки.
Полные O1/O2/S1/M1 и PROD торговые gates остаются OPEN.

## Deployment receipt — 03.10.2026

Deployment commit `be84ed4f047734ccacfba9b6ccb531af51ba4b94` принят под release
`vps-20261003-be84ed4` по
`develop/reports/vps-release-20261003/root-native-receipts.json` (17:14 UTC).
Оба current links указывают на `/opt/moex-sentinel/releases/vps-20261003-be84ed4`;
schema `0003_user_broker_archive`. Native stage/build/permission/frontend-config/migration/
Core/Worker/finalize gates прошли; отдельная post-deploy HTTPS verification PASS.
Frozen diagnostics HTTP: Core TEST/PROD `0.2.0`, TEST Worker/Analytics OBSERVED `0.2.0`,
PROD Worker UNKNOWN и Analytics NOT_CONFIGURED. Frontend build metadata — `0.2.0`.
HTTP проверки покрывали index/assets, auth и status/API GET; browser acceptance не
проводилась. Отдельная архивная запись отражена только агрегатным read receipt с неизвестным
actor и не относится к HTTP verification. Dump был DUMP_ONLY (116246851 bytes, SHA и structural list; без paired
Worker backup/restore). TEST оставлен READ_ONLY и strategy=false; PROD Worker не запущен,
TRADE не разрешён. Версии и UNKNOWN состояния не подтверждают торговую готовность.

V1 source acceptance остаётся DONE; deployment принят в указанных границах. Полные O1/O2,
S1/M1, LIVE observation, 24h/7d, PROD Worker и TRADE остаются OPEN.
