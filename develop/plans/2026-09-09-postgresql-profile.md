# План выполнения 0.9.4: профиль доставки на PostgreSQL

> Для исполнителей: использовать superpowers:subagent-driven-development;
> каждый результат проверяет независимый агент по AGENTS.md.

**Цель:** воспроизводимо измерить Worker SQLite → HTTP ingress → Core PostgreSQL
и подтвердить неизменный replay после потерянного ACK.

**Архитектура:** вспомогательный стенд в develop использует реальные Worker,
CoreClient, HTTP router, ingress/UoW и миграции Core. Брокер и рынок синтетические.
PostgreSQL запускается отдельным временным контейнером; схема каждого сценария
изолирована. Приёмка не требует менять рабочие сервисы.

**Стек:** Python 3.12, SQLAlchemy/psycopg, FastAPI/uvicorn/httpx, PostgreSQL 16,
файловая Worker SQLite, pytest, Docker.

**Постановка:** [актуальный план](../../docs/phase-1-implementation-plan.md).

## Ограничения

- Один воспроизводимый запуск; инструменты 6/20/50, batch=100, 3 warmup,
  100 измеряемых WAIT-тактов. Начальные исполнения измеряются отдельно.
- Domain time синтетическое; интервалы измерять monotonic/perf_counter.
- Отдельно показывать очередь, HTTP roundtrip, ingress и транзакцию/commit;
  вложенные измерения не складывать как независимые.
- Не читать .env, credentials, рабочую БД; не менять persistent volumes и Git index.
- Без целевого порога не утверждать достижение production SLA.
- Оптимизации runtime — только после профиля и с отдельным объёмом.

## Задачи

### 1. Воспроизводимый PostgreSQL

Файлы: `develop/scripts/profile_postgresql.sh`,
`develop/tests/test_postgresql_profile_runner.py`.

- [x] RED: fake Docker проверяет уникальный контейнер, tmpfs, loopback,
  cleanup после успеха, ошибки benchmark и ошибки запуска PostgreSQL.
- [x] GREEN: shell runner создаёт контейнер postgres:16-alpine без volume,
  получает динамический порт, ждёт pg_isready, запускает модуль benchmark.
- [x] Передача интерфейса: только `PG_PROFILE_DATABASE_URL` созданной БД;
  CLI параметры передаются как отдельные аргументы.
- [x] Независимое ревью lifecycle и отсутствие обращения к рабочим ресурсам.

### 2. HTTP benchmark и измерения

Файлы: `develop/benchmarks/postgresql.py`, вспомогательные `pg_*` модули,
`develop/tests/test_postgresql_benchmark.py`.

- [x] RED: проверка реального HTTP, persisted facts и replay, статистики
  отдельных этапов, числа решений и отдельных профилей WAIT/BUY.
- [x] GREEN: `async run_case(database_url, instruments, *, warmup=3, iterations=100,
  batch_size=100)` возвращает только безопасные агрегаты.
- [x] Уникальная схема с Alembic head, корректный FK seed; cleanup своей схемы.
- [x] Реальные CoreClient/HTTP router/ingress/UoW; telemetry в develop.
- [x] Счётчик попыток SDK увеличивается до проверок внутри синтетического брокера.
- [x] Независимое ревью метрик, границ транзакции и достаточности replay-проверки.

### 3. Профиль и приёмка

Файлы: `develop/reports/postgresql-profile-2026-09-09.json`, README benchmark,
`docs/milestone-0.9.4-acceptance-2026-09-09.md`, текущие индексы.

- [x] Прогнать 6/20/50 по 100 измеряемых тактов на отдельном PostgreSQL.
- [x] Зафиксировать распределения, throughput, SQL стоимость, batch и условия.
- [x] Проверить lost ACK/restart, точное содержимое replay и отсутствие дублей.
- [x] Отделить найденное ограничение от гипотезы; обосновать следующий объём.
- [x] Выполнить релевантную регрессию, Ruff/Black и кросс-ревью итоговой интеграции.
- [x] Обновить статусы и остановить/удалить только созданные тестовые контейнеры.

Результат: [приёмка 0.9.4](../../docs/milestone-0.9.4-acceptance-2026-09-09.md).
