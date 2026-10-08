# Исправления слоистой архитектуры и SOLID — 06.10.2026

Статус: **LOCAL DONE / INDEPENDENT REVIEW PASS**. Исправления выполнены по принятому
[аудиту](layered-architecture-solid-20261006.md) и команде пользователя «тогда
исправим эти замечания». Исторический аудит не переписывается. Этот документ
фиксирует принятую кодовую веху, результаты и оставшиеся пределы проверки.

Исходный код: `8d4805e47a39843c4f74619e75918d31ed459915`, текущий checkout
`master`; исходный аудит и пользовательские изменения сохранены.
Scope, sole writers и порядок — в
[плане](../superpowers/plans/2026-10-06-layered-solid-fixes.md), границы — в
[AGENT_BRIEF](../../AGENT_BRIEF.md), очередь — в
[плане реализации](../phase-1-implementation-plan.md).

## Матрица разрешения исходных замечаний

Все 9 пунктов выполнены и приняты в рамках независимого интеграционного review.

| Пункт | Реализованное изменение | Локальное доказательство | Текущий статус |
| --- | --- | --- | --- |
| I1 missing broker | Actual UserBrokerNotFoundError подставляется в доменный lookup contract; accounts и TRADE create явно переводят ошибку. | RED 11 failed / 3 passed → GREEN 40; 12 actual composition HTTP routes возвращают 404 BROKER_NOT_FOUND без adapter construction. | Локально выполнено; независимое code review принято. |
| I2 stale decision admission | Проверки command/lifecycle до нового intent; ранее committed/sent intent продолжает supervision. | Cash re-materialization RED 1 failed / 3 passed → GREEN 30; SQLite two-connection RED 2 failed → targeted GREEN 131 PASS. Условный `BEGIN IMMEDIATE` до state reads только при новом intent; admission-first сохраняет committed intent, stale CAS reject и успешный fresh HOLD. | Оба followup исправлены и независимо перепроверены без замечаний. |
| I3 builder ownership | Failure/cancellation до передачи bundle закрывает SDK session и internally owned Analytics client, сохраняя первичную ошибку. | Genuine SDK RED 8 failures → GREEN 49 PASS. | Локально выполнено; независимое code review принято. |
| M1 storage dependency | Neutral persistence errors, narrow Core claim/status ports и Worker lot protocol; atomic repository не дробится. | Core error mapping 13 PASS; lot 9 PASS; junior M1/M4 assigned selection 55 PASS. | Локально выполнено; независимое code review принято. |
| M2 broker port | Actual UserBroker lookup/read capabilities; configuration writes UserBrokerDraft/archive отдельно. | Immutable/archive behavior smoke и Core regression 820 PASS. | Локально выполнено; независимое code review принято. |
| M3 vendor/transport semantics | Shared BrokerOperationError и typed BrokerRecoveryOperation; SDK/HTTP adapter translation, application policy зависит от neutral contracts. | Core SDK RED 35 failed / 1 passed → selected GREEN 75; cancellation/unexpected identity 10 PASS; Worker SDK selection 81 PASS. | Локально выполнено; независимое code review принято. |
| M4 decision/batch contract | DecisionContext, BatchTickResult.persisted: BatchPersistResult, прямое чтение committed fields; реальные DTO в doubles. | Junior assigned 55 PASS; batch runtime fake RED 13 passed / 3 failed → GREEN 16 PASS. | Локально выполнено; независимое code review принято. |
| M5 Analytics boundary | Hydration требует prepared metrics; удалены недостижимые local stream/history/calculator пути после caller check; planner/evaluator сохранены. | M5 50 PASS; calculator tests перенесены в Analytics — 55 PASS; before/after/dynamic caller graph evidence. Датированное уточнение ADR0007 внесено. | Локально выполнено; независимый recheck production/ADR scope принят. |
| M6 direction guards | Whole-src AST gate и negative absolute/relative import fixtures; допустимые adapter/composition imports по слоям. | Negative fixtures покрывают абсолютные/относительные импорты, aliases и `__init__.py`; архитектурная selection 39 PASS. | Локально пройдено; независимый recheck M6 принят. |

Shared safe error: `BrokerOperationError(code: str, safe_message: str, *, retryable: bool)`.
Recovery DTO frozen: operation_id, side, executed, occurred_at UTC milliseconds,
quantity_units Decimal, price/commission Decimal, currency. Quantity — instrument
units, не lots; vendor enums/dictionaries переводит SDK adapter.

Core market сохраняет пять permanent statuses: UNAUTHENTICATED,
PERMISSION_DENIED, INVALID_ARGUMENT, FAILED_PRECONDITION, UNIMPLEMENTED.
Прочие market transport failures transient; existing read mapper defaults не
меняются. Cancellation и неожиданные ошибки проходят без подмены. Transient
Core/Worker outages после короткого бюджета продолжают probes через 60 секунд;
permanent failure блокирует источник/runtime по существующим правилам.

## Проверки и evidence

Genuine pinned SDK `t-tech-investments==1.49.3` установлен из официального wheel
с `uv.lock` SHA256 и TLS verification. Process-only CA bundle использует tracked
Docker certificates и certifi; TLS/global trust/lock-файлы не менялись. Детали —
[environment evidence](../../develop/reports/layered-solid-fixes-20261006/environment/provenance.log).

Core команда с READ_ONLY и `PYTHONPATH=src`: `<python> -B -m pytest tests/api
tests/adapters tests/domain tests/services tests/storage tests/usecases
tests/test_composition.py tests/test_architecture.py -p no:cacheprovider` с
отдельным basetemp в develop. Результат: **820 PASS**, exit 0, 17 dependency
deprecation warnings, 31.66s; SDK exclusions сняты. Это выбранная Core regression,
не полный backend/PG/runtime PASS.
[Core отчёт](../../develop/reports/layered-solid-fixes-20261006/core/report.md),
[лог](../../develop/reports/layered-solid-fixes-20261006/core/full-core-regression.log).

Основные Worker receipts:
[I3 RED](../../develop/reports/layered-solid-fixes-20261006/worker/i3-red.log),
[I3 GREEN49](../../develop/reports/layered-solid-fixes-20261006/worker/i3-sdk-green.log),
[I2 freeze62](../../develop/reports/layered-solid-fixes-20261006/worker/i2-freeze-green.log),
[SDK81](../../develop/reports/layered-solid-fixes-20261006/worker/adapter-integration-correction-green.log),
[M5 caller graph](../../develop/reports/layered-solid-fixes-20261006/worker/m5-before-callers.log),
[junior M1/M4 отчёт](../../develop/reports/layered-solid-fixes-20261006/contracts-junior/report.md).
Дополнительные receipts: [I2 cash GREEN](../../develop/reports/layered-solid-fixes-20261006/worker/i2-cash-second-await-green.log),
[I2 SQLite targeted GREEN](../../develop/reports/layered-solid-fixes-20261006/worker/i2-review-final-green.log),
[M6 architecture report](../../develop/reports/layered-solid-fixes-20261006/architecture-junior/report.md),
[typed recovery fake review](../../develop/reports/layered-solid-fixes-20261006/recovery-fakes/report.md).

Исторический baseline до подключения тестовой PostgreSQL: полная offline backend
регрессия дала **1825 passed, 166 skipped, 17 warnings**, exit 0, 89.10s. Все
166 skips были связаны с отсутствующим `POSTGRES_TEST_DATABASE_URL`; этот запуск
не проверял PostgreSQL. Датированный follow-up с PostgreSQL приведён ниже.
Ruff прошёл (exit 0), Black check прошёл (448 files unchanged), `git diff --check`
прошёл до текущего изменения документа. Логи: [pytest](../../develop/reports/layered-solid-fixes-20261006/verification/pytest-final.log),
[Ruff](../../develop/reports/layered-solid-fixes-20261006/verification/ruff-final.log),
[Black](../../develop/reports/layered-solid-fixes-20261006/verification/black-final.log).

## PostgreSQL follow-up — 06.10.2026

После baseline root запустил полный backend suite на отдельной тестовой
PostgreSQL **16.15** в изолированном Compose project. Чистый итоговый запуск:
**1991 passed, 0 failed, 0 skipped, 17 warnings**, exit 0; pytest — 217.77s,
полная команда — 225.31s. XML содержит 1991 cases: PostgreSQL suite — 187 PASS,
включая прежние 166 skips, и ещё 21 проверку, не требовавшую БД. Оба paired
restore сценария завершились PASS. Исторический baseline выше сохранён как
результат запуска до тестовой БД, а не как текущий статус PostgreSQL.

Проверки изоляции подтвердили healthy PostgreSQL, только тестовый volume,
отсутствие оставшихся тестовых схем и восстановленных БД. Источники: [итоговая
матрица](../../develop/reports/postgresql-compose-20261006/verification/acceptance-counts.json),
[полный pytest log](../../develop/reports/postgresql-compose-20261006/verification/pytest-full.log),
[JUnit XML](../../develop/reports/postgresql-compose-20261006/verification/pytest-full.xml),
[pytest result](../../develop/reports/postgresql-compose-20261006/verification/pytest-full-result.json),
[PostgreSQL tools](../../develop/reports/postgresql-compose-20261006/verification/pg-tools.log),
[runtime inspection](../../develop/reports/postgresql-compose-20261006/verification/runtime-inspect.json),
[database health](../../develop/reports/postgresql-compose-20261006/verification/database-health.log).

Это локальная проверка на тестовой БД; live runtime и deployment не проверялись.
Исходники, миграции, API и стратегия не менялись, рабочие volumes не затрагивались.
Независимый `/root/postgres_compose_review`, не участвовавший в реализации,
принял актуальную интеграцию и PostgreSQL evidence: **FINAL INDEPENDENT REVIEW
PASS, Critical 0 / Important 0 / Minor 0**. Reviewer прочитал XML с 1991 cases,
187 PostgreSQL checks и два paired restore, source hashes, конфигурацию и receipts;
тесты запускал root. Проверка охватила изоляцию тестовой БД; production runtime и
deployment не проверялись.

## Независимое ревью и разрешённые followups

Независимый `/root/fixes_integration_review` не участвовал в реализации.
Первоначальные findings были **Critical 0 / Important 2 / Minor 1**; исправления
прошли повторную проверку. Финальный reviewer проверил полную интеграцию и эти
три closeout-документа, включая точечный diff очереди от 06.10, а также командные
профили: **INDEPENDENT REVIEW PASS, Critical 0 / Important 0 / Minor 0**.
Проверка подтвердила 448 source/test hashes без изменений после приёмки и чистый
HEAD diff check. Root зафиксировал финальное решение: LOCAL DONE.

- Исторический Important I2, `streaming_batch_tick.py`: отзыв предыдущей BUY во
  время cash await следующей теперь приводит к пересчёту cash по актуальным
  requests; barrier использует real cash materializer и бюджет ровно одной BUY.
  [Genuine GREEN: 30 passed](../../develop/reports/layered-solid-fixes-20261006/worker/i2-cash-second-await-green.log).
- Исторический Important I2, `repository.py.save_decision_batch`: conditional
  `BEGIN IMMEDIATE` сериализует lifecycle writer до admission reads только для
  batch с новым intent. WAIT/finalize interleavings и fact sequence сохранены;
  admission-first фиксирует intent, stale CAS отклоняет HOLD, свежий HOLD затем
  проходит штатно. [Targeted GREEN: 131 passed](../../develop/reports/layered-solid-fixes-20261006/worker/i2-review-final-green.log).
- Исторический Minor, ADR0007: local indicators loader/cache описывались как текущий
  planner-контракт после удаления. [Датированное уточнение](../decisions/0007-trading-automaton-runtime-decision-coordinator.md)
  сохраняет исторические разделы и описывает Analytics/shared prepared metrics;
  независимый повторный review замечаний не выявил.

Журнал: `develop/WORK_LOG.md`, записи «независимый Important: cash revocation во
втором await», «независимый Important: SQLite admission transaction» и
«промежуточное независимое ревью REVIEW_OPEN». Общее продолжение —
`develop/CURRENT.md`; их sole writer root. Historical audit acceptance касается
точности исходного аудита и не заменяет review этих исправлений.

## Условия завершения

- [x] Оба I2 Important имеют GREEN на описанных interleavings и повторный
  независимый review без существенных замечаний.
- [x] M6 negative fixtures и whole-src direction gate прошли; reviewer не нашёл
  существенных замечаний по M6.
- [x] Исторический offline baseline до PG follow-up: 1825 passed, 166 skips,
  17 warnings, exit 0.
- [x] Полная регрессия на отдельной PostgreSQL 16.15: 1991 passed, 0 failed,
  0 skipped, 17 warnings; оба paired restore сценария PASS.
- [x] Финальные Ruff, Black check и `git diff --check` прошли; независимое review
  проверило интеграцию и документацию после изменений.
- [x] Исходная кодовая веха принята как LOCAL DONE; PostgreSQL follow-up принят
  независимым review (`/root/postgres_compose_review`: PASS, Critical 0 / Important
  0 / Minor 0). Live runtime не проверялся.

Сохранены execution/ledger/cycle/outbox atomicity, UTC milliseconds,
sequence/revision/replay, TTL, immutable broker/account/archive scope и durable
recovery. Стратегия, новые внешние API, frontend, storage placement и миграции
не входят. Новые ветки разрешаются только отдельным явным распоряжением
пользователя; коммиты делает пользователь, агенты не stage/commit/push.
SSH, live broker API, TRADE и deploy не выполнялись. PostgreSQL проверена только
в изолированном тестовом Compose project; live runtime и deployment не проверялись.
